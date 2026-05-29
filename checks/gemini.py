"""Gemini service health checks — Flash + Pro variants, via proxy."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_DIRECT_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


async def check_gemini(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    deep_probe: bool = False,
    base_url: str = "",
) -> dict[str, Any]:
    """Run a health check against a Gemini endpoint.

    Args:
        client: httpx client (with proxy if configured)
        api_key: Google API key
        model: model name (e.g. gemini-2.0-flash)
        deep_probe: force deep probe with real generation
        base_url: override base URL (e.g. CF Worker proxy); falls back to direct Google API

    Returns:
        dict with online, latency_ms, phase, error, status_code
    """
    health_base = f"{base_url.rstrip('/')}/v1beta/models" if base_url else _DIRECT_BASE

    result: dict[str, Any] = {
        "online": False,
        "latency_ms": 0.0,
        "phase": "ping",
        "error": None,
        "status_code": None,
    }

    try:
        health_url = f"{health_base}?key={api_key}"

        start = time.monotonic()
        response = await client.get(health_url)
        latency = (time.monotonic() - start) * 1000
        result["latency_ms"] = round(latency, 2)
        result["status_code"] = response.status_code

        if response.status_code >= 500:
            result["error"] = f"HTTP {response.status_code}"
            return result
        if response.status_code in (401, 403):
            result["error"] = f"Auth error: HTTP {response.status_code}"
            return result

        result["online"] = True

        if deep_probe:
            result["phase"] = "probe"
            probe_result = await _deep_probe_gemini(
                client, api_key, model, base_url=base_url
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
        logger.exception("check_gemini_failed", model=model, error=str(exc))

    return result


async def _deep_probe_gemini(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    base_url: str = "",
) -> dict[str, Any]:
    """Send a generateContent request to validate Gemini end-to-end."""
    generate_base = f"{base_url.rstrip('/')}/v1beta/models" if base_url else _DIRECT_BASE
    try:
        generate_url = (
            f"{generate_base}/{model}:generateContent?key={api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 4},
        }

        start = time.monotonic()
        response = await client.post(generate_url, json=payload)
        rtt = (time.monotonic() - start) * 1000
        data = response.json()

        if response.status_code == 429:
            # 429 means the API is reachable — health-check quota exhausted, not an outage
            return {
                "online": True,
                "latency_ms": round(rtt, 2),
                "error": "quota_exceeded",
            }
        if response.status_code != 200:
            return {
                "online": False,
                "latency_ms": round(rtt, 2),
                "error": f"probe HTTP {response.status_code}: {data.get('error', {}).get('message', 'unknown')}",
            }

        candidates = data.get("candidates")
        if not candidates:
            return {
                "online": False,
                "latency_ms": round(rtt, 2),
                "error": "probe: no candidates in response",
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
