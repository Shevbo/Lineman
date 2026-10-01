#!/usr/bin/env python3
"""Сторож расхода токенов Клода: на что именно уходит подписка.

Боря 2026-10-01: «чтобы Клод тратил токены строго на задачи от запросов других
агентов и окон, а не палил ресурсы на мнимые задачи селфчека».

Сразу о факте, который стоит знать: сами по себе SELFPING токенов НЕ тратят —
диспетчер отвечает на них PONG без обращения к модели (klod_dispatch, ветка
_is_selfping). Проверено 2026-10-01. Но проверять это следовало измерением, а не
доверием к комментарию в коде, и ровно для этого сторож и нужен: он показывает
расход по статьям, а не по ощущениям.

Что считается:
  * «задачи агентов» — запросы с именем живого агента федерации;
  * «служебное» — собственные пробы Клода, Дозора и watchdog'ов;
  * «неопознанное» — запросы без имени агента. Их доля важнее всего: именно там
    прячется расход, которого никто не заказывал.

Сторож ничего не перекрывает. Он поднимает тревогу, а решение — за Борисом:
автоматическая остановка LLM по счётчику однажды оставит федерацию немой в
худший момент.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.request

LINEMAN_DB = pathlib.Path(os.environ.get(
    "LINEMAN_DB", str(pathlib.Path.home() / "workspaces/infra/lineman/lineman.db")))
LOG = pathlib.Path(os.environ.get(
    "KLOD_TOKEN_GUARD_LOG",
    str(pathlib.Path.home() / "logs/klod/token_guard.jsonl")))
STATE = pathlib.Path(os.environ.get(
    "KLOD_TOKEN_GUARD_STATE", str(pathlib.Path.home() / ".klod/token_guard.json")))
LINEMAN = os.environ.get("KLOD_LINEMAN", "http://127.0.0.1:9090").rstrip("/")

# Имена, по которым запрос считается служебным: это пробы самой инфраструктуры,
# а не работа по заказу агента.
SERVICE_AGENTS = {"klod-sentry", "dispatch-selfping", "klod-support-watchdog",
                  "probe-agent", "lineman", "klod-access"}

# Пороги суточного расхода. Подобраны по факту: обычный день федерации — единицы
# миллионов токенов, 56 млн был день плотной работы в сессии с Борисом.
DAILY_WARN_TOKENS = int(os.environ.get("KLOD_TG_DAILY_WARN", "40000000"))
# Доля служебного расхода, выше которой это уже не фон, а утечка ресурса.
SERVICE_SHARE_WARN = float(os.environ.get("KLOD_TG_SERVICE_SHARE", "0.25"))
# Доля расхода без имени агента. Неопознанный расход опаснее служебного:
# его некому предъявить.
UNNAMED_SHARE_WARN = float(os.environ.get("KLOD_TG_UNNAMED_SHARE", "0.50"))
ALERT_DEDUP_S = int(os.environ.get("KLOD_TG_ALERT_DEDUP_S", "21600"))   # 6 часов
# Ниже этого объёма доли не анализируются — проценты от сотни тысяч токенов
# показывают случайность дежурного часа, а не поведение федерации.
MIN_TOTAL_FOR_SHARES = int(os.environ.get("KLOD_TG_MIN_TOTAL", "1000000"))

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def collect(hours: int) -> dict:
    """Расход за последние N часов по статьям.

    Окно задаётся строкой ISO с 'T': в request_log время хранится именно так, и
    сравнение с результатом datetime('now') (там пробел вместо 'T') даёт ложное
    совпадение для всех записей дня — эти грабли уже стоили одного разбора.
    """
    since = time.strftime("%Y-%m-%dT%H", time.localtime(time.time() - hours * 3600))
    con = sqlite3.connect("file:%s?mode=ro" % LINEMAN_DB, uri=True)
    try:
        # Только обращения к моделям. Без этого условия в выборку попадает
        # прокси-трафик (Telegram, CONNECT-туннели) — десятки тысяч записей с
        # нулевыми токенами, которые раздували долю «без имени» до 89% и давали
        # ложную тревогу о неопознанном расходе.
        rows = con.execute(
            "SELECT COALESCE(source_agent,''), "
            "       SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)), "
            "       COUNT(*) "
            "FROM request_log "
            "WHERE timestamp > ? "
            "  AND COALESCE(llm_provider,'') <> '' "
            "  AND (COALESCE(tokens_in,0) + COALESCE(tokens_out,0)) > 0 "
            "GROUP BY 1", (since,)).fetchall()
    finally:
        con.close()

    by_agent, service, unnamed, agents_work = {}, 0, 0, 0
    for name, tokens, calls in rows:
        tokens = int(tokens or 0)
        name = (name or "").strip()
        by_agent[name or "(без имени)"] = {"tokens": tokens, "calls": calls}
        if not name:
            unnamed += tokens
        elif name in SERVICE_AGENTS or name.startswith("klod-cli"):
            service += tokens
        else:
            agents_work += tokens
    total = service + unnamed + agents_work
    return {
        "window_hours": hours,
        "total_tokens": total,
        "agents_tokens": agents_work,
        "service_tokens": service,
        "unnamed_tokens": unnamed,
        "by_agent": dict(sorted(by_agent.items(),
                                key=lambda kv: -kv[1]["tokens"])[:12]),
    }


def judge(stats: dict) -> list[str]:
    """Поводы для тревоги. Пустой список — всё в порядке."""
    alerts = []
    total = stats["total_tokens"]
    # На малых объёмах доли ничего не значат: 89% от 90 тысяч токенов — это шум
    # тихого часа, а не утечка. Тревожим только когда есть о чём тревожиться.
    if total < MIN_TOTAL_FOR_SHARES:
        return []
    if total > DAILY_WARN_TOKENS:
        alerts.append("расход %.1f млн токенов за %dч — выше порога %.1f млн"
                      % (total / 1e6, stats["window_hours"],
                         DAILY_WARN_TOKENS / 1e6))
    if total > 0:
        share = stats["service_tokens"] / total
        if share > SERVICE_SHARE_WARN:
            alerts.append("служебные пробы съели %.0f%% расхода (порог %.0f%%)"
                          % (share * 100, SERVICE_SHARE_WARN * 100))
        unnamed = stats["unnamed_tokens"] / total
        if unnamed > UNNAMED_SHARE_WARN:
            alerts.append("%.0f%% расхода без имени агента — непонятно, кто тратит"
                          % (unnamed * 100))
    return alerts


def notify(text: str, tag: str) -> bool:
    """Сообщить Борису, но не чаще раза в ALERT_DEDUP_S на один повод."""
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    now = int(time.time())
    if now - int(state.get(tag, 0)) < ALERT_DEDUP_S:
        return False
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(LINEMAN + "/api/tg/send", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Agent-Name": "klod-token-guard"})
    try:
        with _NOPROXY.open(req, timeout=30):
            pass
    except Exception as e:
        print("[token-guard] не отправил алерт: %s" % str(e)[:120], file=sys.stderr)
        return False
    state[tag] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--quiet", action="store_true", help="не слать алерты")
    args = ap.parse_args()

    stats = collect(args.hours)
    alerts = judge(stats)
    record = dict(stats, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), alerts=alerts)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    total = stats["total_tokens"] or 1
    print("За %dч: всего %.2f млн токенов" % (args.hours, stats["total_tokens"] / 1e6))
    print("  задачи агентов : %7.2f млн (%2.0f%%)"
          % (stats["agents_tokens"] / 1e6, 100 * stats["agents_tokens"] / total))
    print("  служебные пробы: %7.2f млн (%2.0f%%)"
          % (stats["service_tokens"] / 1e6, 100 * stats["service_tokens"] / total))
    print("  без имени      : %7.2f млн (%2.0f%%)"
          % (stats["unnamed_tokens"] / 1e6, 100 * stats["unnamed_tokens"] / total))
    print("  крупнейшие потребители:")
    for name, rec in list(stats["by_agent"].items())[:6]:
        print("    %-24s %7.2f млн  %d запросов"
              % (name[:24], rec["tokens"] / 1e6, rec["calls"]))

    if not alerts:
        print("  тревог нет")
        return 0
    print("  ТРЕВОГА:")
    for a in alerts:
        print("   -", a)
    if not args.quiet:
        notify("Сторож токенов Клода за %dч:\n" % args.hours
               + "\n".join("- " + a for a in alerts), tag="|".join(sorted(alerts))[:80])
    return 1


if __name__ == "__main__":
    sys.exit(main())
