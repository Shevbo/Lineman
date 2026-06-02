"""Lazy Queue — отложенные не срочные LLM-задачи федерации.

Контракт:
- Любой агент `POST /api/queue/lazy` с {kind, prompt, system?, ...} → {job_id}.
- Worker (scripts/lazy_worker.py) тянет задачи по приоритету и шлёт на local LLM
  (LM Studio → Ollama-hoster → DeepSeek-flash fallback).
- Агент забирает `GET /api/queue/lazy/<id>` → {status, output, ...}.

См. docs/LAZY_QUEUE.md для подробного дизайна.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "lineman.db"
LINEMAN = "http://127.0.0.1:9090"
HTTP_TIMEOUT = 180

# Fallback chain per kind. Worker идёт по списку, пока кто-то не ответит 200.
# (backend_id, model). backend_id берётся из config.reverse_proxy.upstreams.
ROUTES: dict[str, list[tuple[str, str]]] = {
    "tune":      [("lm-studio", "google/gemma-4-e4b"),
                  ("ollama-hoster", "llama3.2:1b"),
                  ("deepseek", "deepseek-v4-flash")],
    "eval":      [("ollama-hoster", "llama3.2:1b"),
                  ("lm-studio", "google/gemma-4-e4b"),
                  ("deepseek", "deepseek-v4-flash")],
    "lint":      [("ollama-hoster", "llama3.2:1b"),
                  ("lm-studio", "google/gemma-4-e4b")],
    "html":      [("lm-studio", "gemma-4-26b-a4b-it-imatrix"),
                  ("deepseek", "deepseek-v4-flash")],
    "css":       [("lm-studio", "gemma-4-26b-a4b-it-imatrix"),
                  ("deepseek", "deepseek-v4-flash")],
    "summarise": [("lm-studio", "gemma-4-26b-a4b-it-imatrix"),
                  ("deepseek", "deepseek-v4-flash")],
    "critique":  [("lm-studio", "gemma-4-26b-a4b-it-imatrix"),
                  ("deepseek", "deepseek-v4-flash")],
    "reason":    [("lm-studio", "deepseek-r1-distill-qwen-14b"),
                  ("deepseek", "deepseek-v4-pro")],
    # sweep-варианты (federation_sweep.py)
    "sweep_doc":      [("lm-studio", "gemma-4-26b-a4b-it-imatrix"),
                       ("ollama-hoster", "llama3.2:1b")],
    "sweep_deadcode": [("lm-studio", "gemma-4-26b-a4b-it-imatrix")],
    "sweep_secsan":   [("lm-studio", "deepseek-r1-distill-qwen-14b")],
    "sweep_hardcode": [("lm-studio", "google/gemma-4-e4b"),
                       ("ollama-hoster", "llama3.2:1b")],
    "sweep_leaks":    [("lm-studio", "google/gemma-4-e4b"),
                       ("ollama-hoster", "llama3.2:1b")],
}
DEFAULT_ROUTE = [("ollama-hoster", "llama3.2:1b"),
                 ("lm-studio", "google/gemma-4-e4b"),
                 ("deepseek", "deepseek-v4-flash")]


# ────────────────────────────── DB helpers ──────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def submit_job(*, from_agent: str, from_node: str, kind: str,
               user_prompt: str, system_prompt: str = "",
               max_tokens: int = 600, temperature: float = 0.3,
               priority: int = 3, deadline_hint_minutes: int = 60) -> int:
    deadline = time.time() + (deadline_hint_minutes * 60) if deadline_hint_minutes else None
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO lazy_jobs
               (ts_created, from_agent, from_node, kind, system_prompt, user_prompt,
                max_tokens, temperature, priority, deadline_ts, status)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'queued')""",
            (datetime.now(timezone.utc).isoformat(),
             from_agent, from_node, kind, system_prompt, user_prompt,
             max_tokens, temperature, priority, deadline),
        )
        return cur.lastrowid


def claim_next() -> dict | None:
    """Атомарно: взять наивысший приоритет/старейший из status='queued'."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM lazy_jobs WHERE status='queued' "
            "ORDER BY priority ASC, id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE lazy_jobs SET status='running', ts_started=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
    return dict(row)


def complete_job(job_id: int, *, output: str, model: str, backend: str,
                 tokens_in: int, tokens_out: int, latency_ms: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE lazy_jobs SET status='done', ts_done=?, output=?, "
            "model_used=?, backend_used=?, tokens_in=?, tokens_out=?, latency_ms=? "
            "WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), output, model, backend,
             tokens_in, tokens_out, latency_ms, job_id),
        )


def fail_job(job_id: int, error: str, retries: int) -> None:
    status = "queued" if retries < 2 else "failed"
    with _conn() as c:
        c.execute(
            "UPDATE lazy_jobs SET status=?, error=?, retries=? WHERE id=?",
            (status, error[:500], retries + 1, job_id),
        )


def get_job(job_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM lazy_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(*, from_agent: str | None = None, status: str | None = None,
              limit: int = 50) -> list[dict]:
    q = "SELECT id, ts_created, ts_started, ts_done, from_agent, from_node, " \
        "kind, status, model_used, backend_used, tokens_in, tokens_out, " \
        "latency_ms, priority FROM lazy_jobs WHERE 1=1"
    params: list[Any] = []
    if from_agent:
        q += " AND from_agent=?"; params.append(from_agent)
    if status:
        q += " AND status=?"; params.append(status)
    q += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM lazy_jobs WHERE id=? AND status='queued'", (job_id,))
        return cur.rowcount > 0


def stats_24h() -> dict:
    with _conn() as c:
        row = c.execute("""
            SELECT
              SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
              SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
              SUM(CASE WHEN status='done' AND ts_done > datetime('now','-1 day') THEN 1 ELSE 0 END) AS done_24h,
              SUM(CASE WHEN status='failed' AND ts_done > datetime('now','-1 day') THEN 1 ELSE 0 END) AS failed_24h,
              SUM(CASE WHEN ts_done > datetime('now','-1 day') THEN COALESCE(tokens_in,0)+COALESCE(tokens_out,0) ELSE 0 END) AS tokens_24h
            FROM lazy_jobs
        """).fetchone()
    return {k: (row[k] or 0) for k in row.keys()} if row else {}


# ───────────────────── HTTP call to backend via Lineman ─────────────────

_NOPROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_backend(backend: str, model: str, system: str, user: str,
                 max_tokens: int, temperature: float, agent: str = "lazy-worker"
                 ) -> tuple[str, int, int, int]:
    """Возвращает (content, tokens_in, tokens_out, latency_ms). Raise on error."""
    url = f"{LINEMAN}/proxy/{backend}/v1/chat/completions"
    body = {
        "model": model,
        "messages": [
            *([{"role": "system", "content": system}] if system else []),
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Name": agent,
            "Authorization": "Bearer local",
        },
    )
    t0 = time.monotonic()
    with _NOPROXY_OPENER.open(req, timeout=HTTP_TIMEOUT) as r:
        data = json.loads(r.read())
    latency = int((time.monotonic() - t0) * 1000)
    if "choices" not in data:
        raise RuntimeError(f"backend {backend} no choices: {str(data)[:200]}")
    content = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage", {}) or {}
    return content, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), latency


def route_for_kind(kind: str) -> list[tuple[str, str]]:
    return ROUTES.get(kind, DEFAULT_ROUTE)
