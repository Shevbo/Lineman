"""DeepSeek service health checks — Flash + Pro variants."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

DEEPSEEK_HEALTH_URL = "https://api.deepseek.com/v1/models"
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/v1/chat/completions"


async def check_deepseek(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    deep_probe: bool = False,
) -> dict[str, Any]:
    """Run a health check against a DeepSeek endpoint.

    Returns dict with:
      - online: bool
      - latency_ms: float
      - phase: "ping" | "probe"
      - error: str | None
      - status_code: int | None
    """
    result: dict[str, Any] = {
        "online": False,
        "latency_ms": 0.0,
        "phase": "ping",
        "error": None,
        "status_code": None,
    }

    try:
        start = time.monotonic()
        response = await client.get(
            DEEPSEEK_HEALTH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        latency = (time.monotonic() - start) * 1000
        result["latency_ms"] = round(latency, 2)
        result["status_code"] = response.status_code

        if response.status_code >= 500:
            result["error"] = f"HTTP {response.status_code}"
            return result
        if response.status_code == 401 or response.status_code == 403:
            result["error"] = f"Auth error: HTTP {response.status_code}"
            return result

        result["online"] = True

        if deep_probe:
            result["phase"] = "probe"
            probe_result = await _deep_probe_deepseek(
                client, api_key, model
            )
            result.update(probe_result)

    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connection refused"
    except httpx.ProxyError:
        result["error"] = "proxy error"
    except Exception as exc:
        result["error"] = f"unexpected: {exc}"
        logger.exception("check_deepseek_failed", model=model, error=str(exc))

    return result


async def _deep_probe_deepseek(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Send a real chat completion to validate the API end-to-end."""
    try:
        start = time.monotonic()
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 4,
            "temperature": 0,
        }
        response = await client.post(
            DEEPSEEK_CHAT_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        rtt = (time.monotonic() - start) * 1000
        data = response.json()

        if response.status_code != 200:
            return {
                "online": False,
                "latency_ms": round(rtt, 2),
                "error": f"probe HTTP {response.status_code}: {data.get('error', {}).get('message', 'unknown')}",
            }

        content = data.get("choices", [{}])[0].get("message", {}).get("content")
        if content is None:
            return {
                "online": False,
                "latency_ms": round(rtt, 2),
                "error": "probe: missing content in response",
            }

        return {
            "online": True,
            "latency_ms": round(rtt, 2),
        }
    except Exception as exc:
        return {
            "online": False,
            "latency_ms": 0.0,
            "error": f"probe error: {exc}",
        }
