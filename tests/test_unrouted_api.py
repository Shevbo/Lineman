"""Неизвестные относительные пути: 404/405 вместо «Proxy error» 502.

Письмо omniroute (2026-09-14): GET на /api/tg/send и /api/klod/ask отдавал 502, и API
выглядел лежащим. На деле эти эндпоинты POST-only, а запрос проваливался сквозь все
ветки диспетчера в форвард-прокси. 502 означает «upstream сломан» и прямо уводит
диагностику в сторону.
"""
from __future__ import annotations

import pytest

from proxy_server import ProxyServer


@pytest.fixture
def srv():
    return ProxyServer(config={"proxy_server": {}, "proxy_pool": {}})


def test_get_на_post_only_tg_send_это_405(srv):
    status, body = srv._unrouted_response("GET", "/api/tg/send")
    assert status == 405
    assert body["allowed_method"] == "POST"


def test_get_на_post_only_klod_ask_это_405(srv):
    status, body = srv._unrouted_response("GET", "/api/klod/ask")
    assert status == 405
    assert body["allowed_method"] == "POST"


def test_post_на_get_only_это_405(srv):
    status, body = srv._unrouted_response("POST", "/api/pool/stats")
    assert status == 405
    assert body["allowed_method"] == "GET"


def test_несуществующий_путь_это_404(srv):
    status, body = srv._unrouted_response("GET", "/api/nonexistent-path")
    assert status == 404
    assert body["path"] == "/api/nonexistent-path"


def test_тот_же_метод_на_известном_пути_не_405(srv):
    """Сюда запрос с правильным методом не доходит — его забирает ветка диспетчера.
    Если всё же дошёл, это не «метод не тот», а отсутствующий обработчик: 404."""
    status, _ = srv._unrouted_response("POST", "/api/tg/send")
    assert status == 404


def test_относительный_путь_не_считается_форвард_прокси(srv):
    assert not srv._is_forward_proxy("GET", "/api/tg/send")


def test_абсолютный_uri_по_прежнему_форвард_прокси(srv):
    """Сторож не должен ломать настоящий форвард-прокси."""
    assert srv._is_forward_proxy("GET", "http://example.com/")
    assert srv._is_forward_proxy("CONNECT", "api.telegram.org:443")


def test_таблица_методов_согласована_с_диспетчером():
    """Каждый путь из таблицы действительно есть в диспетчере с этим методом —
    иначе 405 будет врать про допустимый метод."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "proxy_server.py").read_text(encoding="utf-8")
    for path, method in ProxyServer._METHOD_RESTRICTED_API.items():
        single = re.search(
            r'request_path_only == "%s" and method == "%s"' % (re.escape(path), method), src)
        grouped = re.search(
            r'request_path_only in \([^)]*"%s"[^)]*\) and method == "%s"' % (re.escape(path), method), src)
        assert single or grouped, "в диспетчере нет ветки %s %s" % (method, path)
