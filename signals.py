"""Signal queue — SQLite-backed, shared DB connection from RequestLogDB."""
from __future__ import annotations

import sqlite3
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    from_agent     TEXT,
    from_node      TEXT,
    to_service     TEXT,
    type           TEXT,
    model          TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    latency_ms     INTEGER,
    status         TEXT,
    prompt_snippet TEXT
);
"""

_RETENTION_HOURS = 24


class SignalQueue:
    """SQLite signal queue sharing the DB connection and asyncio lock."""

    def __init__(self, conn: sqlite3.Connection, lock: Any) -> None:
        self._conn = conn
        self._lock = lock  # asyncio.Lock shared with RequestLogDB

    def init_table(self) -> None:
        """Create table + indexes. Call once at startup (sync)."""
        self._conn.execute(_CREATE_SIGNALS)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sig_ts    ON signals(ts);"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sig_agent ON signals(from_agent);"
        )
        # Migration: add prompt_snippet if upgrading from older schema
        try:
            self._conn.execute("ALTER TABLE signals ADD COLUMN prompt_snippet TEXT;")
        except Exception:
            pass  # column already exists
        self._conn.commit()
        logger.info("signals_table_initialized")

    _KNOWN_COLS = frozenset({
        "ts", "from_agent", "from_node", "to_service",
        "type", "model", "tokens_in", "tokens_out", "latency_ms", "status",
        "prompt_snippet",
    })

    async def async_enqueue(self, sig: dict[str, Any]) -> None:
        """Async-safe insert; acquires shared lock then runs sync SQLite."""
        sig.setdefault("ts", time.time())
        sig = {k: v for k, v in sig.items() if k in self._KNOWN_COLS}
        cols = list(sig.keys())
        vals = [sig[c] for c in cols]
        sql = (
            f"INSERT INTO signals ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})"
        )
        async with self._lock:
            try:
                self._conn.execute(sql, vals)
                cutoff = time.time() - _RETENTION_HOURS * 3600
                self._conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
            except Exception as exc:
                logger.error("signal_enqueue_error", error=str(exc))

    async def recent(
        self,
        since_ts: float = 0.0,
        limit: int = 100,
        from_node: str | None = None,
        from_agent: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent signals ordered by ts DESC."""
        where: list[str] = ["ts >= ?"]
        params: list[Any] = [since_ts]
        if from_node:
            where.append("from_node = ?")
            params.append(from_node)
        if from_agent:
            where.append("from_agent = ?")
            params.append(from_agent)
        params.append(min(limit, 500))
        sql = (
            f"SELECT * FROM signals WHERE {' AND '.join(where)}"
            f" ORDER BY ts DESC LIMIT ?"
        )
        async with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:
                logger.error("signal_recent_error", error=str(exc))
                return []

    async def agent_history(
        self, agent_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Signals sent or received by a specific agent."""
        async with self._lock:
            try:
                sql = (
                    "SELECT * FROM signals"
                    " WHERE from_agent = ? OR to_service = ?"
                    " ORDER BY ts DESC LIMIT ?"
                )
                cur = self._conn.execute(
                    sql, (agent_id, agent_id, min(limit, 100))
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:
                logger.error("signal_agent_history_error", error=str(exc))
                return []

    async def today_stats(self) -> dict[str, Any]:
        """Aggregate today's signal stats for /api/nodes."""
        import datetime as _dt
        today_start = _dt.datetime.now(_dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        async with self._lock:
            try:
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM signals WHERE ts >= ?", (today_start,)
                ).fetchone()[0]
                tin = self._conn.execute(
                    "SELECT SUM(tokens_in) FROM signals WHERE ts >= ? AND tokens_in IS NOT NULL",
                    (today_start,),
                ).fetchone()[0] or 0
                tout = self._conn.execute(
                    "SELECT SUM(tokens_out) FROM signals WHERE ts >= ? AND tokens_out IS NOT NULL",
                    (today_start,),
                ).fetchone()[0] or 0
                return {
                    "calls_today": total,
                    "tokens_in_today": tin,
                    "tokens_out_today": tout,
                }
            except Exception as exc:
                logger.error("signal_today_stats_error", error=str(exc))
                return {"calls_today": 0, "tokens_in_today": 0, "tokens_out_today": 0}
