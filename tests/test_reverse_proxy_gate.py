"""Реверс-прокси /proxy/{provider}/* — только из доверенных сетей.

Аудит 2026-09-15: :9090 слушает 0.0.0.0, а /proxy/* не был за IP-барьером. Любой
адрес в интернете получал ключи провайдеров, которые Lineman подставляет сам:
GET http://83.69.248.77:9090/proxy/deepseek/v1/models → 200 со списком моделей,
GET .../proxy/google/v1beta/models → 200. Для anthropic хватало заголовка
X-Agent-Name: ltx, чтобы получить OAuth Клода.

Легитимные потребители /proxy/* — агенты федерации (loopback, WG, Tailscale,
docker); за месяц в request_log только smain и hoster. Снаружи в него ходить некому.
"""
from __future__ import annotations

import inspect

import pytest

from proxy_server import ProxyServer


@pytest.fixture
def srv():
    return ProxyServer(config={"proxy_server": {}, "proxy_pool": {}})


def test_публичный_адрес_на_proxy_блокируется(srv):
    assert srv._reverse_proxy_blocked("/proxy/deepseek/v1/models", "83.69.248.77")
    assert srv._reverse_proxy_blocked("/proxy/google/v1beta/models", "203.0.113.5")
    assert srv._reverse_proxy_blocked("/proxy/anthropic/v1/messages", "134.195.158.62")


def test_доверенные_сети_на_proxy_пропускаются(srv):
    for ip in ("127.0.0.1", "::1", "10.66.0.7", "100.64.1.1", "172.18.0.2"):
        assert not srv._reverse_proxy_blocked("/proxy/deepseek/v1/chat/completions", ip), ip


def test_не_proxy_пути_сторож_не_трогает(srv):
    """Публичные auth-эндпоинты и /health живут под своими правилами."""
    for path in ("/health", "/api/login", "/proxy", "/proxyfoo", "/dashboard"):
        assert not srv._reverse_proxy_blocked(path, "83.69.248.77"), path


def test_пустой_или_кривой_source_ip_блокируется(srv):
    assert srv._reverse_proxy_blocked("/proxy/deepseek/v1/models", "")
    assert srv._reverse_proxy_blocked("/proxy/deepseek/v1/models", "not-an-ip")


def test_сторож_встроен_в_диспетчер_до_ветки_proxy():
    """Проверка не по поведению, а по коду: сторож должен стоять в _raw_handler
    раньше ветки `startswith("/proxy/")`, иначе запрос уйдёт к провайдеру."""
    src = inspect.getsource(ProxyServer.start)
    gate = src.index("self._reverse_proxy_blocked(request_path_only, source_ip)")
    branch = src.index('elif request_path_only.startswith("/proxy/"):')
    assert gate < branch
