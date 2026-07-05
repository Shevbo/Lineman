#!/usr/bin/env python3
"""claude_cli_budget_guard — сторож дневного расхода claude-cli агентов (cron hourly).

Зачем: Claude Code (claude-cli:<workspace>) ходит в Anthropic НАПРЯМУЮ, минуя
Lineman (TLS-туннель). Его токены Lineman видит только постфактум через
session_import (~/.claude/projects → request_log, крон каждые 2ч). Live-кап на
такой трафик невозможен. Единственный рычаг — детект превышения в тот же день и
алерт Боре, чтобы 71M/сутки (инцидент 2026-07-05, career-bot уронил общий
Anthropic-аккаунт rate-limit'ом для ВСЕЙ федерации) не жглись незамеченными весь день.

Лимиты в config.json → reverse_proxy.claude_cli_daily_budgets (per-agent, tokens/day),
дефолт reverse_proxy.claude_cli_default_budget. Алерт дедупится 6ч на агента.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request

HOME = os.path.expanduser("~")
LINEMAN = "http://127.0.0.1:9090"
DB = os.path.join(HOME, "workspaces/infra/lineman/lineman.db")
CONFIG = os.path.join(HOME, "workspaces/infra/lineman/config.json")
STATE = os.path.join(HOME, ".klod", "cli_budget_guard.json")
LOG = os.path.join(HOME, "logs", "klod", "cli_budget_guard.jsonl")
BORIS_CHAT = "36910539"
RESEND_S = 21600  # 6ч между повторными алертами одного агента
DEFAULT_BUDGET = 30_000_000

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _cfg() -> tuple[dict, int]:
    try:
        rp = json.load(open(CONFIG, encoding="utf-8")).get("reverse_proxy", {}) or {}
        return (rp.get("claude_cli_daily_budgets", {}) or {},
                int(rp.get("claude_cli_default_budget", DEFAULT_BUDGET)))
    except Exception:
        return {}, DEFAULT_BUDGET


def _today_burn() -> dict[str, int]:
    """{agent: tokens} за сегодня (MSK) для claude-cli:* по request_log."""
    day = time.strftime("%Y-%m-%d")  # локальная TZ сервера = MSK
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT source_agent, "
            "COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) "
            "FROM request_log WHERE source_agent LIKE 'claude-cli:%' "
            "AND timestamp >= ? GROUP BY source_agent",
            (day + "T00:00:00",)).fetchall()
    finally:
        con.close()
    return {r[0]: int(r[1]) for r in rows if r[1]}


def _load_state() -> dict:
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def send_tg(text: str) -> None:
    try:
        body = json.dumps({"account": "default", "chat_id": BORIS_CHAT, "text": text}).encode()
        req = urllib.request.Request(f"{LINEMAN}/api/tg/send", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        _NOPROXY.open(req, timeout=8)
    except Exception:
        pass


def main() -> None:
    budgets, default = _cfg()
    burn = _today_burn()
    st = _load_state()
    now = int(time.time())
    over = []
    for agent, toks in burn.items():
        short = agent.split(":", 1)[1] if ":" in agent else agent
        limit = int(budgets.get(short, default))
        if toks > limit:
            last = st.get(agent, 0)
            over.append((agent, toks, limit, now - last >= RESEND_S))
    fired = []
    for agent, toks, limit, do_alert in over:
        if do_alert:
            send_tg(f"[cli-budget-guard] {agent}: сожжено {toks//1_000_000}M токенов сегодня "
                    f"(лимит {limit//1_000_000}M). Claude Code жрёт Anthropic напрямую — "
                    f"проверь сессию, большие несжатые контексты роняют общий аккаунт.")
            st[agent] = now
            fired.append(agent)
    _save_state(st)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "burn": burn, "over": [o[0] for o in over],
                            "alerted": fired}, ensure_ascii=False) + "\n")
    print(f"burn={ {k.split(':')[-1]: v//1_000_000 for k,v in burn.items()} } alerted={fired}")


if __name__ == "__main__":
    main()
