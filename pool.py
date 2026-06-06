"""Proxy pool manager for Lineman.

Selects the best available proxy for a given target host based on
performance history. Pool config lives in config.json proxy_pool section.

Per-host circuit breaker: when a proxy returns too many errors for a specific
host within a sliding window, that proxy is bypassed for that host and the
request falls back to direct (or the next available proxy). A Telegram alert
is sent when a circuit trips. The circuit auto-resets after recovery_secs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_BORIS_CHAT_ID = "36910539"


@dataclass
class _ProxyStat:
    """Rolling in-memory stats for one proxy (global, not per-host)."""
    success: int = 0
    error: int = 0
    latency_sum_ms: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    last_used: float = 0.0
    last_error: float = 0.0

    @property
    def total(self) -> int:
        return self.success + self.error

    @property
    def error_rate(self) -> float:
        return self.error / self.total if self.total > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.latency_sum_ms / self.success if self.success > 0 else 9999.0


@dataclass
class _HostCircuit:
    """Per-(proxy, host) circuit breaker state."""
    # sliding window of (timestamp, success:bool) for recent attempts
    window: deque = field(default_factory=deque)
    tripped_at: float = 0.0   # 0 = closed (normal), >0 = tripped timestamp
    alerted: bool = False      # avoid repeat TG alerts


class ProxyPool:
    """Select and track proxies from config.json proxy_pool section.

    Config shape:
    {
        "proxies": [
            {"id": "px1", "name": "...", "url": "http://u:p@host:port",
             "priority": 1, "enabled": true}
        ],
        "routes": [
            {"hosts": ["*.googleapis.com", "api.telegram.org"],
             "proxies": ["px1", "iproyal"]},
            {"hosts": ["api.deepseek.com"],
             "proxies": []},
            {"hosts": ["*"], "proxies": []}
        ],
        "host_circuit_breaker": {
            "enabled": true,
            "window_secs": 600,
            "error_threshold": 10,
            "recovery_secs": 1800,
            "alert_cooldown_secs": 300
        }
    }

    A route with an empty proxies list means "go direct".
    Proxies within a route are tried in priority+performance order.
    If no route matches, go direct.

    Per-host circuit breaker: when a proxy accumulates >= error_threshold
    errors for a specific host within window_secs, it is bypassed for that
    host (fallback to direct or next proxy). After recovery_secs the circuit
    resets and the proxy is retried.
    """

    def __init__(self, pool_config: dict[str, Any]) -> None:
        self._proxies: dict[str, dict] = {}
        self._routes: list[dict] = []
        self._stats: dict[str, _ProxyStat] = {}
        # (proxy_id, host) -> circuit state
        self._host_circuits: dict[tuple[str, str], _HostCircuit] = defaultdict(_HostCircuit)
        self._session: aiohttp.ClientSession | None = None

        cb_cfg = pool_config.get("host_circuit_breaker", {})
        self._cb_enabled: bool = cb_cfg.get("enabled", True)
        self._cb_window: float = float(cb_cfg.get("window_secs", 600))
        self._cb_threshold: int = int(cb_cfg.get("error_threshold", 10))
        self._cb_recovery: float = float(cb_cfg.get("recovery_secs", 1800))
        # Кулдаун поднят 300→3600с: trip-алерты — низкоценный шум (auto-reset + fallback),
        # видны в daily-отчёте и дашборде. Не спамить Бориса в ТГ.
        self._cb_alert_cooldown: float = float(cb_cfg.get("alert_cooldown_secs", 3600))
        # Хосты, по которым НЕ слать TG-алерт о срыве циркуита (хронически срываются,
        # fallback=direct работает). api.telegram.org — геороут, прокси её не достают.
        self._cb_alert_suppress: set[str] = set(
            cb_cfg.get("alert_suppress_hosts", ["api.telegram.org"]))
        self._last_alert: dict[tuple[str, str], float] = {}

        for proxy in pool_config.get("proxies", []):
            pid = proxy["id"]
            self._proxies[pid] = proxy
            self._stats[pid] = _ProxyStat()

        self._routes = pool_config.get("routes", [])
        logger.info(
            "pool_initialized",
            proxies=len(self._proxies),
            routes=len(self._routes),
            host_circuit_breaker=self._cb_enabled,
        )

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(self, host: str) -> tuple[str | None, str]:
        """Return (proxy_url, proxy_id) for the given target host.

        proxy_url=None means direct connection. proxy_id is always a
        non-empty string (either a proxy id or "direct").
        """
        proxy_ids = self._route_for_host(host)
        if not proxy_ids:
            return None, "direct"

        candidates = [
            pid for pid in proxy_ids
            if pid in self._proxies
            and self._proxies[pid].get("enabled", True)
            and self._proxies[pid].get("url", "").strip()
        ]

        if not candidates:
            return None, "direct"

        # Filter out tripped circuits for this host
        if self._cb_enabled:
            open_candidates = [pid for pid in candidates if not self._is_tripped(pid, host)]
            if not open_candidates:
                logger.warning(
                    "pool_all_circuits_tripped",
                    host=host,
                    proxies=candidates,
                    fallback="direct",
                )
                return None, "direct"
            candidates = open_candidates

        best = min(candidates, key=self._score)
        url = self._proxies[best]["url"]
        logger.debug("pool_selected", host=host, proxy=best)
        return url, best

    def record(
        self,
        proxy_id: str,
        success: bool,
        latency_ms: float,
        host: str = "",
        bytes_in: int = 0,
        bytes_out: int = 0,
    ) -> None:
        """Update rolling stats after a proxied connection closes."""
        if proxy_id == "direct" or proxy_id not in self._stats:
            return
        stat = self._stats[proxy_id]
        now = time.monotonic()
        if success:
            stat.success += 1
            stat.latency_sum_ms += latency_ms
            stat.last_used = now
        else:
            stat.error += 1
            stat.last_error = now
        stat.bytes_in += bytes_in
        stat.bytes_out += bytes_out

        # Per-host circuit breaker tracking
        if self._cb_enabled and host:
            self._record_host(proxy_id, host, success, now)

    def get_stats(self) -> dict[str, Any]:
        """Return per-proxy stats for /api/pool/stats endpoint."""
        result: dict[str, Any] = {}
        for pid, proxy in self._proxies.items():
            stat = self._stats[pid]
            result[pid] = {
                "name": proxy.get("name", pid),
                "enabled": proxy.get("enabled", True),
                "url_masked": _mask_url(proxy.get("url", "")),
                "priority": proxy.get("priority", 99),
                "success": stat.success,
                "error": stat.error,
                "error_rate": round(stat.error_rate, 4),
                "avg_latency_ms": round(stat.avg_latency_ms, 1),
                "bytes_in": stat.bytes_in,
                "bytes_out": stat.bytes_out,
            }
        # Add tripped circuits
        tripped = [
            {"proxy": pid, "host": h, "since": round(time.monotonic() - c.tripped_at)}
            for (pid, h), c in self._host_circuits.items() if c.tripped_at > 0
        ]
        if tripped:
            result["__tripped_circuits"] = tripped
        return result

    def hitparade(self) -> list[dict[str, Any]]:
        """Return proxies ranked by performance (best first)."""
        stats = self.get_stats()
        ranked = sorted(
            stats.items(),
            key=lambda kv: (
                kv[1].get("error_rate", 0),
                kv[1].get("avg_latency_ms", 9999),
                kv[1].get("priority", 99),
            ),
        )
        return [{"id": pid, **data} for pid, data in ranked]

    # ------------------------------------------------------------------
    # Per-host circuit breaker
    # ------------------------------------------------------------------

    def _record_host(self, proxy_id: str, host: str, success: bool, now: float) -> None:
        key = (proxy_id, host)
        circuit = self._host_circuits[key]

        # Evict old entries outside window
        cutoff = now - self._cb_window
        while circuit.window and circuit.window[0][0] < cutoff:
            circuit.window.popleft()

        circuit.window.append((now, success))

        recent_errors = sum(1 for _, ok in circuit.window if not ok)

        # Trip the circuit if threshold exceeded and not already tripped
        if circuit.tripped_at == 0 and recent_errors >= self._cb_threshold:
            circuit.tripped_at = now
            circuit.alerted = False
            logger.warning(
                "host_circuit_tripped",
                proxy=proxy_id,
                host=host,
                recent_errors=recent_errors,
                window_secs=self._cb_window,
            )
            if self._session is not None:
                asyncio.create_task(self._alert_tripped(proxy_id, host, recent_errors))

        # Auto-reset: if circuit tripped and recovery period elapsed, reset
        elif circuit.tripped_at > 0 and (now - circuit.tripped_at) > self._cb_recovery:
            logger.info("host_circuit_reset", proxy=proxy_id, host=host)
            circuit.tripped_at = 0.0
            circuit.window.clear()
            circuit.alerted = False

    def _is_tripped(self, proxy_id: str, host: str) -> bool:
        circuit = self._host_circuits.get((proxy_id, host))
        return circuit is not None and circuit.tripped_at > 0

    def _is_alert_suppressed(self, host: str) -> bool:
        """Хосты, по которым не шлём TG-алерт о срыве циркуита (хронический шум)."""
        return host in self._cb_alert_suppress

    async def _alert_tripped(self, proxy_id: str, host: str, error_count: int) -> None:
        if self._is_alert_suppressed(host):
            return  # лог host_circuit_tripped уже записан; в ТГ не спамим
        key = (proxy_id, host)
        now = time.monotonic()
        if now - self._last_alert.get(key, 0.0) < self._cb_alert_cooldown:
            return
        self._last_alert[key] = now

        text = (
            f"[LINEMAN] Proxy circuit breaker tripped\n"
            f"proxy: {proxy_id}\n"
            f"host: {host}\n"
            f"errors: {error_count} in last {int(self._cb_window)}s\n"
            f"fallback: direct\n"
            f"auto-reset in: {int(self._cb_recovery/60)} min"
        )
        await _send_tg(text, self._session)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _route_for_host(self, host: str) -> list[str]:
        host_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
        try:
            host_ip = ipaddress.ip_address(host)
        except ValueError:
            pass

        for route in self._routes:
            for pattern in route.get("hosts", []):
                if host_ip is not None:
                    try:
                        if host_ip in ipaddress.ip_network(pattern, strict=False):
                            return route.get("proxies", [])
                        continue
                    except ValueError:
                        pass
                if pattern == "*" or fnmatch(host, pattern):
                    return route.get("proxies", [])
        return []

    def _score(self, proxy_id: str) -> tuple:
        stat = self._stats[proxy_id]
        priority = self._proxies[proxy_id].get("priority", 99)
        return (stat.error_rate, stat.avg_latency_ms, priority)


async def _send_tg(text: str, session: aiohttp.ClientSession | None) -> None:
    if session is None:
        return
    from pathlib import Path
    import json
    try:
        oc = json.loads(Path.home().joinpath(".openclaw/openclaw.json").read_text())
        token = (oc.get("channels", {}).get("telegram", {})
                 .get("accounts", {}).get("default", {}).get("botToken", ""))
        if not token:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with session.post(
            url,
            json={"chat_id": _BORIS_CHAT_ID, "text": text},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("pool_tg_alert_failed", status=resp.status)
    except Exception:
        logger.exception("pool_tg_alert_error")


def _mask_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.username:
            return urlunparse(p._replace(netloc=f"***:***@{p.hostname}:{p.port}"))
    except Exception:
        pass
    return url
