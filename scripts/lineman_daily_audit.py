#!/usr/bin/env python3
"""Daily Lineman audit — собирает KPI-сводку за последние 24 часа и пишет в docs/DAILY_YYYY-MM-DD.md.

Запуск из user crontab:
    0 9 * * * /usr/bin/python3 /home/shectory/workspaces/infra/lineman/scripts/lineman_daily_audit.py >> /home/shectory/logs/lineman/audit.log 2>&1

Базовый отчёт: docs/REPORT_2026-05-29.md. Этот скрипт пишет только дельту-за-сутки.
"""
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "lineman.db")
DOCS = os.path.join(ROOT, "docs")
HEALTH_URL = "http://127.0.0.1:9090/health"
POOL_URL = "http://127.0.0.1:9090/api/pool/stats"
METRICS_URL = "http://127.0.0.1:9090/metrics"
TG_ALERT_URL = "http://127.0.0.1:9090/api/agent/main/message"

# Gemini anti-dormant keep-alive
GEMINI_PROBE_URL = "http://127.0.0.1:9090/proxy/google/v1beta/models"
GEMINI_KEY_ENV = "GEMINI_API_KEY"

# Expected egress IPs — if they drift, GCP IP-allowlist breaks silently
EXPECTED_EGRESS = {
    "iproyal": "86.109.80.236",
    "proxy6": "23.236.141.49",
}

# KPI thresholds — нарушение каждого пишется в action items
KPI_PROVIDER_ERR_PCT = 5.0
KPI_GEOBLOCK_403_PER_DAY = 5
KPI_HUGE_CTX_TOKENS = 200_000
KPI_LEAK_PATTERNS = re.compile(r"(api_key|sk-[A-Za-z0-9_-]{10,}|Bearer\s+[A-Za-z0-9_.-]{20,})", re.I)


