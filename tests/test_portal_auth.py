"""Tests for Shectory Portal Basic Auth bridge in proxy_server."""
from __future__ import annotations

import base64
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_server import ProxyServer


@pytest.fixture
def srv():
    return ProxyServer(config={"proxy_server": {}, "proxy_pool": {}})


def test_parse_basic_auth_ok(srv):
    raw = base64.b64encode(b"bshevelev@mail.ru:secret123").decode()
    headers = {"authorization": f"Basic {raw}"}
    assert srv._parse_basic_auth(headers) == ("bshevelev@mail.ru", "secret123")


def test_parse_basic_auth_missing(srv):
    assert srv._parse_basic_auth({}) is None


def test_parse_basic_auth_wrong_scheme(srv):
    assert srv._parse_basic_auth({"authorization": "Bearer abc"}) is None


def test_parse_basic_auth_no_colon(srv):
    raw = base64.b64encode(b"noseparator").decode()
    assert srv._parse_basic_auth({"authorization": f"Basic {raw}"}) is None


def test_parse_basic_auth_empty_password(srv):
    raw = base64.b64encode(b"user:").decode()
    assert srv._parse_basic_auth({"authorization": f"Basic {raw}"}) is None


def test_parse_basic_auth_malformed_b64(srv):
    assert srv._parse_basic_auth({"authorization": "Basic !!!"}) is None


@pytest.mark.asyncio
async def test_verify_portal_credentials_no_secret(srv, monkeypatch):
    monkeypatch.delenv("SHECTORY_AUTH_BRIDGE_SECRET", raising=False)
    assert await srv._verify_portal_credentials("a@b.c", "x") is False


@pytest.mark.asyncio
async def test_verify_portal_credentials_cache_hit(srv):
    import hashlib
    key = hashlib.sha256(b"a@b.c:pw").hexdigest()
    srv._portal_auth_cache[key] = time.time() + 60
    assert await srv._verify_portal_credentials("a@b.c", "pw") is True


@pytest.mark.asyncio
async def test_verify_portal_credentials_cache_expired(srv, monkeypatch):
    import hashlib
    key = hashlib.sha256(b"a@b.c:pw").hexdigest()
    srv._portal_auth_cache[key] = time.time() - 1
    monkeypatch.delenv("SHECTORY_AUTH_BRIDGE_SECRET", raising=False)
    assert await srv._verify_portal_credentials("a@b.c", "pw") is False


@pytest.mark.asyncio
async def test_verify_portal_credentials_bridge_ok(srv, monkeypatch):
    monkeypatch.setenv("SHECTORY_AUTH_BRIDGE_SECRET", "bridge-secret")
    monkeypatch.setenv("SHECTORY_PORTAL_URL", "http://portal.local")

    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"ok": True, "email": "a@b.c", "role": "admin"})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    sess = MagicMock()
    sess.post = MagicMock(return_value=resp)
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=False)

    with patch("proxy_server.aiohttp.ClientSession", return_value=sess):
        ok = await srv._verify_portal_credentials("a@b.c", "pw")
    assert ok is True
    # Кэш заполнен.
    import hashlib
    key = hashlib.sha256(b"a@b.c:pw").hexdigest()
    assert key in srv._portal_auth_cache


@pytest.mark.asyncio
async def test_verify_portal_credentials_bridge_bad_password(srv, monkeypatch):
    monkeypatch.setenv("SHECTORY_AUTH_BRIDGE_SECRET", "bridge-secret")

    resp = MagicMock()
    resp.status = 401
    resp.json = AsyncMock(return_value={"error": "Invalid credentials"})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    sess = MagicMock()
    sess.post = MagicMock(return_value=resp)
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=False)

    with patch("proxy_server.aiohttp.ClientSession", return_value=sess):
        ok = await srv._verify_portal_credentials("a@b.c", "wrong")
    assert ok is False
    assert srv._portal_auth_cache == {}


@pytest.mark.asyncio
async def test_verify_portal_credentials_bridge_network_error(srv, monkeypatch):
    monkeypatch.setenv("SHECTORY_AUTH_BRIDGE_SECRET", "bridge-secret")

    sess = MagicMock()
    sess.post = MagicMock(side_effect=ConnectionError("portal down"))
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=False)

    with patch("proxy_server.aiohttp.ClientSession", return_value=sess):
        ok = await srv._verify_portal_credentials("a@b.c", "pw")
    assert ok is False
