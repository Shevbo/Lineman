"""Двусторонний канал messaging для агента klod-access + автотриаж жалоб.

Inbox — это append-only JSONL (`~/klod-access/inbox.jsonl`). Каждая строка:
    {"id": N, "ts": "ISO", "from": "agent_id", "node": "smain", "message": "...",
     "meta": {"kind": "complaint", "triage": {...}}}

Outbox — replies от klod-access обратно отправителю, формат тот же файл
`outbox.jsonl`, плюс попытка прямой доставки через `/api/agent/{to}/message`.

Авто-триаж: если в сообщении распознан паттерн жалобы (по словам ниже),
write_inbox автоматически собирает последние 5 ошибочных запросов агента
из request_log и кладёт их в meta.triage. Это помогает Klod-Access
сразу видеть контекст проблемы без отдельного запроса в БД.

Endpoints (см. proxy_server._raw_api_klod_access):
- POST /api/agent/klod-access/message?from=X[&node=Y]   body=text  → inbox
- GET  /api/agent/klod-access/inbox?since=N&limit=K     → JSONL
- POST /api/agent/klod-access/reply?to=X[&in_reply_to=N] body=text → outbox + forward
- GET  /api/agent/klod-access/outbox?since=N            → JSONL
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

INBOX_DIR = Path.home() / "klod-access"
INBOX_FILE = INBOX_DIR / "inbox.jsonl"
OUTBOX_FILE = INBOX_DIR / "outbox.jsonl"
COUNTER_FILE = INBOX_DIR / "counter.txt"
PUSH_URLS_FILE = INBOX_DIR / "push_urls.json"

_PUSH_URL_RE = re.compile(r"^https?://[A-Za-z0-9._:\-]+(/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?$")

DB_PATH = Path(__file__).resolve().parent / "lineman.db"

_COMPLAINT_RE = re.compile(
    r"(?i)\b(401|403|429|5\d\d|timeout|timed out|ошибка|упал|не работает|"
    r"недоступн|blocked|заблокирован|quota|rate\s?limit|"
    r"can\'?t|cannot|fail|error|exception|broken|crash|жалоб)\b"
)


def _is_complaint(message: str) -> bool:
    return bool(message and _COMPLAINT_RE.search(message))


def _triage_from_db(agent: str, window_minutes: int = 60) -> dict[str, Any] | None:
    """Собрать последние 5 ошибочных запросов агента — root-cause hints для Klod-Access."""
    if not DB_PATH.exists() or not agent:
        return None
    try:
        con = sqlite3.connect(str(DB_PATH))
        cur = con.cursor()
        rows = cur.execute(
            """SELECT timestamp, llm_provider, target_host, status_code,
                      substr(COALESCE(error,''),1,160), route_applied,
                      tokens_in, latency_ms
               FROM request_log
               WHERE source_agent = ?
                 AND timestamp > datetime('now', ?)
                 AND (status_code >= 400 OR error IS NOT NULL)
               ORDER BY id DESC LIMIT 5""",
            (agent, f"-{int(window_minutes)} minutes"),
        ).fetchall()
        total = cur.execute(
            "SELECT COUNT(*) FROM request_log WHERE source_agent = ? "
            "AND timestamp > datetime('now', ?)",
            (agent, f"-{int(window_minutes)} minutes"),
        ).fetchone()[0]
        errs = cur.execute(
            "SELECT COUNT(*) FROM request_log WHERE source_agent = ? "
            "AND timestamp > datetime('now', ?) AND status_code >= 400",
            (agent, f"-{int(window_minutes)} minutes"),
        ).fetchone()[0]
        con.close()
        recent_errors = [
            {
                "ts": r[0], "provider": r[1], "host": r[2], "status": r[3],
                "error": r[4], "route": r[5], "tokens_in": r[6], "latency_ms": r[7],
            }
            for r in rows
        ]
        return {
            "window_min": window_minutes,
            "agent_requests_total": total,
            "agent_requests_errored": errs,
            "agent_error_rate_pct": round(100.0 * errs / total, 1) if total else 0.0,
            "recent_errors": recent_errors,
        }
    except Exception as e:
        logger.exception("triage_failed")
        return {"error": str(e)}


def _ensure_dir() -> None:
    INBOX_DIR.mkdir(mode=0o700, exist_ok=True)
    for f in (INBOX_FILE, OUTBOX_FILE):
        if not f.exists():
            f.touch(mode=0o600)


def _next_id() -> int:
    _ensure_dir()
    try:
        n = int(COUNTER_FILE.read_text().strip())
    except Exception:
        n = 0
    n += 1
    COUNTER_FILE.write_text(str(n))
    return n


def _append_line(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _tail_jsonl(path: Path, since: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("id", 0) > since:
                out.append(d)
    return out[-limit:]


def write_inbox(from_agent: str, message: str, node: str | None = None,
                meta: dict | None = None) -> dict[str, Any]:
    rec = {
        "id": _next_id(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "from": from_agent,
        "node": node or "smain",
        "message": message,
    }
    meta = dict(meta) if meta else {}
    # Auto-triage: жалобы → подтянуть контекст из request_log
    if _is_complaint(message) and meta.get("kind") not in {"huge_context"}:
        meta.setdefault("kind", "complaint")
        triage = _triage_from_db(from_agent)
        if triage:
            meta["triage"] = triage
    if meta:
        rec["meta"] = meta
    _append_line(INBOX_FILE, rec)
    return rec


def read_inbox(since: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    return _tail_jsonl(INBOX_FILE, since, limit)


def write_outbox(to_agent: str, message: str, in_reply_to: int | None = None,
                 delivered: bool | None = None, delivery_error: str | None = None) -> dict[str, Any]:
    rec = {
        "id": _next_id(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "to": to_agent,
        "in_reply_to": in_reply_to,
        "message": message,
        "delivered": delivered,
        "delivery_error": delivery_error,
    }
    _append_line(OUTBOX_FILE, rec)
    return rec


def read_outbox(since: int = 0, limit: int = 50, to: str | None = None) -> list[dict[str, Any]]:
    """Pull-модель reply-доставки: агент тянет свои ответы через to=<его id> + курсор since.
    Без to — весь outbox (как раньше)."""
    if not OUTBOX_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
    with OUTBOX_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("id", 0) <= since:
                continue
            if to is not None and d.get("to") != to:
                continue
            out.append(d)
    return out[-limit:]


def load_push_urls() -> dict[str, str]:
    """Read the agent_id → push_url map. Returns empty dict if file missing or malformed."""
    if not PUSH_URLS_FILE.exists():
        return {}
    try:
        data = json.loads(PUSH_URLS_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def set_push_url(agent: str, url: str | None) -> dict[str, str]:
    """Register (or remove if url is None/empty) a push_url for an agent. Atomic write."""
    if not agent or not isinstance(agent, str):
        raise ValueError("agent required")
    urls = load_push_urls()
    if url:
        if not _PUSH_URL_RE.match(url):
            raise ValueError(f"invalid push_url: {url!r}")
        urls[agent] = url
    else:
        urls.pop(agent, None)
    _ensure_dir()
    tmp = PUSH_URLS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(urls, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PUSH_URLS_FILE)
    return urls


async def deliver_reply(to_agent: str, message: str,
                        session: aiohttp.ClientSession | None = None,
                        record_id: int | None = None,
                        in_reply_to: int | None = None) -> tuple[bool, str | None]:
    """Push reply to the agent's registered HTTP endpoint if any.
    Falls back to the legacy in-Lineman forward if no push_url is registered,
    so agents that never registered keep working unchanged (pull via /outbox)."""
    push_url = load_push_urls().get(to_agent)
    own_session = session is None
    if session is None:
        session = aiohttp.ClientSession()
    try:
        if push_url:
            payload = {
                "from": "klod-access",
                "to": to_agent,
                "id": record_id,
                "in_reply_to": in_reply_to,
                "ts": datetime.now(timezone.utc).isoformat(),
                "message": message,
            }
            try:
                async with session.post(
                    push_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                    headers={"X-Klod-Channel": "push"},
                ) as resp:
                    ok = 200 <= resp.status < 300
                    return ok, None if ok else f"push HTTP {resp.status}"
            except Exception as e:
                return False, f"push exc: {e}"
        # Fallback: legacy in-Lineman forward
        from urllib.parse import urlencode
        qs = urlencode({"from": "klod-access", "message": message})
        url = f"http://127.0.0.1:9090/api/agent/{to_agent}/message?{qs}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                ok = 200 <= resp.status < 300
                return ok, None if ok else f"fwd HTTP {resp.status}"
        except Exception as e:
            return False, f"fwd exc: {e}"
    finally:
        if own_session:
            await session.close()
