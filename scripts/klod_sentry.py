#!/usr/bin/env python3
"""klod_sentry — Дозор Клода-Доступа (cron */5).

Родился из инцидента 2026-07-02: keymaster-api завис на 11 часов (PM2 показывал
online, порт 9093 не принимал соединения), агенты ждали ответа, Карьера бросила
«дозор вхолостую». Sentry делает то, что тогда было некому:

1. Проверяет живость по ФАКТУ (HTTP-проба порта), а не по статусу PM2.
2. Пишет историю здоровья в ~/logs/klod/sentry.jsonl (для диагностики трендов).
3. Чинит то, чем владеет Klod-Access:
   - keymaster-api: pm2 restart при мёртвом порте (cooldown 30 мин, максимум 3/сутки)
   - klod-dispatch: systemctl --user restart при протухшем heartbeat
4. Алертит Борю в TG один раз на смену состояния (дедуп как у medic-watcher),
   а не каждые 5 минут.

Значения секретов сюда не попадают: пробы ходят только на /health.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
LINEMAN = "http://127.0.0.1:9090"
KEYMASTER = "http://127.0.0.1:9093"
HEARTBEAT_FILE = os.path.join(HOME, ".klod", "dispatch_heartbeat")
HEARTBEAT_MAX_AGE_S = 300          # poll диспетчера 20с; 5 мин тишины = мёртв
LOG_FILE = os.path.join(HOME, "logs", "klod", "sentry.jsonl")
STATE_FILE = os.path.join(HOME, ".klod", "sentry_state.json")
RESTART_COOLDOWN_S = 1800
RESTART_MAX_PER_DAY = 3
BORIS_CHAT = "36910539"
PM2_BIN = os.environ.get(
    "KLOD_PM2_BIN",
    "/home/shectory/.npm/_npx/5f7878ce38f1eb13/node_modules/pm2/bin/pm2",
)

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_ok(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with _NOPROXY.open(url, timeout=timeout) as r:
            return 200 <= r.status < 300, f"http {r.status}"
    except Exception as e:
        return False, str(e)[:120]


def check_lineman() -> dict:
    ok, detail = _http_ok(f"{LINEMAN}/health")
    return {"service": "lineman", "ok": ok, "detail": detail}


def check_keymaster() -> dict:
    ok, detail = _http_ok(f"{KEYMASTER}/health")
    return {"service": "keymaster", "ok": ok, "detail": detail}


def check_dispatch() -> dict:
    try:
        age = int(time.time()) - int(open(HEARTBEAT_FILE).read().strip())
        return {"service": "klod-dispatch", "ok": age <= HEARTBEAT_MAX_AGE_S,
                "detail": f"heartbeat age {age}s"}
    except Exception as e:
        # heartbeat появился 2026-07-03 — до первого tick файла нет
        return {"service": "klod-dispatch", "ok": False, "detail": f"no heartbeat: {e}"[:120]}


def check_klod_answers() -> dict:
    """Способен ли Клод НА САМОМ ДЕЛЕ ответить старшей моделью.

    Зачем отдельная проба. Остальные проверки смотрят, что порт отвечает 200, а
    `/health` зелен даже когда Клод не может ответить ни одному агенту: за ним стоит
    чужой лимит, до которого health-ручка не достаёт. 2026-09-18 Клод полтора суток
    отвечал федерации через слабые модели и фолбэки, а Дозор всё это время рапортовал
    «всё в порядке» — потому что проверял живость сокета, а не способность работать.

    Проба намеренно дешёвая: два слова в ответе, модель `normal`. Раз в 5 минут это
    несопоставимо дешевле, чем сутки незамеченной деградации. `latency_ms` пишем в
    журнал всегда: по нему видно, что ответы начали даваться через повторы, ещё до
    того, как проба совсем покраснеет.
    """
    body = json.dumps({"agent": "klod-sentry", "prompt": "ответь одним словом: ок",
                       "model_hint": "normal", "max_tokens": 16}).encode("utf-8")
    req = urllib.request.Request(f"{LINEMAN}/api/klod/ask", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with _NOPROXY.open(req, timeout=120) as r:
            data = json.loads(r.read() or b"{}")
        ms = int((time.monotonic() - t0) * 1000)
        text = (data.get("text") or data.get("answer") or "").strip()
        return {"service": "klod-answers", "ok": bool(text),
                "latency_ms": ms,
                "detail": f"ответил за {ms} мс" if text else "пустой ответ"}
    except urllib.error.HTTPError as e:
        ms = int((time.monotonic() - t0) * 1000)
        raw = e.read().decode("utf-8", "replace")[:160]
        # 429 здесь — это исчерпанный лимит ПОСЛЕ повторов 2/4/8/16 внутри Lineman.
        # Отличаем от поломки: чинить рестартом нечего, ждать бесполезно, но знать надо.
        kind = "лимит" if e.code == 429 else f"http {e.code}"
        return {"service": "klod-answers", "ok": False, "latency_ms": ms,
                "detail": f"{kind}: {raw}"}
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return {"service": "klod-answers", "ok": False, "latency_ms": ms,
                "detail": str(e)[:120]}


def _load_state() -> dict:
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _log(entry: dict) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send_tg(text: str) -> None:
    """Через Lineman. Если Lineman лежит — молчим: его смерть алертит medic-watcher."""
    try:
        body = json.dumps({"account": "default", "chat_id": BORIS_CHAT, "text": text}).encode()
        req = urllib.request.Request(
            f"{LINEMAN}/api/tg/send", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        _NOPROXY.open(req, timeout=8)
    except Exception:
        pass


# Срок OAuth-подписки Клода. refreshToken при обновлении НЕ продлевается, а войти
# заново можно только с Windows под VPN: с smain claude.ai отдаёт challenge
# Cloudflare. Если срок истечёт, Клод замолчит для всей федерации, и починить это
# с сервера будет нельзя — поэтому напоминание идёт заранее и само, а не зависит
# от того, работаем ли мы в этот день.
CREDS_FILE = os.path.join(HOME, ".claude", ".credentials.json")
CREDS_WARN_DAYS = int(os.environ.get("KLOD_SENTRY_CREDS_WARN_DAYS", "7"))
CREDS_URGENT_DAYS = int(os.environ.get("KLOD_SENTRY_CREDS_URGENT_DAYS", "2"))
# Путь к ярлыку на машине Бориса — он его сам туда положил, подсказываем дословно,
# чтобы не вспоминать, где лежит и как называется.
CREDS_SCRIPT = os.environ.get("KLOD_SENTRY_CREDS_SCRIPT",
                              r"C:\Dev\klod-creds-sync-v1.cmd")


def check_creds_expiry(st: dict, now: int) -> dict:
    """Сколько осталось жить OAuth-подписке и пора ли напоминать Борису.

    Напоминаем раз в сутки, когда до конца меньше CREDS_WARN_DAYS, и переходим на
    настойчивый тон за CREDS_URGENT_DAYS. Чаще раза в сутки не пишем: ежечасное
    напоминание о том, что нельзя сделать прямо сейчас, превращается в шум,
    который перестают читать.
    """
    try:
        with open(CREDS_FILE, encoding="utf-8-sig") as fh:
            oauth = json.load(fh).get("claudeAiOauth") or {}
        exp_ms = int(oauth.get("refreshTokenExpiresAt") or 0)
    except Exception as e:
        return {"service": "claude-creds", "ok": False,
                "detail": f"не прочитал {CREDS_FILE}: {str(e)[:60]}"}
    if exp_ms <= 0:
        return {"service": "claude-creds", "ok": False,
                "detail": "в файле нет refreshTokenExpiresAt"}

    left_days = (exp_ms / 1000.0 - now) / 86400.0
    when = datetime.datetime.fromtimestamp(exp_ms / 1000.0).strftime("%d.%m %H:%M")
    detail = "refreshToken до %s (%.1f дн)" % (when, left_days)

    if left_days > CREDS_WARN_DAYS:
        return {"service": "claude-creds", "ok": True, "detail": detail}

    # Одно напоминание в сутки на каждый оставшийся день: по мере приближения
    # ключ меняется, поэтому письмо приходит снова, но не чаще раза в день.
    tag = "creds-warn-%d" % int(max(0, left_days))
    if st.get("creds_notify_tag") != tag:
        st["creds_notify_tag"] = tag
        urgent = left_days <= CREDS_URGENT_DAYS
        head = ("СРОЧНО: вход Клода истекает через %.0f дн (%s)" % (left_days, when)
                if urgent else
                "Через %.0f дн истекает вход Клода (%s)" % (left_days, when))
        send_tg(head + "\n\nПодними VPN и запусти " + CREDS_SCRIPT
                + "\n\nЕсли скрипт скажет ПРОПУСК — сначала в claude выполни "
                  "/logout и /login, потом запусти снова."
                + ("\n\nПосле истечения Клод замолчит, и починить это с сервера "
                   "будет нельзя." if urgent else ""))
    return {"service": "claude-creds", "ok": left_days > CREDS_URGENT_DAYS,
            "detail": detail}


def _can_restart(st: dict, key: str, now: int) -> bool:
    hist = [t for t in st.get(f"{key}_restarts", []) if now - t < 86400]
    st[f"{key}_restarts"] = hist
    if hist and now - hist[-1] < RESTART_COOLDOWN_S:
        return False
    return len(hist) < RESTART_MAX_PER_DAY


def restart_keymaster(st: dict, now: int) -> str:
    if not _can_restart(st, "keymaster", now):
        return "skipped: cooldown/limit"
    r = subprocess.run([PM2_BIN, "restart", "keymaster-api"],
                       capture_output=True, text=True, timeout=60)
    st.setdefault("keymaster_restarts", []).append(now)
    return "restarted" if r.returncode == 0 else f"restart failed: {r.stderr[:120]}"


def restart_dispatch(st: dict, now: int) -> str:
    if not _can_restart(st, "dispatch", now):
        return "skipped: cooldown/limit"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    r = subprocess.run(["systemctl", "--user", "restart", "klod-dispatch"],
                       capture_output=True, text=True, timeout=60, env=env)
    st.setdefault("dispatch_restarts", []).append(now)
    return "restarted" if r.returncode == 0 else f"restart failed: {r.stderr[:120]}"


def main() -> None:
    now = int(time.time())
    checks = [check_lineman(), check_keymaster(), check_dispatch()]
    st_early = _load_state()
    checks.append(check_creds_expiry(st_early, now))
    # Пробу «Клод реально отвечает» делаем, только когда шлюз жив: иначе она просто
    # повторит уже известный отказ и зря прождёт свой таймаут.
    if checks[0]["ok"]:
        checks.append(check_klod_answers())
    st = st_early
    actions = []

    by_name = {c["service"]: c for c in checks}

    if not by_name["keymaster"]["ok"] and by_name["lineman"]["ok"]:
        # порт мёртв — чиним сами (владелец keymaster-api = Klod-Access)
        actions.append({"target": "keymaster-api", "result": restart_keymaster(st, now)})

    if not by_name["klod-dispatch"]["ok"] and by_name["lineman"]["ok"]:
        actions.append({"target": "klod-dispatch", "result": restart_dispatch(st, now)})

    # TG только на смену состояния (down->up, up->down), не каждые 5 минут
    prev = st.get("last_status", {})
    cur = {c["service"]: c["ok"] for c in checks}
    for svc, ok in cur.items():
        if svc in prev and prev[svc] != ok:
            mark = "восстановился" if ok else f"УПАЛ ({by_name[svc]['detail']})"
            acted = next((a["result"] for a in actions if svc in a["target"]), None)
            extra = f" Действие: {acted}." if acted else ""
            send_tg(f"[klod-sentry] {svc} {mark}.{extra}")
    st["last_status"] = cur

    _save_state(st)
    _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": checks,
          "actions": actions})


if __name__ == "__main__":
    main()
