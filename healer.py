"""Auto-healing: apply remediation actions when services go down.

Rules from TZ §5:
  - DeepSeek Pro DOWN → switch default to Flash
  - Gemini Pro DOWN → switch fallback to Flash
  - DeepSeek ALL DOWN → switch default to Gemini
  - Gemini ALL DOWN → alert, manual
  - Telegram DOWN → switch to Proxy6
  - Google Drive/Gmail DOWN → alert, manual
  - Gateway crash → systemctl restart openclaw-gateway
  - gemini-live-service crash → pm2 restart
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class HealAction:
    service_id: str
    action: str
    command: str | None = None
    success: bool = False
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _run(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    """Run a subprocess command, return (success, output/error)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip() or "ok"
        return False, result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    except FileNotFoundError:
        return False, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, str(exc)


def evaluate_and_heal(
    state: dict[str, Any],
    config: dict[str, Any],
) -> list[HealAction]:
    """Check service states and apply auto-healing rules."""
    actions: list[HealAction] = []
    services_state = state.get("services", {})
    global_cfg = config.get("global", {})

    # Gather states
    ds_flash = services_state.get("deepseek-flash", {})
    ds_pro = services_state.get("deepseek-pro", {})
    gem_flash = services_state.get("gemini-flash", {})
    gem_pro = services_state.get("gemini-pro", {})
    telegram = services_state.get("telegram", {})
    gdrive = services_state.get("google-drive", {})
    gmail = services_state.get("google-gmail", {})

    ds_all_down = (
        ds_flash.get("state") == "down" and ds_pro.get("state") == "down"
    )
    gem_all_down = (
        gem_flash.get("state") == "down" and gem_pro.get("state") == "down"
    )

    # Rule: DeepSeek Pro DOWN → switch default to Flash
    if ds_pro.get("state") == "down" and ds_flash.get("state") == "online":
        ha = HealAction(
            service_id="deepseek-pro",
            action="Switch default model to DeepSeek Flash",
            command="openclaw config set model.default deepseek/deepseek-v4-flash",
        )
        success, output = _run(["openclaw", "config", "set", "model.default", "deepseek/deepseek-v4-flash"])
        ha.success = success
        ha.error = output if not success else None
        actions.append(ha)
        _log_heal(ha)

    # Rule: Gemini Pro DOWN → log only (Flash is already in fallbacks chain; no config change needed)
    if gem_pro.get("state") == "down" and gem_flash.get("state") == "online":
        logger.warning("gemini_pro_down_flash_active", note="gemini-pro degraded; gemini-flash is serving as fallback")

    # Rule: DeepSeek ALL DOWN → switch default to Gemini
    if ds_all_down and not gem_all_down:
        target = "google/gemini-2.5-flash"
        ha = HealAction(
            service_id="deepseek-all",
            action=f"All DeepSeek down, switch default to {target}",
            command=f"openclaw config set model.default {target}",
        )
        success, output = _run(["openclaw", "config", "set", "model.default", target])
        ha.success = success
        ha.error = output if not success else None
        actions.append(ha)
        _log_heal(ha)

    # Rule: Gemini ALL DOWN → alert only (manual intervention)
    if gem_all_down:
        ha = HealAction(
            service_id="gemini-all",
            action="All Gemini services DOWN — manual intervention required",
        )
        actions.append(ha)
        logger.critical("gemini_all_down_manual_intervention")

    # Rule: Telegram DOWN → alert, manual intervention required
    # Proxy6 switching not implemented: Telegram service has use_proxy6=false in config.
    # Auto-switch was a no-op (set success=True without running any command). Fixed to alert-only.
    if telegram.get("state") == "down":
        ha = HealAction(
            service_id="telegram",
            action="Telegram DOWN — manual intervention required (check iProyal proxy, consider enabling use_proxy6 in config)",
        )
        actions.append(ha)
        logger.critical("telegram_down_manual_intervention")

    # Rule: Google Drive/Gmail DOWN → alert, manual
    for svc_id, svc_state in [("google-drive", gdrive), ("google-gmail", gmail)]:
        if svc_state.get("state") == "down":
            ha = HealAction(
                service_id=svc_id,
                action=f"{svc_id} DOWN — manual intervention required",
            )
            actions.append(ha)
            logger.critical("google_service_down_manual", service=svc_id)

    # Record incidents in state
    for ha in actions:
        entry = {
            "timestamp": ha.timestamp,
            "service_id": ha.service_id,
            "action": ha.action,
            "success": ha.success,
            "error": ha.error,
        }
        svc_entry = services_state.setdefault(ha.service_id, {})
        incidents = svc_entry.setdefault("incidents", [])
        incidents.append(entry)
        if len(incidents) > 200:
            svc_entry["incidents"] = incidents[-200:]

    return actions


def _log_heal(ha: HealAction) -> None:
    if ha.success:
        logger.info("heal_success", service=ha.service_id, action=ha.action)
    else:
        logger.error(
            "heal_failed",
            service=ha.service_id,
            action=ha.action,
            error=ha.error,
        )
