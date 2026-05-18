"""Request deduplication cache + retry analyzer for LLM proxy.

For each request:
  1. Hash (model + messages) to a canonical key.
  2. If an identical request was answered in the last TTL seconds, return the
     cached response — no upstream call, zero token spend.
  3. Track per-source_ip hash frequency; if the same hash fires > threshold
     times in the window, emit a retry signal and TG alert.

Inserted in reverse_proxy.handle_reverse_proxy BEFORE the upstream call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_OC_PATH = Path.home() / ".openclaw" / "openclaw.json"
_BORIS_CHAT_ID = "36910539"

_DEFAULT_TTL        = 30      # seconds: cached response lifetime
_DEFAULT_MAX_CACHE  = 200     # max distinct keys in cache
_DEFAULT_WIN_SECS   = 60      # retry-detection window
_DEFAULT_MAX_RETRIES = 3      # same-hash calls in window before alert
_DEFAULT_ALERT_COOL = 300     # seconds between TG alerts per key


class DedupCache:
    """In-memory LRU response cache + retry frequency tracker."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = (config or {}).get("dedup_cache", {})
        self._ttl        = int(cfg.get("ttl_secs",        _DEFAULT_TTL))
        self._max_cache  = int(cfg.get("max_entries",     _DEFAULT_MAX_CACHE))
        self._win        = int(cfg.get("window_secs",     _DEFAULT_WIN_SECS))
        self._max_retries= int(cfg.get("max_retries",     _DEFAULT_MAX_RETRIES))
        self._alert_cool = int(cfg.get("alert_cooldown",  _DEFAULT_ALERT_COOL))
        self._enabled    = bool(cfg.get("enabled", True))

        # {req_hash: (ts_stored, status_code, body_bytes)}
        self._cache: dict[str, tuple[float, int, bytes]] = {}
        # {req_hash: deque[(ts, source_ip)]}
        self._freq: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        # {req_hash: ts_last_alert}
        self._alerted: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    # ── public API ──────────────────────────────────────────────────────────

    def req_hash(self, body: bytes) -> str | None:
        """Return a dedup key for the request body, or None if unparseable."""
        if not body:
            return None
        try:
            data = json.loads(body)
            model = data.get("model", "")
            msgs  = data.get("messages") or data.get("contents")
            if not msgs:
                return None
            canonical = json.dumps({"m": model, "msgs": msgs}, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(canonical.encode()).hexdigest()[:16]
        except Exception:
            return None

    def get_cached(self, key: str) -> tuple[int, bytes] | None:
        """Return (status_code, body) if a valid cache entry exists."""
        if not self._enabled or key not in self._cache:
            return None
        ts, status, body = self._cache[key]
        if time.time() - ts > self._ttl:
            del self._cache[key]
            return None
        return status, body

    def store(self, key: str, status_code: int, body: bytes) -> None:
        """Cache a successful response."""
        if not self._enabled or status_code not in (200, 201):
            return
        if len(self._cache) >= self._max_cache:
            # evict oldest
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[key] = (time.time(), status_code, body)

    def record_and_check(
        self,
        key: str,
        source_ip: str,
        provider: str,
        model: str,
        agent_name: str | None,
    ) -> tuple[bool, int]:
        """Record a call attempt. Return (is_retry, retry_count)."""
        if not self._enabled:
            return False, 0
        now = time.time()
        dq  = self._freq[key]
        cutoff = now - self._win
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        dq.append((now, source_ip))
        count = len(dq)
        is_retry = count > self._max_retries
        if is_retry:
            last = self._alerted.get(key, 0.0)
            if now - last >= self._alert_cool:
                self._alerted[key] = now
                asyncio.create_task(
                    self._alert(key, count, source_ip, provider, model, agent_name)
                )
        return is_retry, count

    # ── internals ───────────────────────────────────────────────────────────

    async def _alert(
        self,
        key: str,
        count: int,
        source_ip: str,
        provider: str,
        model: str,
        agent_name: str | None,
    ) -> None:
        logger.warning(
            "dedup_retry_detected",
            key=key, count=count, source_ip=source_ip,
            provider=provider, model=model, agent=agent_name,
        )
        text = (
            f"[LINEMAN] RETRY LOOP DETECTED\n"
            f"hash: {key}\n"
            f"count: {count}x in {self._win}s\n"
            f"source: {source_ip}\n"
            f"agent: {agent_name or 'unknown'}\n"
            f"provider: {provider} / {model or '?'}\n"
            f"action: dedup cache returning cached response"
        )
        await _send_tg(text, self._session)

    def stats(self) -> dict[str, Any]:
        return {
            "cache_entries": len(self._cache),
            "tracked_keys": len(self._freq),
        }


async def _send_tg(text: str, session: aiohttp.ClientSession | None) -> None:
    if session is None:
        return
    token = _load_tg_token()
    if not token:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with session.post(
            url,
            json={"chat_id": _BORIS_CHAT_ID, "text": text},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("dedup_tg_failed", status=resp.status)
    except Exception:
        logger.exception("dedup_tg_error")


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
