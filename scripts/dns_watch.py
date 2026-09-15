#!/usr/bin/env python3
"""Сторож DNS-записей федерации: алерт Борису в Telegram, если запись пропала.

Появился 2026-09-15. Из зоны shectory.ru (DNS у hoster.ru) пропало делегирование
unisenderlinks.shectory.ru на NS Unisender. Никто не заметил: узнали из письма
Unisender с ультиматумом «исправьте до 29.09, иначе домен ссылок удалим».
infra-guard проверяет только, открываются ли сайты, DNS-записи не смотрит.

Что делает (крон, раз в 30 минут):
1. Для каждой ожидаемой записи из dns_watch.json спрашивает АВТОРИТЕТНЫЕ серверы зоны
   напрямую (+norecurse) — без кешей резолверов, чтобы видеть правку сразу.
2. Сравнивает с прошлым состоянием и шлёт ОДНО сообщение на запуск: что пропало,
   что восстановилось. Пока запись сломана — напоминание раз в сутки.
3. Если сервер зоны не ответил вовсе, это не «записи нет», а «не смогли проверить»:
   такой запуск не меняет состояние и не шлёт ложную тревогу.
4. JSONL-журнал ~/logs/klod/dns_watch.jsonl, состояние ~/.klod/dns_watch_state.json.

LLM здесь не используются (правило Бориса для сторожей, как в infra-guard):
только dig и /api/tg/send.

Ловушка делегирования: на запрос NS поддомена с делегированием родительский сервер
отвечает не в ANSWER, а в AUTHORITY (referral). `dig +short` его не показывает, и
починенная запись выглядела бы пропавшей. Поэтому разбираются оба раздела.

Запуск:  dns_watch.py            — проверка и алерты
         dns_watch.py --dry-run  — проверка без алертов и без записи состояния
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("DNS_WATCH_CONFIG", str(HERE / "dns_watch.json")))
LINEMAN = os.environ.get("KLOD_LINEMAN", "http://127.0.0.1:9090").rstrip("/")
STATE_FILE = Path(os.environ.get("DNS_WATCH_STATE", str(Path.home() / ".klod/dns_watch_state.json")))
LOG_FILE = Path(os.environ.get("DNS_WATCH_LOG", str(Path.home() / "logs/klod/dns_watch.jsonl")))
REMIND_S = int(os.environ.get("DNS_WATCH_REMIND_S", "86400"))

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(entry: dict) -> None:
    entry["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def run_dig(server: str, name: str, rtype: str) -> tuple[str, str]:
    """Вернуть (статус, текст ответа). Статус: NOERROR/NXDOMAIN/... или UNREACHABLE."""
    try:
        p = subprocess.run(
            ["dig", "@" + server, name, rtype, "+norecurse", "+time=3", "+tries=2",
             "+noall", "+comments", "+answer", "+authority"],
            capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return "UNREACHABLE", ""
    out = p.stdout
    status = "UNREACHABLE"
    for line in out.splitlines():
        if "status:" in line:
            status = line.split("status:", 1)[1].split(",", 1)[0].strip()
            break
    return status, out


def parse_records(text: str, name: str, rtype: str) -> list[str]:
    """Значения записей name/rtype из разделов ANSWER и AUTHORITY вывода dig."""
    want = name.rstrip(".").lower()
    vals = []
    for line in text.splitlines():
        if not line or line.startswith(";"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        rname, _ttl, _cls, typ, value = parts
        if rname.rstrip(".").lower() == want and typ.upper() == rtype.upper():
            vals.append(value.strip().strip('"').replace('" "', ""))
    return vals


def evaluate(check: dict, servers: list[str], dig=run_dig) -> dict:
    """Проверить одну ожидаемую запись на всех серверах зоны.

    ok=True  — на каждом ответившем сервере есть все ожидаемые фрагменты;
    ok=False — хотя бы один ответивший сервер отдал запись без них;
    ok=None  — ни один сервер не ответил, проверка не состоялась.
    """
    name, rtype, need = check["name"], check["type"], check["contains"]
    answered, problems = 0, []
    for srv in servers:
        status, text = dig(srv, name, rtype)
        if status in ("UNREACHABLE", "SERVFAIL", "REFUSED"):
            continue
        answered += 1
        vals = parse_records(text, name, rtype)
        joined = " | ".join(vals).lower()
        missing = [n for n in need if n.lower() not in joined]
        if missing:
            problems.append("%s: %s, нет %s" % (srv, status, ", ".join(missing)))
    if answered == 0:
        return {"id": check["id"], "ok": None, "detail": "серверы зоны не ответили"}
    return {"id": check["id"], "ok": not problems, "detail": "; ".join(problems)}


def build_message(changes: list[dict], reminders: list[dict], checks: dict) -> str:
    lines = []
    broken = [c for c in changes if c["ok"] is False]
    fixed = [c for c in changes if c["ok"] is True]
    if broken:
        lines.append("🚨 DNS: пропали записи")
        for c in broken:
            lines.append("• %s — %s" % (checks[c["id"]]["title"], c["detail"]))
            if checks[c["id"]].get("hint"):
                lines.append("  что сделать: " + checks[c["id"]]["hint"])
    if fixed:
        lines.append("✅ DNS: восстановлены")
        for c in fixed:
            lines.append("• " + checks[c["id"]]["title"])
    if reminders:
        lines.append("⏰ DNS: всё ещё сломано")
        for c in reminders:
            lines.append("• %s — %s" % (checks[c["id"]]["title"], c["detail"]))
    return "\n".join(lines)


def tg_send(text: str) -> bool:
    try:
        body = json.dumps({"account": "default", "text": text[:3800]}).encode()
        req = urllib.request.Request(
            LINEMAN + "/api/tg/send", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Agent-Name": "dns-watch"})
        _NOPROXY.open(req, timeout=20).read()
        return True
    except Exception as e:
        _log({"event": "alert_fail", "err": str(e)[:200]})
        return False


def run(cfg: dict, state: dict, now: int, dig=run_dig, send=tg_send, dry: bool = False) -> dict:
    checks = {c["id"]: c for c in cfg["checks"]}
    results = [evaluate(c, cfg["servers"], dig) for c in cfg["checks"]]
    prev = state.setdefault("checks", {})
    changes, reminders = [], []
    for r in results:
        if r["ok"] is None:
            _log({"event": "unverifiable", "id": r["id"]})
            continue
        p = prev.get(r["id"])
        if p is None or p.get("ok") != r["ok"]:
            # Первый запуск: алертим только сломанное, про исправное молчим.
            if not (p is None and r["ok"]):
                changes.append(r)
            prev[r["id"]] = {"ok": r["ok"], "since": now, "notified": now, "detail": r["detail"]}
        elif r["ok"] is False and now - p.get("notified", 0) >= REMIND_S:
            reminders.append(r)
            p["notified"] = now
            p["detail"] = r["detail"]
    _log({"event": "run", "results": results, "changes": [c["id"] for c in changes],
          "reminders": [c["id"] for c in reminders], "dry": dry})
    msg = build_message(changes, reminders, checks) if (changes or reminders) else ""
    if msg and not dry:
        sent = send(msg)
        _log({"event": "alert_sent" if sent else "alert_not_sent", "text": msg[:300]})
    return {"results": results, "message": msg, "state": state}


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = _load_state()
    out = run(cfg, state, int(time.time()), dry=dry)
    for r in out["results"]:
        mark = {True: "ok  ", False: "FAIL", None: "????"}[r["ok"]]
        print("%s %-28s %s" % (mark, r["id"], r["detail"]))
    if out["message"]:
        print("\n--- сообщение%s ---\n%s" % (" (dry-run, не отправлено)" if dry else "", out["message"]))
    if not dry:
        _save_state(out["state"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
