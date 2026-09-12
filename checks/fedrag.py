"""Проба индекса канона федерации (ragkit на sdev).

Без этой пробы падение индекса замечает только агент, которому не повезло спросить:
Lineman отдаёт деградацию молча и с кодом 200, потому что агент не должен вставать.
Тишина в такой схеме и есть опасность — федерация продолжает работать «вслепую»
по тонким карточкам, и никто об этом не знает.

Проба ходит через локальный конец ssh-туннеля (PM2 `fedrag-tunnel`), поэтому клиент
берётся БЕЗ прокси-пула: loopback наружу гонять незачем.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

DEFAULT_URL = "http://127.0.0.1:8791"
# Демон держит модели резидентно, но первый запрос после перезагрузки индекса
# перечитывает векторы. Порог поставлен выше обычного ответа, чтобы не шуметь.
SLOW_MS = 5000.0


async def check_fedrag(
    client: httpx.AsyncClient,
    base_url: str = DEFAULT_URL,
    profile: str = "fed",
    deep_probe: bool = False,
    **_ignored: Any,
) -> dict[str, Any]:
    """Проверить, что индекс отвечает.

    ping: `GET /profiles` — жив ли демон и какие профили поднял.
    deep: реальный поиск, чтобы поймать случай «демон жив, индекс пуст или сломан».
    """
    result: dict[str, Any] = {
        "online": False,
        "latency_ms": 0.0,
        "phase": "ping",
        "error": None,
        "status_code": None,
    }

    base = base_url.rstrip("/")
    try:
        start = time.monotonic()
        response = await client.get(base + "/profiles")
        result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
        result["status_code"] = response.status_code

        if response.status_code in (404, 501):
            # Старый демон без поддержки профилей. 501 — потому что у него вообще нет
            # обработчика GET, и BaseHTTPRequestHandler отвечает "Unsupported method";
            # 404 — если обработчик есть, а пути нет. И то и другое означает «жив,
            # просто ещё не обновлён», а не аварию: ложная тревога тут хуже молчания.
            result["online"] = True
            result["error"] = None
            result["profiles"] = ["(демон без профилей)"]
            result["needs_upgrade"] = True
        elif response.status_code != 200:
            result["error"] = f"HTTP {response.status_code}"
            return result
        else:
            result["online"] = True
            data = response.json()
            result["profiles"] = data.get("profiles", [])
            if profile not in result["profiles"]:
                result["online"] = False
                result["error"] = (
                    f"профиль {profile!r} не поднят, есть только {result['profiles']}"
                )
                return result

        if not deep_probe:
            return result

        result["phase"] = "probe"
        start = time.monotonic()
        body = {"query": "контракт агента федерации"}
        if not result.get("needs_upgrade"):
            body["profile"] = profile
        response = await client.post(base + "/search", json=body)
        probe_ms = round((time.monotonic() - start) * 1000, 2)
        result["probe_latency_ms"] = probe_ms

        if response.status_code != 200:
            result["online"] = False
            result["error"] = f"поиск вернул HTTP {response.status_code}"
            return result

        text = (response.json() or {}).get("text", "")
        # Пустая выдача при живом демоне означает пустой или побитый индекс —
        # снаружи это неотличимо от «всё хорошо», если не проверять содержимое.
        if not text or "hits=0" in text:
            result["online"] = False
            result["error"] = "индекс отвечает, но ничего не находит"
        elif probe_ms > SLOW_MS:
            result["error"] = f"медленно: {probe_ms:.0f} мс"

    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connection refused (туннель fedrag-tunnel лежит?)"
    except Exception as exc:
        result["error"] = f"unexpected: {exc}"
        logger.exception("check_fedrag_failed", url=base, error=str(exc))

    return result
