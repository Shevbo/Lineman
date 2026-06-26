"""Tests for forward-proxy IP-allowlist gate (инцидент 2026-06-26 open-proxy абуз)."""
from __future__ import annotations

import pytest

from proxy_server import ProxyServer


@pytest.fixture
def srv():
    return ProxyServer(config={"proxy_server": {}, "proxy_pool": {}})


# --- _is_forward_proxy: что считается forward-proxy запросом ---

def test_connect_is_forward_proxy(srv):
    assert srv._is_forward_proxy("CONNECT", "api.solebox.com:443") is True


def test_absolute_http_uri_is_forward_proxy(srv):
    assert srv._is_forward_proxy("GET", "http://example.com/path") is True


def test_absolute_https_uri_is_forward_proxy(srv):
    assert srv._is_forward_proxy("GET", "https://example.com/path") is True


def test_origin_form_api_is_not_forward_proxy(srv):
    # Локальные management/reverse пути не должны попадать под forward-gate.
    assert srv._is_forward_proxy("GET", "/api/log") is False
    assert srv._is_forward_proxy("POST", "/proxy/google/v1beta/models") is False
    assert srv._is_forward_proxy("GET", "/health") is False


# --- gate = forward-proxy И не из доверенной сети ---

def test_external_connect_blocked(srv):
    # Внешний абуз (публичный IP) через CONNECT — блок.
    assert srv._is_forward_proxy("CONNECT", "cqsqwl.com:443") is True
    assert srv._is_admin_allowed("134.195.158.62") is False


def test_wg_federation_connect_allowed(srv):
    # Агент федерации через WireGuard — пропускаем.
    assert srv._is_admin_allowed("10.66.0.5") is True


def test_loopback_connect_allowed(srv):
    # Локальные агенты и vibe-туннель (127.0.0.1:19090).
    assert srv._is_admin_allowed("127.0.0.1") is True


def test_tailscale_connect_allowed(srv):
    assert srv._is_admin_allowed("100.64.1.1") is True
