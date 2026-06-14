"""SQLite request log database for Lineman observability."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DB_PATH = Path(__file__).resolve().parent / "lineman.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS request_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    source_host         TEXT,
    source_agent        TEXT,
    session_id          TEXT,
    llm_provider        TEXT,
    llm_model           TEXT,
    target_url          TEXT,
    target_host         TEXT,
    request_body        TEXT,
    request_size        INTEGER,
    has_attachment      INTEGER DEFAULT 0,
    attachment_info     TEXT,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    route_applied       TEXT,
    optimization        TEXT,
    cache_hit           INTEGER DEFAULT 0,
    latency_ms          INTEGER,
    bytes_in            INTEGER,
    bytes_out           INTEGER,
    status_code         INTEGER,
    error               TEXT,
    token_economy_pct   REAL DEFAULT 0.0,
    traffic_economy_pct REAL DEFAULT 0.0
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_req_time    ON request_log(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_req_host    ON request_log(source_host);",
    "CREATE INDEX IF NOT EXISTS idx_req_agent   ON request_log(source_agent);",
    "CREATE INDEX IF NOT EXISTS idx_req_model   ON request_log(llm_provider, llm_model);",
]

# WireGuard IP → human hostname
WG_HOST_MAP: dict[str, str] = {
    "10.66.0.1": "smain",
    "10.66.0.2": "pi",
    "10.66.0.3": "cloud",
    "10.66.0.4": "sdev",
    "10.66.0.5": "pi2",
    "10.66.0.6": "vibe",
    "10.66.0.7": "hoster",
}

# Target host → LLM provider name
LLM_PROVIDER_MAP: dict[str, str] = {
    "api.deepseek.com":                                   "deepseek",
    "generativelanguage.googleapis.com":                  "gemini",
    "gemini-proxy-worker.bshevelev75.workers.dev":        "gemini",
}


def _local_wg_hostname() -> str:
    """Return human hostname for the local WireGuard node."""
    try:
        import subprocess
        out = subprocess.run(
            ["ip", "-4", "addr", "show", "wg0"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        for part in out.split():
            wg_ip = part.split("/")[0]
            if wg_ip in WG_HOST_MAP:
                return WG_HOST_MAP[wg_ip]
    except Exception:
        pass
    import socket
    return socket.gethostname()


def source_host_from_ip(ip: str) -> str:
    """Map WireGuard IP to host alias. Falls back to the raw IP."""
    if ip in ("127.0.0.1", "::1", ""):
        return _local_wg_hostname()
    return WG_HOST_MAP.get(ip, ip)


def llm_provider_from_host(host: str) -> str | None:
    """Infer LLM provider from the target host."""
    for key, provider in LLM_PROVIDER_MAP.items():
        if key in host:
            return provider
    return None


class RequestLogDB:
    """Thread-safe (asyncio.Lock) SQLite wrapper for request_log."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def init(self) -> None:
        """Open DB and create schema. Call once at startup (sync)."""
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            self._conn.execute(idx_sql)
        for col, col_type in [
            ("compression_applied", "INTEGER DEFAULT 0"),
            ("tail_tokens_before", "INTEGER"),
            ("tail_tokens_after", "INTEGER"),
            ("request_headers_masked", "TEXT"),
        ]:
            try:
                self._conn.execute(
                    f"ALTER TABLE request_log ADD COLUMN {col} {col_type}"
                )
            except sqlite3.OperationalError:
                pass
        # Lazy queue table — not-urgent jobs offloaded to local LLMs (LM Studio,
        # Ollama-hoster) с fallback на cloud. См. docs/LAZY_QUEUE.md.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS lazy_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_created      TEXT NOT NULL,
                ts_started      TEXT,
                ts_done         TEXT,
                from_agent      TEXT,
                from_node       TEXT,
                kind            TEXT NOT NULL,
                system_prompt   TEXT,
                user_prompt     TEXT NOT NULL,
                max_tokens      INTEGER DEFAULT 600,
                temperature     REAL DEFAULT 0.3,
                priority        INTEGER DEFAULT 3,
                deadline_ts     REAL,
                status          TEXT NOT NULL DEFAULT 'queued',
                model_used      TEXT,
                backend_used    TEXT,
                output          TEXT,
                tokens_in       INTEGER,
                tokens_out      INTEGER,
                latency_ms      INTEGER,
                retries         INTEGER DEFAULT 0,
                error           TEXT
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lazy_status_pri "
            "ON lazy_jobs(status, priority, ts_created)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lazy_agent ON lazy_jobs(from_agent, ts_created)"
        )
        # New column for cost accounting (saved $ vs paid-equivalent baseline)
        try:
            self._conn.execute("ALTER TABLE lazy_jobs ADD COLUMN saved_usd REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        self._conn.commit()
        logger.info("db_initialized", path=str(self._path))

    def make_signal_queue(self):
        """Create a SignalQueue sharing this connection and lock."""
        from signals import SignalQueue
        sq = SignalQueue(self._conn, self._lock)
        sq.init_table()
        return sq

    async def log_request(self, row: dict[str, Any]) -> int | None:
        """Insert a row into request_log. Returns row id or None on error."""
        if not self._conn:
            return None
        row.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        cols = list(row.keys())
        vals = [row[c] for c in cols]
        sql = (
            f"INSERT INTO request_log ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})"
        )
        async with self._lock:
            try:
                cur = self._conn.execute(sql, vals)
                return cur.lastrowid
            except Exception as exc:
                logger.error("db_insert_error", error=str(exc))
                return None

    async def query_logs(
        self,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        source_host: str | None = None,
        source_agent: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
    ) -> list[dict[str, Any]]:
        """SELECT from request_log with optional filters."""
        if not self._conn:
            return []
        where: list[str] = []
        params: list[Any] = []
        if since:
            where.append("timestamp >= ?"); params.append(since)
        if until:
            where.append("timestamp <= ?"); params.append(until)
        if source_host:
            where.append("source_host = ?"); params.append(source_host)
        if source_agent:
            where.append("source_agent = ?"); params.append(source_agent)
        if llm_provider:
            where.append("llm_provider = ?"); params.append(llm_provider)
        if llm_model:
            where.append("llm_model = ?"); params.append(llm_model)
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(min(limit, 1000))
        sql = (
            f"SELECT * FROM request_log {where_clause}"
            f" ORDER BY timestamp DESC LIMIT ?"
        )
        async with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:
                logger.error("db_query_error", error=str(exc))
                return []

    async def get_stats(self) -> dict[str, Any]:
        """Aggregate stats for /api/log/stats."""
        if not self._conn:
            return {}
        async with self._lock:
            try:
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM request_log"
                ).fetchone()[0]
                by_provider = dict(self._conn.execute(
                    "SELECT llm_provider, COUNT(*) FROM request_log"
                    " WHERE llm_provider IS NOT NULL GROUP BY llm_provider"
                ).fetchall())
                by_host = dict(self._conn.execute(
                    "SELECT source_host, COUNT(*) FROM request_log"
                    " WHERE source_host IS NOT NULL GROUP BY source_host"
                ).fetchall())
                avg_lat = self._conn.execute(
                    "SELECT AVG(latency_ms) FROM request_log WHERE latency_ms IS NOT NULL"
                ).fetchone()[0]
                errors = self._conn.execute(
                    "SELECT COUNT(*) FROM request_log WHERE error IS NOT NULL"
                ).fetchone()[0]
                total_bytes_in = self._conn.execute(
                    "SELECT SUM(bytes_in) FROM request_log WHERE bytes_in IS NOT NULL"
                ).fetchone()[0] or 0
                total_bytes_out = self._conn.execute(
                    "SELECT SUM(bytes_out) FROM request_log WHERE bytes_out IS NOT NULL"
                ).fetchone()[0] or 0
                return {
                    "total_requests": total,
                    "by_provider": by_provider,
                    "by_source_host": by_host,
                    "avg_latency_ms": round(avg_lat or 0, 1),
                    "errors": errors,
                    "total_bytes_in": total_bytes_in,
                    "total_bytes_out": total_bytes_out,
                }
            except Exception as exc:
                logger.error("db_stats_error", error=str(exc))
                return {}

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
