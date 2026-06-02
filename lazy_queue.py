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
    # task-split: разбиение большой задачи на N мелких — нужен chain-of-thought,
    # лучше через большую gemma или DeepSeek-pro.
    "task-split":     [("lm-studio", "gemma-4-26b-a4b-it-imatrix"),
                       ("deepseek", "deepseek-v4-pro")],
    # Vision/multimodal: gemma-4-e4b-it на LM Studio принимает image_url
    # OpenAI-vision format. Идеально для тяжёлых задач 'опиши', 'извлеки',
    # OCR, captioning без оплаты Gemini Vision.
    "vision":   [("lm-studio", "gemma-4-e4b-it")],
    "ocr":      [("lm-studio", "gemma-4-e4b-it")],
    "caption":  [("lm-studio", "gemma-4-e4b-it")],
    "describe": [("lm-studio", "gemma-4-e4b-it")],
}
DEFAULT_ROUTE = [("ollama-hoster", "llama3.2:1b"),
                 ("lm-studio", "google/gemma-4-e4b"),
                 ("deepseek", "deepseek-v4-flash")]

# Local backends = zero cost. Если job ушёл на один из них вместо платного,
# считаем экономию относительно baseline-цены (deepseek-v4-flash по умолчанию,
# для тяжёлых kind — deepseek-v4-pro как разумная замена).
LOCAL_BACKENDS = {"ollama-hoster", "lm-studio"}

# USD per 1M tokens (from config.json pricing на 2026-06)
BASELINE_PRICE_FLASH = {"in": 0.14, "out": 0.28}  # deepseek-v4-flash
BASELINE_PRICE_PRO   = {"in": 0.435, "out": 0.87}  # deepseek-v4-pro

# kinds которые «дороже» (reasoning/24K+ context), baseline = pro
PRO_BASELINE_KINDS = {"reason", "critique", "summarise", "sweep_secsan"}

# System-prompt overlays: добавляются в начало system_prompt по флагу.
# terse — caveman-style, до 65% экономии output tokens на черновых задачах.
TERSE_OVERLAY = (
    "Reply in ≤80 words. No preamble. No closing. No pleasantries. "
    "No 'I think', no 'sure', no 'great question'. "
    "Bullet points or numbered list when possible. "
    "Code only when explicitly asked. "
    "Если нужен русский — короткие фразы, без воды, без эмоджи."
)


def with_terse_overlay(system_prompt: str) -> str:
    return TERSE_OVERLAY + ("\n\n" + system_prompt if system_prompt else "")


def compute_saved_usd(backend: str, kind: str, tokens_in: int, tokens_out: int) -> float:
    """Сколько долларов сэкономлено благодаря local backend.

    0.0 если ушло на платный backend (никакой экономии — это «настоящая» цена).
    Иначе: цена baseline-модели × actual tokens.
    """
    if backend not in LOCAL_BACKENDS:
        return 0.0
    price = BASELINE_PRICE_PRO if kind in PRO_BASELINE_KINDS else BASELINE_PRICE_FLASH
    return round(
        (tokens_in * price["in"] + tokens_out * price["out"]) / 1_000_000,
        6,
    )


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


SPLIT_SYSTEM = (
    "You split a large task into a list of small atomic subtasks. "
    "Each subtask must be independent, with minimal shared context, "
    "and answerable by a 1B-3B model. "
    "Output STRICT JSON only, no markdown, format: "
    '{"subtasks":[{"kind":"...","prompt":"...","max_tokens":NN},...]} '
    "kinds: tune, eval, lint, html, css, summarise, critique. "
    "Choose 3-10 subtasks. No prose, only the JSON."
)


def parse_split_response(text: str) -> list[dict]:
    """Извлечь subtasks из ответа LLM. Прощает markdown-обёртку и trailing prose."""
    s = text.strip()
    # Срезать markdown code-fence
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[-1].strip() in ("```", ""):
            lines = lines[:-1]
        s = "\n".join(lines[1:])
    # Найти первый valid JSON object
    try:
        data = json.loads(s)
    except Exception:
        # Найти {"subtasks":...}
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(s[start:end + 1])
            except Exception:
                return []
        else:
            return []
    subs = data.get("subtasks") if isinstance(data, dict) else None
    if not isinstance(subs, list):
        return []
    out = []
    for st in subs[:10]:  # cap N=10
        if not isinstance(st, dict):
            continue
        k = str(st.get("kind") or "tune")
        p = str(st.get("prompt") or "")
        mx = int(st.get("max_tokens") or 400)
        if p and k:
            out.append({"kind": k, "prompt": p, "max_tokens": mx})
    return out


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
                 tokens_in: int, tokens_out: int, latency_ms: int,
                 kind: str = "") -> None:
    saved = compute_saved_usd(backend, kind, tokens_in, tokens_out)
    with _conn() as c:
        c.execute(
            "UPDATE lazy_jobs SET status='done', ts_done=?, output=?, "
            "model_used=?, backend_used=?, tokens_in=?, tokens_out=?, latency_ms=?, "
            "saved_usd=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), output, model, backend,
             tokens_in, tokens_out, latency_ms, saved, job_id),
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
        "latency_ms, priority, saved_usd FROM lazy_jobs WHERE 1=1"
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
              SUM(CASE WHEN ts_done > datetime('now','-1 day') THEN COALESCE(tokens_in,0)+COALESCE(tokens_out,0) ELSE 0 END) AS tokens_24h,
              SUM(CASE WHEN ts_done > datetime('now','-1 day') THEN COALESCE(saved_usd,0) ELSE 0 END) AS saved_usd_24h,
              SUM(COALESCE(saved_usd,0)) AS saved_usd_total
            FROM lazy_jobs
        """).fetchone()
    if not row:
        return {}
    out = {k: (row[k] or 0) for k in row.keys()}
    # USD округлим до 4 знаков для отображения
    for k in ("saved_usd_24h", "saved_usd_total"):
        out[k] = round(float(out.get(k) or 0), 4)
    return out


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
