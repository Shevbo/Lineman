"""Tests for Router routing decisions log."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router import Router, RouteContext

SAMPLE_CONFIG = {
    "default":     {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "think":       {"provider": "deepseek", "model": "deepseek-v4-pro"},
    "longContext": {"provider": "gemini",   "model": "gemini-3.1-pro-preview"},
    "webSearch":   {"provider": "gemini",   "model": "gemini-2.5-flash"},
    "background":  {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "longContextThreshold": 60000,
}


def test_decisions_log_records_detect_context():
    router = Router(SAMPLE_CONFIG)
    ctx = router.detect_context(b'{"messages":[{"role":"user","content":"Hello"}]}', {})
    decisions = router.recent_decisions()
    assert len(decisions) == 1
    assert decisions[0]["context"] == ctx.value
    assert decisions[0]["provider"] is not None
    assert "ts" in decisions[0]


def test_decisions_log_captures_think_keyword():
    router = Router(SAMPLE_CONFIG)
    router.detect_context(b'{"messages":[{"role":"user","content":"Enable thinking mode"}]}', {})
    dec = router.recent_decisions()[0]
    assert dec["context"] == "think"
    assert dec["snippet"] is not None


def test_decisions_log_max_50():
    router = Router(SAMPLE_CONFIG)
    for _ in range(60):
        router.detect_context(None, {})
    assert len(router.recent_decisions()) == 50


def test_decisions_log_header_override():
    router = Router(SAMPLE_CONFIG)
    router.detect_context(None, {"X-Lineman-Route": "webSearch"})
    dec = router.recent_decisions()[0]
    assert dec["context"] == "webSearch"
    assert dec["triggered_by"] == "header"


def test_decisions_log_auto_detected():
    router = Router(SAMPLE_CONFIG)
    # "background task" matches batch_keywords → BATCH context
    router.detect_context(b'{"messages":[{"role":"user","content":"background task"}]}', {})
    dec = router.recent_decisions()[0]
    assert dec["triggered_by"] == "body"
    assert dec["context"] == "batch"


def test_decisions_newest_first():
    router = Router(SAMPLE_CONFIG)
    router.detect_context(b'{"messages":[{"role":"user","content":"first"}]}', {})
    router.detect_context(b'{"messages":[{"role":"user","content":"thinking please"}]}', {})
    decisions = router.recent_decisions()
    assert decisions[0]["context"] == "think"   # newest first
    assert decisions[1]["context"] == "default"
