"""Agent notification via OpenClaw sessions send.

Sends state change alerts to all active agents.
Format: ⚠️ ${SERVICE} ${STATE} | latency=${LATENCY}ms | autoheal=${ACTION}
"""

from __future__ import annotations

import subprocess
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def notify_state_change(
    change: dict[str, Any],
    heal_action: str = "",
) -> None:
    """Send a state change notification to all active agents.

    Args:
        change: dict with service_name, previous_state, new_state, latency_ms
        heal_action: description of auto-heal action taken (if any)
    """
    service = change["service_name"]
    prev = change.get("previous_state", "unknown")
    new = change.get("new_state", "unknown")
    latency = change.get("latency_ms", 0)

    emoji = _state_emoji(new)
    msg = f"{emoji} {service} {new.upper()}"

    if new == "degraded":
        msg += f" (was {prev})"
    msg += f" | latency={latency}ms"

    if heal_action:
        msg += f" | autoheal={heal_action}"

    logger.info("notify", message=msg)

    try:
        subprocess.run(
            ["openclaw", "sessions", "send", "--all", msg],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        logger.warning("notify_openclaw_not_found")
    except subprocess.TimeoutExpired:
        logger.warning("notify_timeout")
    except Exception:
        logger.exception("notify_failed")


def _state_emoji(state: str) -> str:
    if state == "online":
        return "✅"
    elif state == "degraded":
        return "🟡"
    elif state == "down":
        return "🔴"
    return "❓"
