"""Проба DeepSeek обязана называть причину отказа.

deepseek-flash лежал сутками с ошибкой «probe error: » — пустой: у httpx.ReadTimeout
нет текста. Настоящая причина (модель не отвечает за таймаут на стороне DeepSeek)
из лога не читалась, пока её не воспроизвели руками (2026-09-14).
"""
from __future__ import annotations

import asyncio

import httpx

from checks.deepseek import _deep_probe_deepseek, _exc_text


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    async def post(self, *a, **kw):
        raise self._exc


def test_пустое_исключение_даёт_имя_типа():
    assert _exc_text(httpx.ReadTimeout("")) == "ReadTimeout"


def test_исключение_с_текстом_даёт_тип_и_текст():
    assert _exc_text(ValueError("bad json")) == "ValueError: bad json"


def test_таймаут_пробы_виден_в_ошибке():
    res = asyncio.run(_deep_probe_deepseek(_RaisingClient(httpx.ReadTimeout("")), "k", "deepseek-v4-flash"))
    assert res["online"] is False
    assert res["error"] == "probe error: ReadTimeout"
