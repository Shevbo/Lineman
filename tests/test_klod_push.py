"""Tests for klod_inbox push_url register + deliver_reply push/fallback."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import klod_inbox


@pytest.fixture
def isolated_klod_dir(tmp_path, monkeypatch):
    """Redirect klod-access dir to a tmp path so tests don't touch the real inbox."""
    monkeypatch.setattr(klod_inbox, "INBOX_DIR", tmp_path)
    monkeypatch.setattr(klod_inbox, "INBOX_FILE", tmp_path / "inbox.jsonl")
    monkeypatch.setattr(klod_inbox, "OUTBOX_FILE", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(klod_inbox, "COUNTER_FILE", tmp_path / "counter.txt")
    monkeypatch.setattr(klod_inbox, "PUSH_URLS_FILE", tmp_path / "push_urls.json")
    return tmp_path


def test_set_push_url_persists_and_validates(isolated_klod_dir):
    klod_inbox.set_push_url("qaper", "http://10.66.0.4:8080/klod/push")
    saved = json.loads((isolated_klod_dir / "push_urls.json").read_text())
    assert saved == {"qaper": "http://10.66.0.4:8080/klod/push"}

    # Update for the same agent
    klod_inbox.set_push_url("qaper", "https://qaper.lan/klod")
    assert klod_inbox.load_push_urls() == {"qaper": "https://qaper.lan/klod"}

    # Clear with None / empty
    klod_inbox.set_push_url("qaper", None)
    assert klod_inbox.load_push_urls() == {}


def test_set_push_url_rejects_garbage(isolated_klod_dir):
    with pytest.raises(ValueError):
        klod_inbox.set_push_url("x", "javascript:alert(1)")
    with pytest.raises(ValueError):
        klod_inbox.set_push_url("x", "ftp://nope")
    with pytest.raises(ValueError):
        klod_inbox.set_push_url("", "http://ok/")


def _mk_session(post_status: int = 200, get_status: int = 200, raise_post: Exception | None = None):
    session = MagicMock()
    # post()
    post_cm = MagicMock()
    post_resp = MagicMock(); post_resp.status = post_status
    post_cm.__aenter__ = AsyncMock(return_value=post_resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)
    if raise_post is not None:
        session.post = MagicMock(side_effect=raise_post)
    else:
        session.post = MagicMock(return_value=post_cm)
    # get()
    get_cm = MagicMock()
    get_resp = MagicMock(); get_resp.status = get_status
    get_cm.__aenter__ = AsyncMock(return_value=get_resp)
    get_cm.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=get_cm)
    return session


def test_deliver_reply_pushes_when_registered(isolated_klod_dir):
    klod_inbox.set_push_url("qaper", "http://10.66.0.4:8080/klod/push")
    session = _mk_session(post_status=204)
    ok, err = asyncio.run(klod_inbox.deliver_reply(
        "qaper", "hello", session=session, record_id=42, in_reply_to=10,
    ))
    assert ok is True and err is None
    session.post.assert_called_once()
    _, kwargs = session.post.call_args
    payload = kwargs["json"]
    assert payload["to"] == "qaper"
    assert payload["id"] == 42
    assert payload["in_reply_to"] == 10
    assert payload["message"] == "hello"
    assert payload["from"] == "klod-access"
    session.get.assert_not_called()


def test_deliver_reply_falls_back_when_no_push_url(isolated_klod_dir):
    # No push_url for this agent → must use legacy GET fallback
    session = _mk_session(get_status=200)
    ok, err = asyncio.run(klod_inbox.deliver_reply("nobody", "hi", session=session))
    assert ok is True and err is None
    session.get.assert_called_once()
    session.post.assert_not_called()


def test_deliver_reply_push_4xx_returns_error(isolated_klod_dir):
    klod_inbox.set_push_url("qaper", "http://qaper.lan/klod")
    session = _mk_session(post_status=503)
    ok, err = asyncio.run(klod_inbox.deliver_reply("qaper", "hi", session=session))
    assert ok is False
    assert err and "push HTTP 503" in err


def test_deliver_reply_push_exception_returns_error(isolated_klod_dir):
    klod_inbox.set_push_url("qaper", "http://qaper.lan/klod")
    session = _mk_session(raise_post=RuntimeError("boom"))
    ok, err = asyncio.run(klod_inbox.deliver_reply("qaper", "hi", session=session))
    assert ok is False
    assert err and "push exc" in err
