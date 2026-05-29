"""Generic HTTP health check for OpenAI-compatible endpoints."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


async def check_openai_compat(
    client: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    health_endpoint: str = "/v1/models",
    deep_probe: bool = False,
) -> dict[str, Any]:
    """GET health_endpoint, treat 200/401/404 as online (service reachable)."""
    result: dict[str, Any] = {
        "online": False,
        "latency_ms": 0.0,
        "phase": "ping",
        "error": None,
        "status_code": None,
    }
    url = base_url.rstrip("/") + health_endpoint
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        start = time.monotonic()
        response = await client.get(url, headers=headers)
        result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
        result["status_code"] = response.status_code
        # 200, 401, 404 all mean the service is reachable
        if response.status_code < 500:
            result["online"] = True
        else:
            result["error"] = f"HTTP {response.status_code}"
    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connection refused"
    except httpx.ProxyError:
        result["error"] = "proxy error"
    except Exception as exc:
        result["error"] = f"unexpected: {exc}"
        logger.exception("check_openai_compat_failed", url=url, error=str(exc))
    return result
