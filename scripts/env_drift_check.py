#!/usr/bin/env python3
# Сверяет HTTPS_PROXY / LINEMAN_*_URL у живых PM2-процессов с актуальным
# значением из Keymaster HTTP API. Если у долгоживущего сервиса в env залип
# старый прокси (типичный сценарий: секрет в Keymaster ротировали, а
# pm2-сервис не перезапустили с --update-env) — присылает ОДНУ TG-нотификацию,
# после чего молчит пока ситуация не изменится. Никаких LLM, никакого спама.
#
# Cutover 2026-08-11: источник истины — Keymaster HTTP (~/keymaster/.lineman-proxy.env
# удалён после инцидента утечки fed-backup msg 24302). До cutover читалось из .env.
#
# Запуск: cron каждые 15 минут (см. crontab).
import json
import os
import subprocess
import sys
import urllib.request
import urllib.parse

HOME = os.path.expanduser("~")
KMASTER_URL = "http://127.0.0.1:9093"
STATE_FILE = os.path.join(HOME, ".cache", "lineman_env_drift_state.json")
LINEMAN_TG_URL = "http://127.0.0.1:9090/api/tg/send"
CHAT_ID = 36910539
TRACKED_VARS = ("LINEMAN_IPROYAL_URL", "LINEMAN_PROXY6_URL", "LINEMAN_PROXY1_URL")
PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "LINEMAN_IPROYAL_URL", "LINEMAN_PROXY6_URL", "LINEMAN_PROXY1_URL")


def _km_get(name: str) -> str:
    """Fetch current value of secret from Keymaster HTTP. Empty on any error —
    drift check просто пропустит эту переменную (не будет ложных алертов если
    Keymaster временно недоступен)."""
    url = (f"{KMASTER_URL}/keymaster/request-value?name={name}"
           f"&requester=lineman&purpose=env-drift-check")
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return ""
    if d.get("status") != "approved":
        return ""
    delivery = d.get("delivery", "")
    if not delivery:
        return ""
    path = delivery.replace("~", HOME, 1) if delivery.startswith("~") else delivery
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def parse_keymaster():
    """Возвращает {VAR: current_value} из Keymaster. Ранее читалось из .env файла;
    после cutover 2026-08-11 — только через Keymaster HTTP."""
    expected = {}
    for var in TRACKED_VARS:
        v = _km_get(var)
        if v:
            expected[var] = v
    return expected


def pm2_processes():
    try:
        out = subprocess.check_output(["pm2", "jlist"], text=True, timeout=15)
        return json.loads(out)
    except Exception:
        return []


def read_proc_env(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read()
        env = {}
        for kv in data.split(b"\x00"):
            if b"=" in kv:
                k, v = kv.split(b"=", 1)
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return env
    except Exception:
        return {}


def sanitize(url):
    if "@" not in url:
        return url
    head, tail = url.split("@", 1)
    if "://" in head:
        scheme, _ = head.split("://", 1)
        return f"{scheme}://CREDS@{tail}"
    return f"CREDS@{tail}"


def detect_drift(expected, pm2_list):
    """Returns list of {svc, var, expected, actual} for drifted services."""
    drifts = []
    iproyal = expected.get("LINEMAN_IPROYAL_URL", "")
    proxy6 = expected.get("LINEMAN_PROXY6_URL", "")
    for proc in pm2_list:
        name = proc.get("name", "?")
        pid = proc.get("pid") or 0
        status = (proc.get("pm2_env") or {}).get("status", "")
        if status != "online" or not pid:
            continue
        env = read_proc_env(pid)
        if not env:
            continue
        for var in PROXY_ENV_KEYS:
            actual = env.get(var)
            if not actual:
                continue
            # Сравниваем с iProyal (если совпадает по хосту) и с proxy6 — точно
            # совпадает full URL → ok. Если хост совпадает, а creds нет → drift.
            for label, exp_url in (("iproyal", iproyal), ("proxy6", proxy6)):
                if not exp_url or "@" not in exp_url:
                    continue
                exp_host = exp_url.split("@", 1)[1]
                if "@" in actual and actual.split("@", 1)[1] == exp_host:
                    if actual != exp_url:
                        drifts.append({
                            "svc": name,
                            "var": var,
                            "expected": sanitize(exp_url),
                            "actual": sanitize(actual),
                            "expected_raw": exp_url,
                            "actual_raw": actual,
                            "kind": label,
                        })
                    break
    return drifts


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_drift_hash": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tg_notify(text):
    payload = json.dumps({"account": "default", "chat_id": CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        LINEMAN_TG_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    expected = parse_keymaster()
    if not expected:
        return 0
    pm2_list = pm2_processes()
    if not pm2_list:
        return 0
    drifts = detect_drift(expected, pm2_list)
    state = load_state()
    drift_key = json.dumps(
        sorted([(d["svc"], d["var"], d["actual_raw"], d["expected_raw"]) for d in drifts])
    )
    if drift_key == state.get("last_drift_hash"):
        return 0
    if drifts:
        lines = [
            "Lineman env-drift: PM2-сервисы держат старые proxy-creds (нужен `pm2 restart <svc> --update-env`)",
        ]
        seen = set()
        for d in drifts:
            row = f"  • {d['svc']}: {d['var']} → актуальный {d['kind']} {d['expected']}, в env {d['actual']}"
            if row not in seen:
                seen.add(row)
                lines.append(row)
        tg_notify("\n".join(lines))
    else:
        # переход «дрифт был → стало чисто»
        if state.get("last_drift_hash") not in (None, "[]"):
            tg_notify("Lineman env-drift: всё синхронизировано с keymaster.")
    state["last_drift_hash"] = drift_key
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
