"""Metrics collection and aggregation.

Stores per-service metrics with rolling 24h history.
Thread-safe via asyncio.Lock, flushes to JSON on disk.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Snapshot:
    timestamp: float
    online: bool
    latency_ms: float
    phase: str  # "ping" | "probe"
    error: str | None = None


@dataclass
class ServiceMetrics:
    """Rolling metrics for a single service."""

    online: bool = False
    latency_ms: float = 0.0
    latency_avg_24h: float = 0.0
    uptime_pct: float = 100.0
    errors_24h: int = 0
    tokens_used: int = 0
    last_check: str = ""
    snapshots: deque[Snapshot] = field(default_factory=lambda: deque(maxlen=1440))
    gauges: dict[str, float] = field(default_factory=dict)


class MetricsStore:
    """In-memory metrics with JSON persistence."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._services: dict[str, ServiceMetrics] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            for svc_id, raw in data.items():
                sm = ServiceMetrics(
                    online=raw.get("online", False),
                    latency_ms=raw.get("latency_ms", 0.0),
                    latency_avg_24h=raw.get("latency_avg_24h", 0.0),
                    uptime_pct=raw.get("uptime_pct", 100.0),
                    errors_24h=raw.get("errors_24h", 0),
                    tokens_used=raw.get("tokens_used", 0),
                    last_check=raw.get("last_check", ""),
                )
                self._services[svc_id] = sm
        except Exception:
            logger.warning("metrics_load_failed", path=str(self._path))

    def flush(self) -> None:
        """Write current metrics to disk (non-async, called under lock)."""
        data: dict[str, Any] = {}
        for svc_id, sm in self._services.items():
            data[svc_id] = {
                "online": sm.online,
                "latency_ms": sm.latency_ms,
                "latency_avg_24h": sm.latency_avg_24h,
                "uptime_pct": sm.uptime_pct,
                "errors_24h": sm.errors_24h,
                "tokens_used": sm.tokens_used,
                "last_check": sm.last_check,
            }
            for key, val in sm.gauges.items():
                data[svc_id][key] = val

        try:
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.rename(self._path)
        except Exception:
            logger.exception("metrics_flush_failed")

    async def record(
        self,
        svc_id: str,
        online: bool,
        latency_ms: float,
        phase: str,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            sm = self._services.setdefault(svc_id, ServiceMetrics())
            sm.online = online
            sm.latency_ms = latency_ms
            hour_ago = time.time() - 3600

            if error:
                sm.errors_24h += 1

            sm.snapshots.append(
                Snapshot(
                    timestamp=time.time(),
                    online=online,
                    latency_ms=latency_ms,
                    phase=phase,
                    error=error,
                )
            )

            # Prune snapshots older than 24h
            cutoff = time.time() - 86400
            active = [s for s in sm.snapshots if s.timestamp > cutoff]
            sm.snapshots = deque(active, maxlen=1440)

            # Recompute aggregates
            if sm.snapshots:
                sm.latency_avg_24h = round(
                    sum(s.latency_ms for s in sm.snapshots) / len(sm.snapshots), 2
                )
                online_count = sum(1 for s in sm.snapshots if s.online)
                sm.uptime_pct = round(online_count / len(sm.snapshots) * 100, 2)

            # Count errors in last 24h
            sm.errors_24h = sum(
                1 for s in sm.snapshots if s.error and s.timestamp > cutoff
            )

    def set_gauge(self, svc_id: str, value: float) -> None:
        """Set a gauge metric (non-async for internal use)."""
        sm = self._services.setdefault(svc_id, ServiceMetrics())
        sm.gauges["gateway_log_errors"] = value

    def get_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a read-only snapshot of current metrics."""
        result: dict[str, dict[str, Any]] = {}
        for svc_id, sm in self._services.items():
            result[svc_id] = {
                "online": sm.online,
                "latency_ms": sm.latency_ms,
                "latency_avg_24h": sm.latency_avg_24h,
                "uptime_pct": sm.uptime_pct,
                "errors_24h": sm.errors_24h,
                "tokens_used": sm.tokens_used,
                "last_check": sm.last_check,
                "snapshot_count": len(sm.snapshots),
            }
        return result
