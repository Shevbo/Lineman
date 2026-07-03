#!/usr/bin/env python3
"""klod_rollcall — утренняя перекличка Клода и федерации (cron 08:30).

Дополняет klod_sentry (тот каждые 5 мин проверяет ядро): раз в утро проходит
ВСЮ систему общения Клода с агентами и чинит замерших:

1. Ядро Клода: Lineman /health, keymaster :9093, heartbeat диспетчера.
2. PM2-парк smain: любой app не в online → pm2 restart.
3. systemd-юниты (user): неактивный → systemctl --user restart.
4. Отвеченность inbox klod-access: не-сигнальные сообщения старше 30 мин без
   ответа. Если диспетчер при этом молчит и по журналу операций (не пытается) —
   рестарт klod-dispatch. Если пытается (например квота LLM) — только доклад.
5. Активность агентов node_map за 24ч по request_log — замершие в докладе.
6. hoster: age heartbeat Медика по SSH.

Итог — одна TG-сводка Боре + JSONL ~/logs/klod/rollcall.jsonl.
Чинит только то, чем владеет Klod-Access; удалённые узлы — доклад, не рестарт.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
LINEMAN = "http://127.0.0.1:9090"
KEYMASTER = "http://127.0.0.1:9093"
DB = os.path.join(HOME, "workspaces/infra/lineman/lineman.db")
HEARTBEAT_FILE = os.path.join(HOME, ".klod", "dispatch_heartbeat")
ACTIONS_LOG = os.path.join(HOME, "logs", "klod", "dispatch_actions.jsonl")
LOG_FILE = os.path.join(HOME, "logs", "klod", "rollcall.jsonl")
BORIS_CHAT = "36910539"
PM2_BIN = os.environ.get(
    "KLOD_PM2_BIN",
    "/home/shectory/.npm/_npx/5f7878ce38f1eb13/node_modules/pm2/bin/pm2",
)
SYSTEMD_UNITS = [
    "klod-dispatch", "klod-tg-bot", "openclaw-gateway", "career-bot",
    "builder", "keymaster-tg-bot", "keymaster-sync", "shectory-portal",
    "webui-career",
]
LIQUIDATED = {"main"}  # Tank ликвидирован 2026-07-03 — тишина от него нормальна
# Интерактивные боты работают только когда Боря им пишет — их суточная тишина
# в LLM-трафике легитимна, «замером» не считается (их живость = openclaw-gateway).
INTERACTIVE = {"qaper", "virtual-boris", "titan", "nurse", "guilya",
               "jobsearch-scanner", "resume-editor", "interview-coach",
               "inbox", "eshkola"}
SIGNAL_SENDERS = {"", "unknown", "(unknown)", "secret-leak-alert", "klod-access", "ea", "lineman"}
STALE_MIN = 30

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str, timeout: float = 8.0):
    with _NOPROXY.open(url, timeout=timeout) as r:
        return json.loads(r.read())


def _run(cmd: list[str], timeout: int = 60, full: bool = False) -> tuple[bool, str]:
    try:
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        out = (r.stdout or r.stderr).strip()
        return r.returncode == 0, out if full else out[:200]
    except Exception as e:
        return False, str(e)[:200]


def check_core(report: list, actions: list) -> None:
    for name, url in (("lineman", f"{LINEMAN}/health"), ("keymaster", f"{KEYMASTER}/health")):
        try:
            _get_json(url, 5)
            report.append((name, "ok", ""))
        except Exception as e:
            report.append((name, "DEAD", str(e)[:80]))
            if name == "keymaster":
                ok, out = _run([PM2_BIN, "restart", "keymaster-api"])
                actions.append(f"keymaster-api restart: {'ok' if ok else out}")
    try:
        age = int(time.time()) - int(open(HEARTBEAT_FILE).read().strip())
        report.append(("dispatch-heartbeat", "ok" if age < 300 else "STALE", f"{age}s"))
    except Exception as e:
        report.append(("dispatch-heartbeat", "MISSING", str(e)[:60]))


def check_pm2(report: list, actions: list) -> None:
    ok, out = _run([PM2_BIN, "jlist"], timeout=30, full=True)
    if not ok:
        report.append(("pm2", "DEAD", out[:80]))
        return
    try:
        apps = json.loads(out[out.index("["):])
    except Exception:
        report.append(("pm2", "UNPARSEABLE", out[:60]))
        return
    bad = [a["name"] for a in apps if a.get("pm2_env", {}).get("status") != "online"]
    for name in bad:
        rok, rout = _run([PM2_BIN, "restart", name])
        actions.append(f"pm2 {name} restart: {'ok' if rok else rout}")
    report.append(("pm2", "ok" if not bad else f"чинил: {','.join(bad)}", f"{len(apps)} apps"))


def check_systemd(report: list, actions: list) -> None:
    for unit in SYSTEMD_UNITS:
        ok, state = _run(["systemctl", "--user", "is-active", unit], timeout=15)
        if not ok or state != "active":
            rok, rout = _run(["systemctl", "--user", "restart", unit])
            actions.append(f"{unit} restart: {'ok' if rok else rout}")
            report.append((unit, "RESTARTED" if rok else "FAILED", state))


def _dispatch_recently_trying(minutes: int = 30) -> bool:
    """Диспетчер жив и ПЫТАЕТСЯ отвечать (пусть и с ошибками LLM)?"""
    try:
        cutoff = time.time() - minutes * 60
        with open(ACTIONS_LOG, encoding="utf-8") as f:
            for line in list(f)[-50:]:
                try:
                    ts = time.mktime(time.strptime(
                        json.loads(line)["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                    if ts >= cutoff:
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def check_inbox_answers(report: list, actions: list) -> None:
    """Главный симптом «Клод молчит»: свежие вопросы без ответов."""
    try:
        inb = _get_json(f"{LINEMAN}/api/agent/klod-access/inbox?since=0&limit=400")
        out = _get_json(f"{LINEMAN}/api/agent/klod-access/outbox?to=&since=0&limit=400")
    except Exception as e:
        report.append(("inbox-check", "FAIL", str(e)[:80]))
        return
    msgs = inb if isinstance(inb, list) else inb.get("messages", inb.get("inbox", []))
    outs = out if isinstance(out, list) else out.get("messages", out.get("outbox", []))
    answered = {o.get("in_reply_to") for o in outs if o.get("in_reply_to") is not None}
    now = time.time()
    stale = []
    for m in msgs:
        if str(m.get("from", "")).lower() in SIGNAL_SENDERS:
            continue
        ts = m.get("ts") or m.get("timestamp") or 0
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        age_min = (now - ts) / 60
        if m.get("id") not in answered and STALE_MIN < age_min < 24 * 60:
            stale.append(f"#{m.get('id')} {m.get('from')} ({int(age_min)}м)")
    if not stale:
        report.append(("перекличка-inbox", "ok", "без зависших"))
        return
    if _dispatch_recently_trying():
        report.append(("перекличка-inbox", "WAITING",
                       f"{len(stale)} без ответа, диспетчер пытается (вероятно квота LLM): "
                       + "; ".join(stale[:5])))
    else:
        ok, out_ = _run(["systemctl", "--user", "restart", "klod-dispatch"])
        actions.append(f"klod-dispatch restart (молчит при {len(stale)} зависших): "
                       f"{'ok' if ok else out_}")
        report.append(("перекличка-inbox", "FIXED", "; ".join(stale[:5])))


def check_agents_activity(report: list) -> None:
    try:
        cfg = json.load(open(os.path.join(HOME, "workspaces/infra/lineman/config.json")))
        node_map = cfg.get("agents", {}).get("node_map", {})
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT DISTINCT source_agent FROM request_log "
            "WHERE timestamp > datetime('now','-1 day') AND source_agent != ''"
        ).fetchall()
        con.close()
        active = {r[0].split(":")[-1].split("[")[0] for r in rows}
        silent = [a for agents in node_map.values() for a in agents
                  if a not in active and a not in LIQUIDATED and a not in INTERACTIVE]
        report.append(("агенты-24ч", "ok" if not silent else "тихие",
                       f"молчали: {', '.join(silent)}" if silent else "все активны"))
    except Exception as e:
        report.append(("агенты-24ч", "FAIL", str(e)[:80]))


def check_hoster(report: list) -> None:
    ok, out = _run(["ssh", "-o", "ConnectTimeout=10", "ubuntu@10.66.0.7",
                    "cat ~/.medic/heartbeat 2>/dev/null; echo; "
                    "awk '/MemAvailable/ {print int($2/1024)\"MB\"}' /proc/meminfo"],
                   timeout=25)
    if not ok:
        report.append(("hoster", "UNREACHABLE", out[:80]))
        return
    lines = out.splitlines()
    report.append(("hoster", "ok", f"medic hb: {lines[0][:19] if lines else '?'}, "
                                   f"mem avail: {lines[-1] if len(lines) > 1 else '?'}"))


def send_tg(text: str) -> None:
    try:
        body = json.dumps({"account": "default", "chat_id": BORIS_CHAT, "text": text}).encode()
        req = urllib.request.Request(f"{LINEMAN}/api/tg/send", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        _NOPROXY.open(req, timeout=8)
    except Exception:
        pass


def main() -> None:
    report: list[tuple[str, str, str]] = []
    actions: list[str] = []
    check_core(report, actions)
    check_pm2(report, actions)
    check_systemd(report, actions)
    check_inbox_answers(report, actions)
    check_agents_activity(report)
    check_hoster(report)

    problems = [r for r in report if r[1] not in ("ok",)]
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "report": report, "actions": actions},
                           ensure_ascii=False) + "\n")

    lines = ["[перекличка Клода]"]
    if not problems and not actions:
        lines.append(f"Все системы ок ({len(report)} проверок). Агенты на связи.")
    for name, status, detail in report:
        if status != "ok" or name in ("агенты-24ч", "hoster"):
            lines.append(f"{name}: {status}" + (f" — {detail}" if detail else ""))
    if actions:
        lines.append("Починил: " + "; ".join(actions))
    send_tg("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
