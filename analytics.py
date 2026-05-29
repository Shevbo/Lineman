"""Analytics — parse Claude Code JSONL logs, compute token usage and cost.

Inspired by ccsage (ryoppippi/ccusage).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

CACHE_TTL = 300  # 5 minutes


class AnalyticsStore:
    """Parse and cache token usage from Claude Code JSONL logs."""

    def __init__(self, logs_path: str) -> None:
        self._logs_path = Path(logs_path).expanduser()
        self._cache: dict[str, Any] = {}
        self._cache_time: float = 0

    def _parse_logs(self) -> list[dict[str, Any]]:
        """Parse JSONL log files, extract usage entries."""
        entries: list[dict[str, Any]] = []
        if not self._logs_path.exists():
            logger.warning("analytics_logs_path_not_found", path=str(self._logs_path))
            return entries

        log_files = sorted(self._logs_path.rglob("*.jsonl"))
        for lf in log_files[-50:]:  # Last 50 files
            try:
                for line in lf.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "cost" in record or "usage" in record:
                        entries.append(record)
            except (OSError, PermissionError):
                continue

        return entries

    def _compute(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute analytics from parsed entries."""
        daily: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"tokens_in": 0, "tokens_out": 0, "cost": 0.0, "requests": 0}
        )
        monthly: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"tokens_in": 0, "tokens_out": 0, "cost": 0.0, "requests": 0}
        )
        by_model: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"tokens_in": 0, "tokens_out": 0, "cost": 0.0, "requests": 0}
        )
        total_tokens_in = 0
        total_tokens_out = 0
        total_cost = 0.0
        total_requests = 0

        for record in entries:
            ts = record.get("timestamp") or record.get("created_at", "")
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                dt = datetime.now(timezone.utc)

            usage = record.get("usage", record)
            tokens_in = (
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("tokens_in", 0)
            )
            tokens_out = (
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("tokens_out", 0)
            )
            cost = record.get("cost", 0.0)
            model = record.get("model", "unknown")

            day_key = dt.strftime("%Y-%m-%d")
            month_key = dt.strftime("%Y-%m")

            daily[day_key]["tokens_in"] += tokens_in
            daily[day_key]["tokens_out"] += tokens_out
            daily[day_key]["cost"] += cost
            daily[day_key]["requests"] += 1

            monthly[month_key]["tokens_in"] += tokens_in
            monthly[month_key]["tokens_out"] += tokens_out
            monthly[month_key]["cost"] += cost
            monthly[month_key]["requests"] += 1

            by_model[model]["tokens_in"] += tokens_in
            by_model[model]["tokens_out"] += tokens_out
            by_model[model]["cost"] += cost
            by_model[model]["requests"] += 1

            total_tokens_in += tokens_in
            total_tokens_out += tokens_out
            total_cost += cost
            total_requests += 1

        return {
            "summary": {
                "total_requests": total_requests,
                "total_tokens_in": total_tokens_in,
                "total_tokens_out": total_tokens_out,
                "total_cost": round(total_cost, 6),
            },
            "daily": dict(sorted(daily.items(), reverse=True)),
            "monthly": dict(sorted(monthly.items(), reverse=True)),
            "by_model": dict(sorted(by_model.items())),
        }

    def get_analytics(self, period: str = "day") -> dict[str, Any]:
        """Return cached analytics, refresh if TTL expired."""
        now = time.time()
        if now - self._cache_time < CACHE_TTL and self._cache:
            result = self._cache.get(period, self._cache.get("day", {}))
            return result

        entries = self._parse_logs()
        computed = self._compute(entries)
        self._cache = computed
        self._cache_time = now

        return computed

    def invalidate_cache(self) -> None:
        """Force re-parse on next request."""
        self._cache_time = 0
