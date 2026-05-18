#!/usr/bin/env python3
"""Lineman v2 — smart API gateway + autonomous health monitor.

Runs an HTTP forward proxy (:9090) alongside continuous health checks.
Python 3.12+, asyncio, aiohttp (proxy), httpx (health checks), structlog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

from checks import (
    check_deepseek,
    check_gemini,
    check_google_calendar,
    check_google_drive,
    check_google_gmail,
    check_telegram,
)
from db import RequestLogDB
from healer import evaluate_and_heal, HealAction
from token_harvester import run_harvester
from metrics import MetricsStore
from notifier import notify_state_change
from proxy_server import ProxyServer
from retry import retry_with_backoff

logger = structlog.get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _expand_env(obj: Any) -> Any:
    """Recursively expand ${VAR} references in config values using os.environ."""
    if isinstance(obj, str):
        return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(i) for i in obj]
    return obj


def load_config() -> dict[str, Any]:
    path = BASE_DIR / "config.json"
    with open(path) as f:
        return _expand_env(json.load(f))


def load_state() -> dict[str, Any]:
    config = load_config()
    state_path = BASE_DIR / config["global"]["state_file"]
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"services": {}, "cycle_count": 0, "last_report": None}


def save_state(state: dict[str, Any]) -> None:
    config = load_config()
    state_path = BASE_DIR / config["global"]["state_file"]
    tmp_path = state_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp_path.rename(state_path)


def resolve_api_key(env_var: str, config_path: str) -> str:
    """Resolve API key: env first, then openclaw config."""
    val = os.environ.get(env_var, "")
    if val:
        return val
    try:
        result = subprocess.run(
            ["openclaw", "config", "get", config_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def resolve_google_token() -> str:
    """Resolve Google OAuth token from env."""
    return os.environ.get("GOOGLE_ACCESS_TOKEN", "")


def build_client(use_proxy: bool, proxy_url: str, timeout: int) -> httpx.AsyncClient:
    """Build an httpx client, optionally with proxy."""
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout=timeout, pool=5.0),
    }
    if use_proxy and proxy_url:
        kwargs["proxy"] = proxy_url
        logger.debug("client_with_proxy", proxy=proxy_url)
    return httpx.AsyncClient(**kwargs)


CHECK_DISPATCH = {
    "deepseek": check_deepseek,
    "gemini": check_gemini,
    "google_drive": check_google_drive,
    "google_gmail": check_google_gmail,
    "google_calendar": check_google_calendar,
    "telegram": check_telegram,
}


RETRYABLE_ERRORS = {"timeout", "connection refused", "proxy error"}


async def _call_with_retry(
    check_fn: Any,
    client: httpx.AsyncClient,
    kwargs: dict[str, Any],
    max_retries: int = 3,
) -> dict[str, Any]:
    """Call check_fn with exponential backoff on transient errors."""
    for attempt in range(max_retries + 1):
        result = await check_fn(client, **kwargs)
        if result.get("online"):
            return result
        error = result.get("error", "")
        if error in RETRYABLE_ERRORS and attempt < max_retries:
            delay = 2 ** attempt
            logger.debug(
                "check_retry",
                attempt=attempt + 1,
                error=error,
                delay=delay,
            )
            await asyncio.sleep(delay)
        else:
            return result
    return {"online": False, "error": "retries exhausted"}


async def run_check(
    svc: dict[str, Any],
    state_svc: dict[str, Any],
    global_cfg: dict[str, Any],
    deep_probe: bool,
    metrics: MetricsStore,
) -> dict[str, Any]:
    """Run a check for one service, update state and metrics."""
    svc_id = svc["id"]
    svc_name = svc["name"]
    svc_type = svc["type"]

    proxy_url = global_cfg.get("proxy_url", "")
    if svc.get("use_proxy6") and global_cfg.get("proxy6_url"):
        proxy_url = global_cfg["proxy6_url"]

    use_proxy = bool(svc.get("proxy", False))
    timeout = svc.get("timeout", 15)

    async with build_client(use_proxy, proxy_url, timeout) as client:
        check_fn = CHECK_DISPATCH.get(svc_type)
        if check_fn is None:
            return {"online": False, "error": f"unknown type: {svc_type}"}

        api_key = ""
        if svc_type in ("google_drive", "google_gmail", "google_calendar"):
            api_key = resolve_google_token()
        else:
            api_key = resolve_api_key(
                svc.get("api_key_env", ""),
                svc.get("openclaw_config_path", ""),
            )

        if not api_key and svc_type not in ("google_drive", "google_gmail", "google_calendar"):
            logger.warning("no_api_key", service=svc_id)
            return {"online": False, "error": "no API key configured"}

        kwargs: dict[str, Any] = {"api_key": api_key, "deep_probe": deep_probe}
        if svc_type in ("deepseek", "gemini"):
            kwargs["model"] = svc["model"]
        if svc_type == "gemini" and svc.get("base_url"):
            kwargs["base_url"] = svc["base_url"]

        result = await _call_with_retry(check_fn, client, kwargs)

    # Update state
    now_iso = datetime.now(timezone.utc).isoformat()
    result["checked_at"] = now_iso

    online = result.get("online", False)
    down_threshold = global_cfg.get("down_threshold", 3)

    if online:
        state_svc["consecutive_failures"] = 0
        if state_svc["state"] == "down":
            state_svc["state"] = "online"
            result["state_changed"] = True
            result["previous_state"] = "down"
            result["new_state"] = "online"
        elif state_svc["state"] == "degraded":
            state_svc["state"] = "online"
            result["state_changed"] = True
            result["previous_state"] = "degraded"
            result["new_state"] = "online"
        else:
            state_svc["state"] = "online"
        state_svc["last_online"] = now_iso
    else:
        state_svc["consecutive_failures"] += 1
        if state_svc["consecutive_failures"] >= down_threshold:
            if state_svc["state"] != "down":
                result["state_changed"] = True
                result["previous_state"] = state_svc["state"]
                result["new_state"] = "down"
            state_svc["state"] = "down"
        else:
            if state_svc["state"] == "online":
                result["state_changed"] = True
                result["previous_state"] = "online"
                result["new_state"] = "degraded"
            state_svc["state"] = "degraded"

    # Update latency baseline (simple EWMA)
    latency = result.get("latency_ms", 0)
    if latency > 0:
        current_baseline = state_svc.get("latency_baseline_ms", latency)
        state_svc["latency_baseline_ms"] = round(
            0.9 * current_baseline + 0.1 * latency, 2
        )

    # Degraded check: latency > 2x baseline
    latency_mult = global_cfg.get("latency_multiplier", 2.0)
    if (
        state_svc["state"] == "online"
        and latency > state_svc["latency_baseline_ms"] * latency_mult
    ):
        state_svc["state"] = "degraded"
        result["state_changed"] = True
        result["previous_state"] = "online"
        result["new_state"] = "degraded"

    # Record metric
    await metrics.record(
        svc_id=svc_id,
        online=online,
        latency_ms=latency,
        phase=result.get("phase", "ping"),
        error=result.get("error"),
    )

    return result


async def monitoring_loop(
    proxy: ProxyServer,
    config: dict[str, Any],
    state: dict[str, Any],
    metrics: MetricsStore,
) -> None:
    """Run the 3-phase health check cycle."""
    global_cfg = config["global"]
    services = config["services"]
    deep_probe_every = global_cfg["deep_probe_every_n"]
    last_report_day: str | None = None

    while True:
        proxy.state = state
        proxy.metrics_store = metrics

        cycle_start = time.monotonic()
        state["cycle_count"] += 1
        cycle = state["cycle_count"]

        force_deep_probe = (cycle % deep_probe_every == 0)
        logger.debug("cycle_start", cycle=cycle, force_deep_probe=force_deep_probe)

        # --- Phase 1 & 2: Check each service ---
        heal_actions: list[HealAction] = []
        state_changes: list[dict[str, Any]] = []

        for svc in services:
            svc_id = svc["id"]
            state_svc = state["services"].setdefault(
                svc_id,
                {
                    "state": "online",
                    "consecutive_failures": 0,
                    "latency_baseline_ms": 300.0,
                    "last_online": None,
                    "incidents": [],
                },
            )

            need_deep_probe = force_deep_probe or (
                state_svc.get("consecutive_failures", 0) > 0
            )

            result = await run_check(
                svc, state_svc, global_cfg, need_deep_probe, metrics
            )

            log_kw = {
                "service": svc_id,
                "online": result.get("online"),
                "latency_ms": result.get("latency_ms"),
                "phase": result.get("phase"),
                "state": state_svc["state"],
                "error": result.get("error"),
            }

            if not result.get("online"):
                logger.warning("service_check_failed", **log_kw)
            elif result.get("state_changed"):
                logger.info("service_state_change", **log_kw)
                state_changes.append({
                    "service_id": svc_id,
                    "service_name": svc["name"],
                    "previous_state": result.get("previous_state"),
                    "new_state": result.get("new_state"),
                    "latency_ms": result.get("latency_ms"),
                })
            else:
                logger.debug("service_check_ok", **log_kw)

        # --- Phase 3: Tail gateway logs (errors only) ---
        await phase3_tail_logs(global_cfg, metrics)

        # --- Heal ---
        heal_actions = evaluate_and_heal(state, config)
        if heal_actions:
            logger.info("heal_actions_performed", count=len(heal_actions))

        # --- Notify ---
        for change in state_changes:
            heal_action_text = ""
            for ha in heal_actions:
                if ha.service_id == change["service_id"]:
                    heal_action_text = ha.action
                    break
            await notify_state_change(change, heal_action_text)

        # --- Persist ---
        save_state(state)
        metrics.flush()

        # --- Daily report check ---
        now = datetime.now()
        report_time = global_cfg.get("report_time", "23:59")
        report_h, report_m = map(int, report_time.split(":"))
        today_key = now.strftime("%Y-%m-%d")

        if (
            last_report_day != today_key
            and now.hour == report_h
            and now.minute >= report_m
        ):
            await generate_daily_report(state, metrics, config)
            last_report_day = today_key
            state["last_report"] = now.isoformat()

        # --- Sleep ---
        elapsed = time.monotonic() - cycle_start
        min_interval = min(svc.get("interval", 60) for svc in services)
        sleep_for = max(0, min_interval - elapsed)
        logger.debug(
            "cycle_end",
            cycle=cycle,
            elapsed_ms=round(elapsed * 1000),
            sleep_s=round(sleep_for, 1),
        )
        await asyncio.sleep(sleep_for)


async def phase3_tail_logs(
    global_cfg: dict[str, Any],
    metrics: MetricsStore,
) -> None:
    """Tail recent gateway log errors."""
    import glob as globmod

    pattern = global_cfg.get("gateway_log_pattern", "/tmp/openclaw/openclaw-*.log")
    try:
        log_files = sorted(globmod.glob(pattern), key=os.path.getmtime, reverse=True)
        if not log_files:
            return
        latest = log_files[0]
        out = subprocess.run(
            ["tail", "-n", "50", latest],
            capture_output=True,
            text=True,
            timeout=5,
        )
        error_count = 0
        for line in out.stdout.splitlines():
            lower = line.lower()
            if any(kw in lower for kw in ("error", "traceback", "failed", "crash")):
                error_count += 1
        if error_count > 0:
            logger.warning("gateway_log_errors", file=latest, errors=error_count)
            metrics.set_gauge("gateway_log_errors", error_count)
    except Exception:
        logger.exception("phase3_log_tail_failed")


async def generate_daily_report(
    state: dict[str, Any],
    metrics: MetricsStore,
    config: dict[str, Any],
) -> None:
    """Generate daily report and write to Google Doc."""
    from reporter import generate_report

    try:
        await generate_report(state, metrics, config)
    except Exception:
        logger.exception("daily_report_failed")


async def main_entry(config: dict[str, Any]) -> None:
    """Run proxy server + monitoring loop concurrently."""
    state = load_state()
    global_cfg = config["global"]
    metrics = MetricsStore(BASE_DIR / global_cfg["metrics_file"])

    # Initialize state for new services
    for svc in config["services"]:
        svc_id = svc["id"]
        if svc_id not in state["services"]:
            state["services"][svc_id] = {
                "state": "online",
                "consecutive_failures": 0,
                "latency_baseline_ms": 300.0,
                "last_online": None,
                "incidents": [],
            }

    # Initialize request log DB + signal queue
    db = RequestLogDB()
    db.init()
    signals = db.make_signal_queue()

    # Load agent metadata
    from agents_meta import load_agents_meta
    agents_cfg = config.get("agents", {})
    node_map = agents_cfg.get("node_map", {})
    agents_meta = load_agents_meta(node_map=node_map)
    logger.info("agents_meta_loaded", count=len(agents_meta))

    proxy = ProxyServer(config)
    proxy._db = db
    proxy._signals = signals
    proxy._agents_meta = agents_meta

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("shutdown_signal_received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    # Любой синхронный subprocess/sqlite-вызов в корутине морозит весь loop.
    # debug-режим логирует callback дольше 0.5с — ловит блокировки event loop.
    loop.slow_callback_duration = 0.5
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info(
        "lineman_started",
        services=len(config["services"]),
        deep_probe_every=global_cfg["deep_probe_every_n"],
        down_threshold=global_cfg["down_threshold"],
    )

    try:
        await proxy.start()

        monitor_task = asyncio.create_task(
            monitoring_loop(proxy, config, state, metrics)
        )
        harvester_task = asyncio.create_task(run_harvester(db))

        await shutdown_event.wait()

        logger.info("shutdown_initiated")
        for task in (monitor_task, harvester_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        await proxy.stop()
        save_state(state)
        metrics.flush()
        db.close()
        logger.info("lineman_shutdown_complete")


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG,
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    logger.info("lineman_booting")
    config = load_config()
    try:
        asyncio.run(main_entry(config), debug=True)
    except KeyboardInterrupt:
        logger.info("lineman_shutdown", reason="keyboard_interrupt")
    except Exception:
        logger.exception("lineman_fatal")
        sys.exit(1)


if __name__ == "__main__":
    main()
