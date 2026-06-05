"""Федеративный web_search — keyless, через egress Lineman (iProyal), без внешних API/ключей.

Источник: DuckDuckGo lite (POST). Парсинг regex (bs4 в venv нет). Возвращает
структурный список {title, url, snippet} — app-friendly для агентов федерации.
"""
from __future__ import annotations

import re
from html import unescape

import aiohttp

_LINK = re.compile(r'<a[^>]+href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', re.S)
_SNIP = re.compile(r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

DDG_LITE = "https://lite.duckduckgo.com/lite/"


def _clean(s: str) -> str:
    return _WS.sub(" ", unescape(_TAG.sub("", s or ""))).strip()


async def web_search(query: str, proxy: str | None = None,
                     limit: int = 6, timeout: int = 20) -> list[dict]:
    """Вернуть до limit результатов [{title, url, snippet}] по query.

    proxy — URL forward-прокси для egress (по умолчанию свой Lineman :9090,
    который пробивает геоблок через iProyal)."""
    # ВАЖНО: НЕ ставить Content-Type вручную — aiohttp сам url-энкодит data=dict
    # и ставит правильный заголовок; ручной Content-Type ломал форму (DDG → пустая страница).
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ru,en;q=0.8",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(DDG_LITE, data={"q": query}, headers=headers, proxy=proxy,
                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            html = await r.text()
    links = _LINK.findall(html)
    snips = [_clean(m) for m in _SNIP.findall(html)]
    out: list[dict] = []
    for i, (url, title) in enumerate(links[:limit]):
        out.append({
            "title": _clean(title),
            "url": url,
            "snippet": snips[i] if i < len(snips) else "",
        })
    return out
