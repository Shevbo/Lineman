"""Proxy pool manager for Lineman.

Selects the best available proxy for a given target host based on
performance history. Pool config lives in config.json proxy_pool section.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse, urlunparse

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class _ProxyStat:
    """Rolling in-memory stats for one proxy."""
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
        ]
    }

    A route with an empty proxies list means "go direct".
    Proxies within a route are tried in priority+performance order.
    If no route matches, go direct.
    """

    def __init__(self, pool_config: dict[str, Any]) -> None:
        self._proxies: dict[str, dict] = {}
        self._routes: list[dict] = []
        self._stats: dict[str, _ProxyStat] = {}

        for proxy in pool_config.get("proxies", []):
            pid = proxy["id"]
            self._proxies[pid] = proxy
            self._stats[pid] = _ProxyStat()

        self._routes = pool_config.get("routes", [])
        logger.info(
            "pool_initialized",
            proxies=len(self._proxies),
            routes=len(self._routes),
        )

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

        best = min(candidates, key=self._score)
        url = self._proxies[best]["url"]
        logger.debug("pool_selected", host=host, proxy=best)
        return url, best

    def record(
        self,
        proxy_id: str,
        success: bool,
        latency_ms: float,
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
        return result

    def hitparade(self) -> list[dict[str, Any]]:
        """Return proxies ranked by performance (best first)."""
        stats = self.get_stats()
        ranked = sorted(
            stats.items(),
            key=lambda kv: (
                kv[1]["error_rate"],
                kv[1]["avg_latency_ms"],
                kv[1]["priority"],
            ),
        )
        return [{"id": pid, **data} for pid, data in ranked]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _route_for_host(self, host: str) -> list[str]:
        for route in self._routes:
            for pattern in route.get("hosts", []):
                if pattern == "*" or fnmatch(host, pattern):
                    return route.get("proxies", [])
        return []

    def _score(self, proxy_id: str) -> tuple:
        stat = self._stats[proxy_id]
        priority = self._proxies[proxy_id].get("priority", 99)
        return (stat.error_rate, stat.avg_latency_ms, priority)


def _mask_url(url: str) -> str:
    """Mask credentials in a proxy URL for safe display."""
    try:
        p = urlparse(url)
        if p.username:
            return urlunparse(p._replace(netloc=f"***:***@{p.hostname}:{p.port}"))
    except Exception:
        pass
    return url