def _fetch(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _q(cur, sql: str):
    try:
        return cur.execute(sql).fetchall()
    except sqlite3.Error as e:
        return [("ERR", str(e))]


def _alert(text: str) -> None:
    try:
        from urllib.parse import urlencode
        params = urlencode({"from": "lineman-curator", "message": text[:400]})
        urllib.request.urlopen(f"{TG_ALERT_URL}?{params}", timeout=5).read()
    except Exception:
        pass


def _gemini_keepalive() -> tuple[bool, int]:
    """Daily call to /v1beta/models prevents dormant-blocking of unrestricted keys.
    Returns (ok, model_count). Keymaster file is authoritative (post-rotation);
    env GEMINI_API_KEY is only a fallback for ad-hoc runs."""
    key = ""
    try:
        key = open(os.path.expanduser("~/.keymaster/credentials/gemini_api_key")).read().strip()
    except Exception:
        key = os.environ.get(GEMINI_KEY_ENV, "")
    if not key:
        return False, 0
    try:
        url = f"{GEMINI_PROBE_URL}?key={key}"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
            return True, len(data.get("models", []))
    except Exception:
        return False, 0


def _check_egress_drift() -> list[str]:
    """Verify forward-proxy egress matches expected per-proxy IPs. Returns drift list."""
    drifts = []
    # We can't directly hit each pool member from cron; use the live forward path:
    try:
        with urllib.request.urlopen("http://127.0.0.1:9090/health", timeout=3) as r:
            json.loads(r.read())
    except Exception:
        return ["lineman not reachable"]
    # Just probe current forward egress once — this tells which proxy is active *now*.
    try:
        proxies = {"https": "http://127.0.0.1:9090", "http": "http://127.0.0.1:9090"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        ip = opener.open("https://api.ipify.org", timeout=8).read().decode().strip()
        if ip not in EXPECTED_EGRESS.values():
            drifts.append(f"unexpected egress IP {ip} (expected one of {list(EXPECTED_EGRESS.values())}) — обнови IP allowlist в GCP")
    except Exception as e:
        drifts.append(f"egress probe failed: {e}")
    return drifts


def main() -> int:
    today = datetime.date.today().isoformat()
    out_path = os.path.join(DOCS, f"DAILY_{today}.md")
    os.makedirs(DOCS, exist_ok=True)

    health = _fetch(HEALTH_URL) or {}
    pool = _fetch(POOL_URL) or {}
    metrics = _fetch(METRICS_URL) or {}
    gem_ok, gem_models = _gemini_keepalive()
    egress_drifts = _check_egress_drift()

    con = sqlite3.connect(DB)
    cur = con.cursor()

    total_24 = _q(cur, "SELECT COUNT(*) FROM request_log WHERE timestamp>datetime('now','-1 days')")[0][0]
    by_provider = _q(cur, """
        SELECT llm_provider, COUNT(*),
               SUM(CASE WHEN status_code BETWEEN 400 AND 599 THEN 1 ELSE 0 END),
               SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0))
        FROM request_log
        WHERE timestamp>datetime('now','-1 days') AND llm_provider IS NOT NULL AND llm_provider<>''
        GROUP BY llm_provider ORDER BY 2 DESC""")
    status_dist = _q(cur, """
        SELECT status_code, COUNT(*) FROM request_log
        WHERE timestamp>datetime('now','-1 days')
        GROUP BY status_code ORDER BY 2 DESC LIMIT 8""")
    geoblock_403 = _q(cur, """
        SELECT COUNT(*) FROM request_log
        WHERE status_code=403 AND llm_provider IN ('google','gemini','anthropic')
        AND timestamp>datetime('now','-1 days')""")[0][0]
    huge_ctx = _q(cur, """
        SELECT source_agent, llm_model, MAX(tokens_in), COUNT(*)
        FROM request_log
        WHERE timestamp>datetime('now','-1 days') AND tokens_in > ?
        GROUP BY source_agent, llm_model ORDER BY 3 DESC LIMIT 5""", )
    huge_ctx = _q(cur, f"""
        SELECT source_agent, llm_model, MAX(tokens_in), COUNT(*)
        FROM request_log
        WHERE timestamp>datetime('now','-1 days') AND tokens_in > {KPI_HUGE_CTX_TOKENS}
        GROUP BY source_agent, llm_model ORDER BY 3 DESC LIMIT 5""")
    leak_count = _q(cur, """
        SELECT COUNT(*) FROM request_log
        WHERE timestamp>datetime('now','-1 days')
        AND (request_body LIKE '%api_key%' OR request_body LIKE '%sk-%' OR request_body LIKE '%Bearer %')""")[0][0]
    connect_flagged = _q(cur, """
        SELECT COUNT(*) FROM request_log
        WHERE route_applied='connect_tunnel_llm_flagged' AND timestamp>datetime('now','-1 days')""")[0][0]
    ollama_ok = _q(cur, """
        SELECT SUM(CASE WHEN status_code=200 THEN 1 ELSE 0 END), COUNT(*)
        FROM request_log WHERE llm_provider='ollama-hoster' AND timestamp>datetime('now','-1 days')""")[0]
    dedup_hits = _q(cur, """
        SELECT COUNT(*) FROM request_log
        WHERE optimization LIKE 'dedup%' AND timestamp>datetime('now','-1 days')""")[0][0]
    flash_share = _q(cur, """
        SELECT
          CAST(SUM(CASE WHEN llm_model='deepseek-v4-flash' THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*),0)
        FROM request_log
        WHERE timestamp>datetime('now','-1 days') AND llm_model IS NOT NULL AND llm_model<>''""")[0][0] or 0.0

    db_size = os.path.getsize(DB) if os.path.exists(DB) else 0
    wal_size = os.path.getsize(DB + "-wal") if os.path.exists(DB + "-wal") else 0

    actions = []
    p0 = False

    if connect_flagged > 50:
        actions.append(f"P1: {connect_flagged} LLM-запросов прошли через CONNECT (агент не настроен на /proxy/{{provider}}/) — проверить smain клиентов.")
    if leak_count > 0:
        actions.append(f"P0: {leak_count} строк request_log содержат api_key/sk-/Bearer. Маскирование в reverse_proxy.py всё ещё не внедрено.")
        p0 = True
    if geoblock_403 > KPI_GEOBLOCK_403_PER_DAY:
        actions.append(f"P0: {geoblock_403} × 403 на LLM-провайдерах за сутки — возможна блокировка Gemini key. Проверь GCP Console → Credentials → IP allowlist / API restriction. См. .claude/memory/07_gemini_key_policy.md.")
        if geoblock_403 >= 3:
            p0 = True
    if not gem_ok:
        actions.append("P0: Gemini keep-alive FAILED — ключ может быть в dormant-block или quota исчерпана.")
        p0 = True
    for drift in egress_drifts:
        actions.append(f"P1: egress drift: {drift}")
    if huge_ctx:
        top_agent, top_model, top_tokens, _cnt = huge_ctx[0]
        actions.append(f"P1: tokens_in > {KPI_HUGE_CTX_TOKENS} y агента {top_agent} ({top_model}, max {top_tokens}). Подключить auto-summarise.")
    if ollama_ok[1] and ollama_ok[0] / max(ollama_ok[1], 1) < 0.5:
        actions.append(f"P0: ollama-hoster: {ollama_ok[0]}/{ollama_ok[1]} успехов за сутки. KPI 'ollama для простых' не выполняется.")
        p0 = True
    for prov, cnt, errs, _toks in by_provider:
        if cnt >= 50 and errs and 100 * errs / cnt > KPI_PROVIDER_ERR_PCT:
            actions.append(f"P1: {prov} error_rate {100*errs/cnt:.1f}% ({errs}/{cnt}). Расследовать.")
    if dedup_hits == 0:
        actions.append("P2: dedup_cache не пишет 'dedup_hit' в optimization — KPI 'ретраи' не измеряется. Поправить dedup_cache.py.")
    if flash_share > 0.85:
        actions.append(f"P2: {flash_share*100:.1f}% LLM-токенов уходит в deepseek-v4-flash. Router не сегментирует — настроить X-Lineman-Route у агентов.")

    tripped = pool.get("__tripped_circuits", []) if isinstance(pool, dict) else []
    if tripped:
        actions.append(f"P1: tripped circuits: {tripped}")

    lines = [
        f"# Lineman — суточная сводка {today}",
        "",
        f"Сравнение с base: [REPORT_2026-05-29.md](REPORT_2026-05-29.md).",
        "",
        "## Здоровье",
        f"- /health: {health.get('status','?')}, uptime {health.get('uptime_s',0)}s, requests {health.get('requests_served',0)}, errors {health.get('errors',0)}",
        f"- DB: {db_size/1024/1024:.1f}MB, WAL: {wal_size/1024/1024:.1f}MB",
        f"- Gemini keep-alive: {'ok' if gem_ok else 'FAIL'} ({gem_models} models visible)",
        f"- Egress drifts: {egress_drifts if egress_drifts else 'none'}",
        "",
        "## Pool за 24h",
    ]
    if isinstance(pool, dict):
        for pid, st in pool.items():
            if pid.startswith("__"):
                continue
            lines.append(
                f"- `{pid}`: success={st.get('success',0)} err={st.get('error',0)} "
                f"rate={st.get('error_rate',0):.2%} lat={st.get('avg_latency_ms',0):.0f}ms"
            )
    if tripped:
        lines.append(f"- TRIPPED: {tripped}")

    lines += [
        "",
        "## 24h объём",
        f"- Всего запросов: {total_24}",
        f"- 200/400/500 распределение: {status_dist}",
        f"- 403 на LLM (геоблок?): {geoblock_403}",
        f"- LLM через CONNECT (миснастройка): {connect_flagged}",
        f"- Подозрительные тела (api_key/sk-/Bearer): {leak_count}",
        f"- ollama-hoster success/total: {ollama_ok[0]}/{ollama_ok[1]}",
        f"- dedup_hit за сутки: {dedup_hits}",
        f"- Доля deepseek-flash в LLM: {flash_share*100:.1f}%",
        "",
        "## По провайдерам (24h)",
        "| provider | total | errors | err% | tokens |",
        "|---|---|---|---|---|",
    ]
    for prov, total, errs, toks in by_provider:
        rate = (100 * errs / total) if total else 0
        lines.append(f"| {prov} | {total} | {errs} | {rate:.1f}% | {toks} |")

    if huge_ctx:
        lines += ["", "## Раздутые контексты (tokens_in > 200K) за 24h"]
        for agent, model, max_t, cnt in huge_ctx:
            lines.append(f"- {agent}/{model}: max={max_t}, count={cnt}")

    lines += ["", "## Action items", ""]
    if not actions:
        lines.append("KPI в норме за сутки. Никаких действий.")
    else:
        for a in actions:
            lines.append(f"- {a}")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"daily audit → {out_path} ({len(actions)} action items)")

    if p0 and actions:
        head = "\n".join(a for a in actions if a.startswith("P0"))
        _alert(f"Lineman daily audit {today}: P0 действия\n{head}\nДеталь: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
