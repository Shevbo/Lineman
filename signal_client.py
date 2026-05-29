"""Lightweight signal emitter for OpenClaw agents.

Zero external dependencies. Fire-and-forget via background thread.

Usage:
    from signal_client import emit
    emit("main", "deepseek", "prompt", model="deepseek-v4-flash", tokens_in=1200)
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any

_DEFAULT_ENDPOINT = "http://127.0.0.1:9090/api/signal"


def emit(
    from_agent: str,
    to_service: str,
    sig_type: str,
    *,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    endpoint: str = _DEFAULT_ENDPOINT,
) -> None:
    """Fire-and-forget signal to Lineman dashboard.

    sig_type: prompt | response | tool_call | error | success | document | image | message
    """
    payload: dict[str, Any] = {
        "ts": time.time(),
        "from_agent": from_agent,
        "to_service": to_service,
        "type": sig_type,
        "status": status,
    }
    if model:
        payload["model"] = model
    if tokens_in:
        payload["tokens_in"] = tokens_in
    if tokens_out:
        payload["tokens_out"] = tokens_out
    if latency_ms:
        payload["latency_ms"] = latency_ms

    def _send() -> None:
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                endpoint,
                data=data,
                headers={"Content-Type": "application/json", "Content-Length": str(len(data))},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2):
                pass
        except Exception:
            pass  # fire-and-forget — never raise

    threading.Thread(target=_send, daemon=True).start()
