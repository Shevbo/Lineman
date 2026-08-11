"""Инвариант: _split_proxy_auth разделяет http://user:pass@host на URL без
auth + BasicAuth. Пароль НИКОГДА не должен оставаться в URL — иначе aiohttp
кладёт его в exception message при 407/timeout (регрессия 2026-08-11).
"""
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from reverse_proxy import _split_proxy_auth


def test_url_without_auth_unchanged():
    url = "http://45.85.162.25:8000"
    u, a = _split_proxy_auth(url)
    assert u == url
    assert a is None


def test_url_with_basic_auth_split():
    url = "http://tfCvF1:97QsVP@45.85.162.25:8000"
    u, a = _split_proxy_auth(url)
    assert u == "http://45.85.162.25:8000"
    assert a is not None
    assert a.login == "tfCvF1"
    assert a.password == "97QsVP"


def test_url_with_path():
    url = "http://user:pass@host:1080/socks"
    u, a = _split_proxy_auth(url)
    assert u == "http://host:1080/socks"
    assert a.login == "user"
    assert a.password == "pass"


def test_url_with_empty_password():
    url = "http://user@host:80"
    u, a = _split_proxy_auth(url)
    assert u == "http://host:80"
    assert a.login == "user"
    assert a.password == ""


def test_url_with_url_encoded_credentials():
    """Passwords с спец-символами приходят URL-encoded, unquote должен вернуть raw."""
    url = "http://us%40er:p%40ss@host:80"
    u, a = _split_proxy_auth(url)
    assert a.login == "us@er"
    assert a.password == "p@ss"


def test_none_input():
    u, a = _split_proxy_auth(None)
    assert u is None and a is None


def test_empty_input():
    u, a = _split_proxy_auth("")
    assert u in ("", None)  # falsy — helper возвращает пустую строку как есть
    assert a is None


def test_password_never_in_url_after_split():
    """Самый важный инвариант: после split строка URL не должна содержать пароль."""
    url = "http://LOGIN123:SECRETPWD@host:80/path"
    u, a = _split_proxy_auth(url)
    assert "SECRETPWD" not in u, "пароль остался в URL — регрессия утечки"


def test_returns_aiohttp_basicauth_type():
    """proxy_auth должен быть именно aiohttp.BasicAuth, не dict."""
    url = "http://u:p@h:1"
    _, a = _split_proxy_auth(url)
    assert isinstance(a, aiohttp.BasicAuth)
