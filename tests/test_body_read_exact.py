"""Тело запроса читается целиком, а не одним TCP-сегментом.

Регрессия, найденная 2026-10-03: все bodyful-ручки Lineman читали тело как
`rd.read(min(content_length, cap))`. `StreamReader.read(n)` отдаёт ДО n байт, то есть
содержимое первого пришедшего сегмента, поэтому длинное письмо обрезалось молча.
Письмо rag-shectory 52694 в inbox Клода потеряло половину текста на 1000 байтах, а
`decode(errors="replace")` дописал U+FFFD — обрез был не отличим от особенности текста.
"""
import asyncio

import pytest

from proxy_server import _read_body_exact


async def _feed(chunks: list[bytes]) -> asyncio.StreamReader:
    rd = asyncio.StreamReader()
    for c in chunks:
        rd.feed_data(c)
    rd.feed_eof()
    return rd


@pytest.mark.asyncio
async def test_body_split_across_segments_arrives_whole():
    """Главный случай: тело пришло двумя порциями."""
    head, tail = b"a" * 1000, b"b" * 500
    rd = await _feed([head, tail])
    assert await _read_body_exact(rd, 1500, 65536) == head + tail


@pytest.mark.asyncio
async def test_russian_text_not_cut_mid_character():
    """Обрез на середине UTF-8 давал U+FFFD и прятал потерю."""
    text = ("Клод, привет. Это rag-shectory. " * 60).encode("utf-8")
    rd = await _feed([text[:1000], text[1000:]])
    got = await _read_body_exact(rd, len(text), 65536)
    assert got == text
    assert "�" not in got.decode("utf-8")


@pytest.mark.asyncio
async def test_cap_is_respected():
    rd = await _feed([b"x" * 5000])
    assert await _read_body_exact(rd, 5000, 1024) == b"x" * 1024


@pytest.mark.asyncio
async def test_zero_length_body():
    rd = await _feed([])
    assert await _read_body_exact(rd, 0, 65536) == b""


@pytest.mark.asyncio
async def test_client_hung_up_returns_partial():
    """Клиент обещал больше, чем отдал: возвращаем что есть, ручка решит сама."""
    rd = await _feed([b"half"])
    assert await _read_body_exact(rd, 100, 65536) == b"half"
