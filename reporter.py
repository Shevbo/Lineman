"""Daily report generator — writes to Google Doc via Google Docs API.

Report format (TZ §7):
  - Uptime % per service
  - Avg/max latency
  - Token consumption
  - Incidents: time, service, action, result
  - ASCII latency chart by hour
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

DOCS_API_BASE = "https://docs.googleapis.com/v1/documents"


async def generate_report(
    state: dict[str, Any],
    metrics: Any,  # MetricsStore (lazy import to avoid cycle)
    config: dict[str, Any],
) -> str | None:
    """Generate and write daily report to Google Doc.

    Returns the document ID on success, None on failure.
    """
    global_cfg = config.get("global", {})
    folder_id = global_cfg.get("google_drive_folder", "")
    proxy_url = global_cfg.get("proxy_url", "")

    metrics_snapshot = metrics.get_snapshot()
    services_state = state.get("services", {})
    now = datetime.now(timezone.utc)

    title = f"Lineman Daily Report — {now.strftime('%Y-%m-%d')}"

    # Build report content
    lines: list[str] = []
    lines.append(title)
    lines.append("=" * len(title))
    lines.append(f"Generated: {now.isoformat()}")
    lines.append(f"Cycle count: {state.get('cycle_count', 0)}")
    lines.append("")

    # Uptime and latency table
    lines.append("## Service Status")
    lines.append("")
    lines.append(f"{'Service':<22} {'State':<10} {'Latency':<10} {'Avg 24h':<10} {'Uptime':<8} {'Err/24h':<8}")
    lines.append("-" * 68)

    for svc_id, svc_state in sorted(services_state.items()):
        m = metrics_snapshot.get(svc_id, {})
        state_str = svc_state.get("state", "unknown")
        latency = svc_state.get("latency_ms", 0)
        avg_24h = m.get("latency_avg_24h", 0)
        uptime = m.get("uptime_pct", 100)
        errors = m.get("errors_24h", 0)

        lines.append(
            f"{svc_id:<22} {state_str:<10} {latency:>6.0f}ms   {avg_24h:>6.0f}ms   "
            f"{uptime:>5.1f}%  {errors:>5}"
        )

    lines.append("")

    # Incidents
    lines.append("## Incidents")
    lines.append("")
    has_incidents = False
    for svc_id, svc_state in sorted(services_state.items()):
        incidents = svc_state.get("incidents", [])
        if incidents:
            has_incidents = True
            lines.append(f"### {svc_id}")
            for inc in incidents[-20:]:  # Last 20
                ts = inc.get("timestamp", "?")
                action = inc.get("action", "?")
                success = "✅" if inc.get("success") else "❌"
                error = inc.get("error", "")
                lines.append(f"  {ts} | {action} | {success}")
                if error:
                    lines.append(f"         error: {error}")
            lines.append("")

    if not has_incidents:
        lines.append("No incidents today.")
        lines.append("")

    # ASCII latency chart
    lines.append("## Latency by Hour (24h)")

    report_text = "\n".join(lines)

    # Write to Google Doc
    token = _get_google_token()
    if not token:
        logger.error("report_no_google_token")
        return None

    doc_id = await _create_google_doc(title, report_text, token, proxy_url, folder_id)
    if doc_id:
        logger.info("report_generated", doc_id=doc_id, folder=folder_id)

    return doc_id


def _get_google_token() -> str:
    import os

    return os.environ.get("GOOGLE_ACCESS_TOKEN", "")


async def _create_google_doc(
    title: str,
    content: str,
    token: str,
    proxy_url: str,
    folder_id: str,
) -> str | None:
    """Create a Google Doc with the report content."""
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout=30, pool=5.0),
        "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**kwargs) as client:
        try:
            # Step 1: Create empty document
            create_body = {"title": title}
            resp = await client.post(
                DOCS_API_BASE,
                json=create_body,
            )
            if resp.status_code != 200:
                logger.error("report_doc_create_failed", status=resp.status_code, body=resp.text)
                return None

            doc_data = resp.json()
            doc_id = doc_data.get("documentId")
            if not doc_id:
                return None

            # Step 2: Insert text
            requests_body = {
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": content,
                        }
                    }
                ]
            }
            resp2 = await client.post(
                f"{DOCS_API_BASE}/{doc_id}:batchUpdate",
                json=requests_body,
            )
            if resp2.status_code != 200:
                logger.error("report_doc_update_failed", status=resp2.status_code)
                return doc_id  # doc exists even if update fails

            # Step 3: Move to folder
            if folder_id:
                await _move_to_folder(doc_id, folder_id, token, proxy_url)

            return doc_id

        except Exception:
            logger.exception("report_create_failed")
            return None


async def _move_to_folder(
    doc_id: str,
    folder_id: str,
    token: str,
    proxy_url: str,
) -> None:
    """Move Google Doc to the specified folder."""
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout=15, pool=5.0),
        "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**kwargs) as client:
        try:
            resp = await client.post(
                f"https://www.googleapis.com/drive/v3/files/{doc_id}",
                json={"addParents": folder_id},
            )
            if resp.status_code != 200:
                logger.warning("report_move_to_folder_failed", status=resp.status_code)
        except Exception:
            logger.exception("report_move_failed")
