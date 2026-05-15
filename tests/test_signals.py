"""Tests for SignalQueue with prompt_snippet field."""
from __future__ import annotations
import time
import pytest


@pytest.mark.asyncio
async def test_signal_stores_prompt_snippet(signal_queue):
    await signal_queue.async_enqueue({
        "ts": time.time(),
        "from_node": "smain",
        "to_service": "deepseek",
        "type": "prompt",
        "tokens_in": 512,
        "tokens_out": 128,
        "latency_ms": 300,
        "status": "ok",
        "prompt_snippet": "Tell me about routing algorithms",
    })
    results = await signal_queue.recent(since_ts=0)
    assert len(results) == 1
    assert results[0]["prompt_snippet"] == "Tell me about routing algorithms"


@pytest.mark.asyncio
async def test_signal_prompt_snippet_none_ok(signal_queue):
    await signal_queue.async_enqueue({
        "ts": time.time(),
        "from_node": "smain",
        "to_service": "gemini",
        "type": "prompt",
        "status": "ok",
    })
    results = await signal_queue.recent(since_ts=0)
    assert len(results) == 1
    assert results[0].get("prompt_snippet") is None


@pytest.mark.asyncio
async def test_signal_prompt_snippet_300_chars(signal_queue):
    snippet = "x" * 300
    await signal_queue.async_enqueue({
        "ts": time.time(),
        "from_node": "smain",
        "to_service": "deepseek",
        "type": "prompt",
        "prompt_snippet": snippet,
    })
    results = await signal_queue.recent(since_ts=0)
    assert len(results) == 1
    assert results[0]["prompt_snippet"] == snippet
