"""Двусторонний канал messaging для агента klod-access.

Inbox — это append-only JSONL (`~/klod-access/inbox.jsonl`). Каждая строка:
    {"id": N, "ts": "ISO", "from": "agent_id", "node": "smain", "message": "..."}

Outbox — replies от klod-access обратно отправителю, формат тот же файл
`outbox.jsonl`, плюс попытка прямой доставки через `/api/agent/{to}/message`.

Endpoints:
- POST /api/agent/klod-access/message?from=X[&node=Y]   body=text  → inbox.jsonl
- GET  /api/agent/klod-access/inbox?since=N&limit=K     → JSONL response
- POST /api/agent/klod-access/reply?to=X[&in_reply_to=N] body=text → outbox + forward
- GET  /api/agent/klod-access/outbox?since=N            → JSONL response
"""
from __future__ import annotations

import asyncio
import json
import os
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


def read_outbox(since: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    return _tail_jsonl(OUTBOX_FILE, since, limit)


async def deliver_reply(to_agent: str, message: str,
                        session: aiohttp.ClientSession | None = None) -> tuple[bool, str | None]:
    """Forward the reply through Lineman's own /api/agent/{to}/message endpoint."""
    from urllib.parse import urlencode
    qs = urlencode({"from": "klod-access", "message": message})
    url = f"http://127.0.0.1:9090/api/agent/{to_agent}/message?{qs}"
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            ok = 200 <= resp.status < 300
            err = None if ok else f"HTTP {resp.status}"
            return ok, err
    except Exception as e:
        return False, str(e)
    finally:
        if own_session:
            await session.close()
