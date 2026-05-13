"""Token harvester for Lineman observability.

Tails OpenClaw trajectory files (*.trajectory.jsonl) for model.completed
events and posts token usage data to the local request_log DB.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_POLL_INTERVAL = 30
_OPENCLAW_DIR = Path.home() / ".openclaw" / "agents"
_STATE_FILE = Path(__file__).resolve().parent / "harvester_state.json"


def _load_state() -> dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.rename(_STATE_FILE)
    except Exception as exc:
        logger.warning("harvester_state_save_failed", error=str(exc))


def _agent_id_from_path(path: str) -> str:
    parts = Path(path).parts
    try:
        idx = parts.index("agents")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "unknown"


def _session_id_from_path(path: str) -> str:
    stem = Path(path).stem
    return stem.replace(".trajectory", "")


def _local_hostname() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "unknown"


async def run_harvester(db: Any) -> None:
    """Background coroutine: poll trajectory files and log token usage."""
    if db is None:
        logger.warning("harvester_no_db")
        return

    state = _load_state()
    logger.info("harvester_started", openclaw_dir=str(_OPENCLAW_DIR))

    while True:
        try:
            await _scan_once(db, state)
        except Exception:
            logger.exception("harvester_scan_error")
        await asyncio.sleep(_POLL_INTERVAL)


async def _scan_once(db: Any, state: dict[str, Any]) -> None:
    pattern = str(_OPENCLAW_DIR / "**" / "*.trajectory.jsonl")
    files = glob.glob(pattern, recursive=True)

    new_events = 0
    for fpath in files:
        try:
            new_events += await _process_file(db, state, fpath)
        except Exception as exc:
            logger.debug("harvester_file_error", file=fpath, error=str(exc))

    if new_events:
        _save_state(state)
        logger.info("harvester_scan_done", new_events=new_events, files=len(files))


async def _process_file(db: Any, state: dict[str, Any], fpath: str) -> int:
    file_size = os.path.getsize(fpath)
    last_pos = state.get(fpath, 0)
    if file_size <= last_pos:
        return 0

    agent_id = _agent_id_from_path(fpath)
    session_id = _session_id_from_path(fpath)
    new_events = 0

    with open(fpath, "r", errors="replace") as fh:
        fh.seek(last_pos)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") != "model.completed":
                continue

            data = event.get("data", {})
            usage = data.get("usage", {})
            tokens_in = usage.get("input")
            tokens_out = usage.get("output")
            cache_read = usage.get("cacheRead", 0)

            if tokens_in is None and tokens_out is None:
                continue

            try:
                ts_ms = int(event.get("ts") or 0)
                ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OSError):
                ts_iso = datetime.now(timezone.utc).isoformat()

            row = {
                "timestamp": ts_iso,
                "source_host": _local_hostname(),
                "source_agent": agent_id,
                "session_id": session_id,
                "llm_provider": event.get("provider", ""),
                "llm_model": event.get("modelId", ""),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "route_applied": "openclaw_session",
                "status_code": 200,
                "cache_hit": 1 if cache_read else 0,
            }

            loop = asyncio.get_event_loop()
            loop.create_task(db.log_request(row))
            new_events += 1

        state[fpath] = fh.tell()

    return new_events
