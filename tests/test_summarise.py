"""Tests for summarise_addendums middleware."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# _count_message_tokens
# ---------------------------------------------------------------------------

def test_count_string_content():
    from reverse_proxy import _count_message_tokens
    assert _count_message_tokens("A" * 400) == 100


def test_count_list_content_text_blocks_only():
    from reverse_proxy import _count_message_tokens
    content = [
        {"type": "text", "text": "A" * 200},
        {"type": "image", "source": {}},
        {"type": "text", "text": "B" * 200},
    ]
    assert _count_message_tokens(content) == 100


def test_count_empty_string():
    from reverse_proxy import _count_message_tokens
    assert _count_message_tokens("") == 0


def test_count_empty_list():
    from reverse_proxy import _count_message_tokens
    assert _count_message_tokens([]) == 0


def test_count_unknown_type_returns_zero():
    from reverse_proxy import _count_message_tokens
    assert _count_message_tokens(12345) == 0


# ---------------------------------------------------------------------------
# summarise_addendums
# ---------------------------------------------------------------------------

def _body(*messages) -> bytes:
    return json.dumps({"model": "deepseek-chat", "messages": list(messages)}).encode()


@pytest.mark.asyncio
async def test_skip_fewer_than_4_messages():
    from reverse_proxy import summarise_addendums
    body = _body(
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    )
    new_body, before, after = await summarise_addendums(body)
    assert new_body == body
    assert before == after == 0


@pytest.mark.asyncio
async def test_skip_when_tail_within_threshold():
    from reverse_proxy import summarise_addendums
    # system=1000 chars (250 tokens), last_user=400 chars (100 tokens) → useful=350
    # tail=assistant 200 chars (50 tokens) → 50 < 0.3*350 = 105 → skip
    body = _body(
        {"role": "system", "content": "S" * 1000},
        {"role": "user", "content": "Q1" * 100},
        {"role": "assistant", "content": "A1" * 50},
        {"role": "user", "content": "Q2" * 100},
    )
    new_body, before, after = await summarise_addendums(body)
    assert before == after
    assert new_body == body


@pytest.mark.asyncio
async def test_compress_when_tail_exceeds_threshold():
    from reverse_proxy import summarise_addendums
    tail_content = "T" * 400
    body = _body(
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": tail_content},
        {"role": "assistant", "content": tail_content},
        {"role": "user", "content": tail_content},
        {"role": "assistant", "content": tail_content},
        {"role": "user", "content": "final question"},
    )
    with patch("reverse_proxy._call_summarizer", new=AsyncMock(return_value="- point1\n- point2")):
        new_body, before, after = await summarise_addendums(body)

    assert before > after
    data = json.loads(new_body)
    msgs = data["messages"]
    assert msgs[0]["role"] == "system"
    assert "[Context summary]" in msgs[1]["content"]
    assert msgs[-1]["content"] == "final question"
    assert len(msgs) == 3


@pytest.mark.asyncio
async def test_fallback_unchanged_when_summarizer_fails():
    from reverse_proxy import summarise_addendums
    tail_content = "T" * 400
    body = _body(
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": tail_content},
        {"role": "assistant", "content": tail_content},
        {"role": "user", "content": tail_content},
        {"role": "assistant", "content": tail_content},
        {"role": "user", "content": "final"},
    )
    with patch("reverse_proxy._call_summarizer", new=AsyncMock(return_value=None)):
        new_body, before, after = await summarise_addendums(body)

    assert new_body == body
    assert before == after


@pytest.mark.asyncio
async def test_skip_non_messages_request():
    from reverse_proxy import summarise_addendums
    body = json.dumps({"model": "text-embedding-ada-002", "input": "Hello"}).encode()
    new_body, before, after = await summarise_addendums(body)
    assert new_body == body
    assert before == 0 and after == 0


@pytest.mark.asyncio
async def test_skip_invalid_json():
    from reverse_proxy import summarise_addendums
    new_body, before, after = await summarise_addendums(b"not json")
    assert before == 0 and after == 0


# ---------------------------------------------------------------------------
# _call_summarizer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_summarizer_no_api_key_returns_none(monkeypatch):
    from reverse_proxy import _call_summarizer
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = await _call_summarizer([{"role": "user", "content": "hi"}])
    assert result is None


@pytest.mark.asyncio
async def test_call_summarizer_returns_content_on_200(monkeypatch):
    from reverse_proxy import _call_summarizer
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LINEMAN_IPROYAL_URL", "")

    resp_data = {"choices": [{"message": {"content": "- bullet1\n- bullet2"}}]}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=resp_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await _call_summarizer([{"role": "user", "content": "context"}])

    assert result == "- bullet1\n- bullet2"


@pytest.mark.asyncio
async def test_call_summarizer_returns_none_on_non_200(monkeypatch):
    from reverse_proxy import _call_summarizer
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LINEMAN_IPROYAL_URL", "")

    mock_response = AsyncMock()
    mock_response.status = 500
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await _call_summarizer([{"role": "user", "content": "context"}])

    assert result is None


@pytest.mark.asyncio
async def test_call_summarizer_returns_none_on_exception(monkeypatch):
    from reverse_proxy import _call_summarizer
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LINEMAN_IPROYAL_URL", "")

    with patch("aiohttp.ClientSession", side_effect=Exception("network error")):
        result = await _call_summarizer([{"role": "user", "content": "context"}])

    assert result is None
