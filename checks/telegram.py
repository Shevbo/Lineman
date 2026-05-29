"""Telegram Bot API health check."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

TELEGRAM_BASE = "https://api.telegram.org"


async def check_telegram(
    client: httpx.AsyncClient,
    api_key: str,
    deep_probe: bool = False,
) -> dict[str, Any]:
    """Check Telegram Bot API availability via getMe.

    Args:
        client: httpx client (with proxy6 if configured)
        api_key: Bot token

    Returns:
        dict with online, latency_ms, phase, error, status_code
    """
    result: dict[str, Any] = {
        "online": False,
        "latency_ms": 0.0,
        "phase": "ping",
        "error": None,
        "status_code": None,
    }

    if not api_key:
        result["error"] = "no bot token configured"
        return result

    url = f"{TELEGRAM_BASE}/bot{api_key}/getMe"

    try:
        start = time.monotonic()
        response = await client.get(url)
        latency = (time.monotonic() - start) * 1000
        result["latency_ms"] = round(latency, 2)
        result["status_code"] = response.status_code

        if response.status_code != 200:
            result["error"] = f"HTTP {response.status_code}"
            return result

        data = response.json()
        if data.get("ok") and data.get("result", {}).get("username"):
            result["online"] = True

            if deep_probe:
                result["phase"] = "probe"
                result["bot_username"] = data["result"]["username"]
        else:
            result["error"] = f"invalid response: {data.get('description', 'unknown')}"

    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connection refused"
    except httpx.ProxyError:
        result["error"] = "proxy error"
    except Exception as exc:
        result["error"] = f"unexpected: {exc}"
        logger.exception("check_telegram_failed", error=str(exc))

    return result
