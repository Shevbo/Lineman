"""Тесты для federation_inbox — файловый fallback push'а."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import federation_inbox as fi


@pytest.fixture
def tmp_inbox(tmp_path, monkeypatch):
    """Подменяет INBOX_ROOT и DELIVERY_LOG на tmp_path, чтобы тесты не трогали ~"""
    monkeypatch.setattr(fi, "INBOX_ROOT", tmp_path / "federation-inbox")
    monkeypatch.setattr(fi, "DELIVERY_LOG", tmp_path / "klod-access" / "delivery_log.jsonl")
    return tmp_path


def _read(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l]


def test_deliver_node_map_writes_inbox_and_log(tmp_inbox):
    r = fi.deliver_to_local_agent("nurse", "klod-access", "hello", in_node_map=True)
    assert r["status"] == "ok"
    assert r["via"] == "lineman-file-node-map"
    assert r["id"] == 1
    inbox = tmp_inbox / "federation-inbox" / "nurse" / "inbox.jsonl"
    log = tmp_inbox / "klod-access" / "delivery_log.jsonl"
    assert inbox.exists() and log.exists()
    rec = _read(inbox)[0]
    assert rec["from"] == "klod-access" and rec["to"] == "nurse" and rec["message"] == "hello"


def test_deliver_catchall(tmp_inbox):
    r = fi.deliver_to_local_agent("career-bot", "klod-access", "hi", in_node_map=False)
    assert r["status"] == "ok"
    assert r["via"] == "lineman-file-catchall"


def test_counter_increments(tmp_inbox):
    a = fi.deliver_to_local_agent("nurse", "klod", "1", in_node_map=True)
    b = fi.deliver_to_local_agent("nurse", "klod", "2", in_node_map=True)
    c = fi.deliver_to_local_agent("nurse", "klod", "3", in_node_map=True)
    assert (a["id"], b["id"], c["id"]) == (1, 2, 3)


def test_unsafe_agent_id_rejected(tmp_inbox):
    for bad in ("../etc/passwd", "..", ".", "a/b", "x\\y", "", " "):
        r = fi.deliver_to_local_agent(bad, "klod", "x", in_node_map=False)
        assert r["status"] == "error", f"expected reject for {bad!r}"
    # никакая директория не должна была создаться
    assert not (tmp_inbox / "federation-inbox").exists() or \
        list((tmp_inbox / "federation-inbox").iterdir()) == []


def test_empty_message_rejected(tmp_inbox):
    r = fi.deliver_to_local_agent("nurse", "klod", "", in_node_map=True)
    assert r["status"] == "error"
    r = fi.deliver_to_local_agent("nurse", "klod", "   ", in_node_map=True)
    assert r["status"] == "error"


def test_read_inbox_with_cursor(tmp_inbox):
    fi.deliver_to_local_agent("nurse", "klod", "m1", in_node_map=True)
    fi.deliver_to_local_agent("nurse", "klod", "m2", in_node_map=True)
    fi.deliver_to_local_agent("nurse", "klod", "m3", in_node_map=True)
    all3 = fi.read_inbox("nurse", since_id=0)
    assert len(all3) == 3
    after_first = fi.read_inbox("nurse", since_id=1)
    assert len(after_first) == 2 and after_first[0]["message"] == "m2"
    none_ = fi.read_inbox("nurse", since_id=99)
    assert none_ == []


def test_unsafe_sender_normalizes(tmp_inbox):
    # plохой from_id не должен ронять доставку; он подменяется на (unknown)
    r = fi.deliver_to_local_agent("nurse", "../bad/from", "x", in_node_map=True)
    assert r["status"] == "ok"
    assert r["from"] == "(unknown)"
