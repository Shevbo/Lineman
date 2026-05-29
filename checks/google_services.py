"""Google service health checks — Drive, Gmail, Calendar.

All checks use OAuth Bearer token from GOOGLE_ACCESS_TOKEN.
Drive/Gmail are read-only non-destructive probes.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


async def _google_health_check(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    service_name: str,
) -> dict[str, Any]:
    """Generic Google API health check.

    When OAuth token is provided, performs a real API call.
    When no token, falls back to simple connectivity ping
    (HEAD to base URL, accepts 401/403 as proof of reachability).
    """
    result: dict[str, Any] = {
        "online": False,
        "latency_ms": 0.0,
        "phase": "ping",
        "error": None,
        "status_code": None,
    }

    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        start = time.monotonic()
        response = await client.get(url, headers=headers)
        latency = (time.monotonic() - start) * 1000
        result["latency_ms"] = round(latency, 2)
        result["status_code"] = response.status_code

        if response.status_code in (200, 401, 403):
            # 200 = full auth OK, 401/403 = host reachable without proper token
            result["online"] = True
            if not api_key and response.status_code in (401, 403):
                result["phase"] = "connectivity"
        elif response.status_code >= 500:
            result["error"] = f"HTTP {response.status_code}"
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
        logger.exception("google_check_failed", service=service_name, error=str(exc))

    return result


async def check_google_drive(
    client: httpx.AsyncClient,
    api_key: str,
    deep_probe: bool = False,
) -> dict[str, Any]:
    """Check Google Drive API availability. Uses about.get for health."""
    result = await _google_health_check(client, DRIVE_ABOUT_URL, api_key, "drive")

    if deep_probe and result.get("online") and api_key:
        # Deep probe: fetch first file
        try:
            files_url = (
                "https://www.googleapis.com/drive/v3/files"
                "?pageSize=1&fields=files(id,name)"
            )
            response = await client.get(
                files_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code == 200:
                result["phase"] = "probe"
            else:
                result["online"] = False
                result["error"] = f"probe: HTTP {response.status_code}"
        except Exception as exc:
            result["online"] = False
            result["error"] = f"probe error: {exc}"

    return result


async def check_google_gmail(
    client: httpx.AsyncClient,
    api_key: str,
    deep_probe: bool = False,
) -> dict[str, Any]:
    """Check Gmail API availability."""
    result = await _google_health_check(client, GMAIL_PROFILE_URL, api_key, "gmail")

    if deep_probe and result.get("online") and api_key:
        try:
            resp = await client.get(
                GMAIL_PROFILE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("emailAddress"):
                    result["phase"] = "probe"
                else:
                    result["online"] = False
                    result["error"] = "probe: no emailAddress"
            else:
                result["online"] = False
                result["error"] = f"probe: HTTP {resp.status_code}"
        except Exception as exc:
            result["online"] = False
            result["error"] = f"probe error: {exc}"

    return result


async def check_google_calendar(
    client: httpx.AsyncClient,
    api_key: str,
    deep_probe: bool = False,
) -> dict[str, Any]:
    """Check Google Calendar API availability."""
    result = await _google_health_check(
        client, CALENDAR_LIST_URL, api_key, "calendar"
    )

    if deep_probe and result.get("online") and api_key:
        try:
            resp = await client.get(
                CALENDAR_LIST_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if "items" in data:
                    result["phase"] = "probe"
                else:
                    result["online"] = False
                    result["error"] = "probe: missing items"
            else:
                result["online"] = False
                result["error"] = f"probe: HTTP {resp.status_code}"
        except Exception as exc:
            result["online"] = False
            result["error"] = f"probe error: {exc}"

    return result
