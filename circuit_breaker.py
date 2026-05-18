"""Sliding-window circuit breaker for reverse proxy calls.

Tracks calls per source_ip over a rolling window.
Returns 429 if call rate or token volume exceeds configured limits.
Sends a Telegram alert (with cooldown) when the breaker trips.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_BORIS_CHAT_ID = "36910539"
_OC_PATH = Path.home() / ".openclaw" / "openclaw.json"

_DEFAULT_WINDOW_SECS = 60
_DEFAULT_MAX_CALLS = 20
_DEFAULT_MAX_BYTES_CALL = 400_000   # ~100k tokens at 4 bytes/tok
_DEFAULT_MAX_BYTES_WINDOW = 2_000_000  # ~500k tokens rolling window
_DEFAULT_ALERT_COOLDOWN = 120.0


class CircuitBreaker:
    """Per-source_ip sliding window rate limiter."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = (config or {}).get("circuit_breaker", {})
        self._window = float(cfg.get("window_secs", _DEFAULT_WINDOW_SECS))
        self._max_calls = int(cfg.get("max_calls", _DEFAULT_MAX_CALLS))
        self._max_bytes_call = int(cfg.get("max_bytes_per_call", _DEFAULT_MAX_BYTES_CALL))
        self._max_bytes_window = int(cfg.get("max_bytes_window", _DEFAULT_MAX_BYTES_WINDOW))
        self._alert_cooldown = float(cfg.get("alert_cooldown_secs", _DEFAULT_ALERT_COOLDOWN))
        self._enabled = bool(cfg.get("enabled", True))

        # source_ip -> deque[(ts_float, body_bytes_int)]
        self._windows: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self._last_alert: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def _evict(self, ip: str, now: float) -> None:
        window = self._windows[ip]
        cutoff = now - self._window
        while window and window[0][0] < cutoff:
            window.popleft()

    def check(
        self,
        source_ip: str,
        body_len: int,
        provider: str,
        model: str,
        agent_name: str | None,
    ) -> tuple[bool, str]:
        """Return (blocked, reason). Call BEFORE the upstream request.

        Caller is responsible for scheduling self.alert() as an asyncio task
        when blocked=True.
        """
        if not self._enabled:
            return False, ""

        now = time.time()
        self._evict(source_ip, now)
        window = self._windows[source_ip]

        # Single-call body size guard
        if body_len > self._max_bytes_call:
            return True, (
                f"body {body_len:,}B > {self._max_bytes_call:,}B limit "
                f"(~{body_len//4:,} tok)"
            )

        # Rolling call frequency
        if len(window) >= self._max_calls:
            return True, (
                f"call rate {len(window)}/{self._window:.0f}s >= limit {self._max_calls}"
            )

        # Rolling body volume
        total_bytes = sum(b for _, b in window)
        if total_bytes > self._max_bytes_window:
            return True, (
                f"window body {total_bytes:,}B > {self._max_bytes_window:,}B limit "
                f"(~{total_bytes//4:,} tok)"
            )

        return False, ""

    async def alert(
        self,
        source_ip: str,
        reason: str,
        provider: str,
        model: str,
        agent_name: str | None,
    ) -> None:
        """Send alert (with cooldown). Caller: asyncio.create_task(breaker.alert(...))."""
        await self._alert(source_ip, reason, provider, model, agent_name)

    def record(self, source_ip: str, body_len: int) -> None:
        """Record a completed call (call AFTER response)."""
        now = time.time()
        self._windows[source_ip].append((now, body_len))

    async def _alert(
        self,
        source_ip: str,
        reason: str,
        provider: str,
        model: str,
        agent_name: str | None,
    ) -> None:
        now = time.time()
        if now - self._last_alert.get(source_ip, 0.0) < self._alert_cooldown:
            return
        self._last_alert[source_ip] = now

        text = (
            f"[LINEMAN] CIRCUIT BREAKER TRIPPED\n"
            f"source: {source_ip}\n"
            f"agent: {agent_name or 'unknown'}\n"
            f"provider: {provider} / {model or '?'}\n"
            f"reason: {reason}"
        )
        logger.warning(
            "circuit_breaker_tripped",
            source_ip=source_ip,
            agent=agent_name,
            provider=provider,
            model=model,
            reason=reason,
        )
        await _send_tg(text, self._session)


async def _send_tg(text: str, session: aiohttp.ClientSession | None) -> None:
    if session is None:
        return
    token = _load_tg_token()
    if not token:
        logger.warning("cb_no_tg_token")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with session.post(
            url,
            json={"chat_id": _BORIS_CHAT_ID, "text": text},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("cb_tg_failed", status=resp.status)
    except Exception:
        logger.exception("cb_tg_error")


def _load_tg_token() -> str:
    try:
        with open(_OC_PATH) as f:
            oc = json.load(f)
        return (
            oc.get("channels", {})
            .get("telegram", {})
            .get("accounts", {})
            .get("default", {})
            .get("botToken", "")
        )
    except Exception:
        return ""
