"""DeepSeek: ключ провайдера держит только Lineman.

Агенты openclaw слали сырой протухший ключ, а Lineman подставлял свой, только если
заголовка не было, — клиентский уходил насквозь. За 3 дня 113 ответов 401: все с
клиентским ключом; все 408 ответов 200 — без него (2026-09-14).
"""
from __future__ import annotations

from reverse_proxy import _inject_deepseek_key


def test_протухший_клиентский_ключ_заменяется_ключом_lineman():
    headers = {"content-type": "application/json", "authorization": "Bearer sk-stale-client"}
    assert _inject_deepseek_key(headers, "sk-lineman") == "injected"
    assert headers["authorization"] == "Bearer sk-lineman"


def test_без_клиентского_ключа_подставляется_ключ_lineman():
    headers = {"content-type": "application/json"}
    assert _inject_deepseek_key(headers, "sk-lineman") == "injected"
    assert headers["authorization"] == "Bearer sk-lineman"


def test_заголовок_в_любом_регистре_срезается_и_не_дублируется():
    headers = {"Authorization": "Bearer sk-stale-client"}
    _inject_deepseek_key(headers, "sk-lineman")
    auth = [k for k in headers if k.lower() == "authorization"]
    assert auth == ["authorization"], "должен остаться ровно один заголовок"
    assert headers["authorization"] == "Bearer sk-lineman"


def test_без_ключа_lineman_клиентский_не_трогаем():
    """Иначе вместо возможного успеха гарантированный 401."""
    headers = {"authorization": "Bearer sk-client"}
    assert _inject_deepseek_key(headers, "") == "no-lineman-key"
    assert headers["authorization"] == "Bearer sk-client"
