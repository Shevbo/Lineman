"""Shared pytest fixtures for Lineman tests."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@pytest.fixture
def signal_queue(in_memory_conn):
    from signals import SignalQueue
    lock = asyncio.Lock()
    sq = SignalQueue(in_memory_conn, lock)
    sq.init_table()
    return sq


@pytest.fixture
def request_log_db(tmp_path):
    from db import RequestLogDB
    db = RequestLogDB(path=tmp_path / "test.db")
    db.init()
    return db
