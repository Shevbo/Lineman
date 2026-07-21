#!/usr/bin/env python3
"""Watchdog службы поддержки Клода-Доступа (3 независимые валидации + TG-алерты).

Крон-скрипт (*/5): проверяет что моя служба реально работает 24/7, а не молчит,
когда агенты пишут OPS. Создан 2026-07-21 после инцидента: klod-stl прислал
2 OPS в inbox, диспетчер ответил ack+завёл тикеты, а Клод-Доступ их не разобрал
несколько часов и STL решил вопрос сам, взяв контракт из garden напрямую.

3 независимые валидации (каждая может упасть отдельно):

    V1 selfping     end-to-end: посылаем OPS от 'dispatch-selfping' → диспетчер
                    обязан ответить PONG через outbox в течение 5 мин, иначе TG.
    V2 aging        тикет со статусом 'new' и note='жалоба от <agent>' старше
                    30 мин → OPS висит без разбора → TG (один раз на тикет).
    V3 heartbeat    systemctl --user is-active klod-dispatch klod-tg-bot;
                    mtime ~/.klod/dispatch_heartbeat не старше 3× POLL_S=60s.

Все TG уходят через Lineman /api/tg/send с dedup по tag'у в state-файле
(один и тот же алерт не спамит каждые 5 минут).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LINEMAN = os.environ.get("KLOD_LINEMAN", "http://127.0.0.1:9090").rstrip("/")
BORIS_CHAT_ID = os.environ.get("BORIS_TG_CHAT_ID", "36910539")
STATE_FILE = os.path.expanduser(
    os.environ.get("KLOD_WD_STATE", "~/.klod/support_watchdog_state.json"))
LOG_FILE = os.path.expanduser(
    os.environ.get("KLOD_WD_LOG", "~/logs/klod/support_watchdog.jsonl"))
HEARTBEAT_FILE = os.path.expanduser(
    os.environ.get("KLOD_DISPATCH_HEARTBEAT", "~/.klod/dispatch_heartbeat"))

V1_SELFPING_TIMEOUT_S = int(os.environ.get("KLOD_WD_V1_TIMEOUT_S", "300"))  # 5 мин
V2_AGING_THRESHOLD_S = int(os.environ.get("KLOD_WD_V2_AGING_S", "1800"))    # 30 мин
# Верхний лимит V2: тикеты старше — уже «болото» (я их проигнорил давно, cron
# не должен спамить Боре каждые 5 мин). Разовый триаж делается вручную, а V2
# фокусируется на СВЕЖИХ висяках 30мин…7дней.
V2_AGING_UPPER_S = int(os.environ.get("KLOD_WD_V2_UPPER_S", "604800"))       # 7 дней
V3_HEARTBEAT_MAX_AGE_S = int(os.environ.get("KLOD_WD_V3_HB_AGE_S", "180"))  # 3 мин
ALERT_DEDUP_S = int(os.environ.get("KLOD_WD_ALERT_DEDUP_S", "3600"))        # 1ч

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(entry: dict) -> None:
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _get(path: str) -> dict:
    with _NOPROXY.open(urllib.request.Request(LINEMAN + path), timeout=10) as r:
        return json.loads(r.read())


def _post(path: str, body_bytes: bytes = b"", ctype: str = "text/plain; charset=utf-8") -> dict:
    req = urllib.request.Request(
        LINEMAN + path, data=body_bytes, method="POST",
        headers={"Content-Type": ctype})
    with _NOPROXY.open(req, timeout=15) as r:
        raw = r.read()
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {"raw": raw.decode("utf-8", "replace")}


def _tg_alert(text: str, tag: str, st: dict) -> bool:
    """Один алерт на tag не чаще ALERT_DEDUP_S. True если реально отправили."""
    now = int(time.time())
    alerts = st.setdefault("alerts", {})
    last = int(alerts.get(tag, 0))
    if now - last < ALERT_DEDUP_S:
        return False
    try:
        _post("/api/tg/send",
              json.dumps({"account": "default", "chat_id": BORIS_CHAT_ID,
                          "text": text[:3800]}).encode("utf-8"),
              ctype="application/json")
        alerts[tag] = now
        _log({"event": "alert_sent", "tag": tag, "text": text[:200]})
        return True
    except Exception as e:
        _log({"event": "alert_fail", "tag": tag, "err": str(e)[:200]})
        return False


# ---------------------------------------------------------------------------
# V1 — end-to-end self-ping диспетчера
# ---------------------------------------------------------------------------

def v1_selfping(st: dict) -> None:
    """Двухфазный: сначала отправляем SELFPING, помечаем в state; на следующем
    прогоне (или дальше) проверяем что outbox содержит PONG с in_reply_to=<mid>.
    Если истёк V1_SELFPING_TIMEOUT_S без PONG → алерт + новая попытка."""
    now = int(time.time())
    pend = st.get("v1_pending")  # {"mid": int, "sent_at": int}
    if pend:
        try:
            out = _get(f"/api/agent/klod-access/outbox?to=dispatch-selfping&since=0&limit=200")
            answered = any(int(m.get("in_reply_to") or 0) == int(pend["mid"])
                           for m in (out.get("messages") or []))
        except Exception as e:
            _log({"event": "v1_outbox_fail", "err": str(e)[:200]})
            answered = False
        age = now - int(pend.get("sent_at", 0))
        if answered:
            _log({"event": "v1_pong_ok", "mid": pend["mid"], "age_s": age})
            st.pop("v1_pending", None)
        elif age > V1_SELFPING_TIMEOUT_S:
            _tg_alert(
                f"[V1 selfping] Диспетчер не ответил на SELFPING #{pend['mid']} "
                f"за {age}s (лимит {V1_SELFPING_TIMEOUT_S}s). klod-dispatch мёртв или "
                f"завис. journalctl --user -u klod-dispatch -n 100",
                tag="v1_selfping_timeout", st=st)
            st.pop("v1_pending", None)
    if "v1_pending" not in st:
        try:
            r = _post(
                f"/api/agent/klod-access/message?"
                f"{urllib.parse.urlencode({'from':'dispatch-selfping','node':'watchdog','topic':'selfping'})}",
                f"SELFPING ts={now}".encode("utf-8"))
            mid = int(r.get("id", 0))
            if mid > 0:
                st["v1_pending"] = {"mid": mid, "sent_at": now}
                _log({"event": "v1_selfping_sent", "mid": mid})
            else:
                _log({"event": "v1_selfping_no_id", "resp": r})
        except Exception as e:
            _tg_alert(f"[V1 selfping] Не могу положить SELFPING в inbox Lineman: "
                      f"{str(e)[:200]}. Проверь /api/agent/klod-access/message.",
                      tag="v1_selfping_send_fail", st=st)


# ---------------------------------------------------------------------------
# V2 — aging OPS-тикетов от агентов
# ---------------------------------------------------------------------------

def v2_backlog_aging(st: dict) -> None:
    """Максимум ОДИН алерт V2 за прогон (rate-limit /api/tg/send = 15s, флуд ни к
    чему). Берём самый старый ещё-не-уведомлённый OPS-тикет. Остальные ждут
    следующего cron-tick."""
    now = int(time.time())
    try:
        items = _get("/api/backlog").get("items", [])
    except Exception as e:
        _log({"event": "v2_backlog_fail", "err": str(e)[:200]})
        return
    already = st.get("alerts", {})
    candidates = []
    for it in items:
        if it.get("status") != "new":
            continue
        if "жалоба от" not in (it.get("note") or ""):
            continue  # только OPS от агентов
        bid = it.get("id", "?")
        if f"v2_aging:{bid}" in already:
            continue  # уже уведомили Борю про этот тикет
        created_ms = int(it.get("created") or 0)
        age_s = now - (created_ms // 1000)
        if age_s < V2_AGING_THRESHOLD_S or age_s > V2_AGING_UPPER_S:
            continue  # свежее 30мин или старше 7 дней — не алертим (см. константы)
        candidates.append((age_s, it))
    if not candidates:
        return
    candidates.sort(reverse=True)  # самый старый первым
    age_s, it = candidates[0]
    bid = it.get("id", "?")
    title = (it.get("title") or "")[:180]
    note = it.get("note", "")
    _tg_alert(
        f"[V2 aging] OPS-тикет #{bid} висит {age_s // 60}мин в статусе new. "
        f"«{title}». ({note[:80]}) — Клод-Доступ не разбирает, разбери или промоуть."
        + (f" Ещё {len(candidates) - 1} таких." if len(candidates) > 1 else ""),
        tag=f"v2_aging:{bid}", st=st)


# ---------------------------------------------------------------------------
# V3 — systemd-alive + heartbeat диспетчера
# ---------------------------------------------------------------------------

def _systemctl_active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _pm2_running(name: str) -> bool:
    try:
        r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "[]")
        for p in data:
            if p.get("name") == name:
                return (p.get("pm2_env", {}).get("status") == "online")
    except Exception:
        pass
    return False


def v3_heartbeat(st: dict) -> None:
    now = int(time.time())
    # 3.1 systemd юниты
    for unit in ("klod-dispatch", "klod-tg-bot"):
        if not _systemctl_active(unit):
            _tg_alert(f"[V3 systemd] {unit} НЕ active. "
                      f"systemctl --user status {unit}",
                      tag=f"v3_systemd:{unit}", st=st)
    # 3.2 PM2 lineman-gateway
    if not _pm2_running("lineman-gateway"):
        _tg_alert("[V3 pm2] lineman-gateway НЕ online. pm2 status.",
                  tag="v3_pm2:lineman-gateway", st=st)
    # 3.3 heartbeat файл диспетчера
    try:
        mtime = int(os.path.getmtime(HEARTBEAT_FILE))
        age = now - mtime
        if age > V3_HEARTBEAT_MAX_AGE_S:
            _tg_alert(f"[V3 heartbeat] {HEARTBEAT_FILE} не обновлялся {age}s "
                      f"(лимит {V3_HEARTBEAT_MAX_AGE_S}s). Цикл tick() klod-dispatch завис.",
                      tag="v3_heartbeat", st=st)
    except FileNotFoundError:
        _tg_alert(f"[V3 heartbeat] Файла {HEARTBEAT_FILE} нет. "
                  f"Диспетчер ни разу не тикал после старта.",
                  tag="v3_heartbeat_missing", st=st)


def main() -> int:
    st = _load_state()
    try:
        v1_selfping(st)
    except Exception as e:
        _log({"event": "v1_exception", "err": str(e)[:300]})
    try:
        v2_backlog_aging(st)
    except Exception as e:
        _log({"event": "v2_exception", "err": str(e)[:300]})
    try:
        v3_heartbeat(st)
    except Exception as e:
        _log({"event": "v3_exception", "err": str(e)[:300]})
    _save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
