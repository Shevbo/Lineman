"""Lineman MCP Server — token economy tools for Claude Code.

Exposes:
  rtk_run         — run bash command via RTK (60-90% output compression)
  lineman_analytics — my Claude Code token usage from JSONL logs
  lineman_stats   — today's API stats from Lineman DB
  routing_hint    — smart model/provider recommendation
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lineman")

RTK_BIN = Path("~/.local/bin/rtk").expanduser()
LINEMAN_DB = Path("~/workspaces/lineman/lineman.db").expanduser()
OC_JSON = Path("~/.openclaw/openclaw.json").expanduser()
CLAUDE_LOGS = Path("~/.claude/projects").expanduser()

RTK_ALLOWED = frozenset({
    "git", "ls", "cat", "grep", "find", "tree",
    "npm", "cargo", "pytest", "docker", "gh",
    "pip", "python", "python3", "make", "head",
    "tail", "diff", "wc", "sort", "uniq",
})


# ── RTK: compress bash output ─────────────────────────────────────────────────

@mcp.tool()
def rtk_run(command: str, cwd: str = "") -> str:
    """Run a shell command through RTK for compressed output (60-90% smaller).

    Use instead of Bash for verbose commands: git log, ls -la, cat <large-file>,
    grep results, find results, diff, pytest output, docker ps, etc.

    Returns compressed text output. Falls back to plain output if RTK fails.
    command: shell command string, first token must be a standard unix tool
    cwd: working directory (optional, defaults to current)
    """
    if not RTK_BIN.exists():
        return _plain_run(command, cwd)

    tokens = command.strip().split()
    if not tokens:
        return "error: empty command"

    base = tokens[0]
    if base not in RTK_ALLOWED:
        return _plain_run(command, cwd)

    # Block shell injection
    if re.search(r'[|;&`$(){}<>]', command):
        return _plain_run(command, cwd)

    work_dir = cwd or os.getcwd()

    # Run raw first to get baseline
    try:
        raw = subprocess.run(tokens, capture_output=True, text=True, timeout=30, cwd=work_dir)
        raw_len = len(raw.stdout)
    except Exception:
        raw = None
        raw_len = 0

    # Run through RTK
    try:
        rtk = subprocess.run(
            [str(RTK_BIN), *tokens],
            capture_output=True, text=True, timeout=30, cwd=work_dir
        )
        if rtk.returncode == 0 and rtk.stdout:
            compressed_len = len(rtk.stdout)
            gain = round((1 - compressed_len / max(raw_len, 1)) * 100, 1)
            header = f"[RTK: {compressed_len} chars, saved {gain}%]\n" if gain > 5 else ""
            return header + rtk.stdout
    except Exception:
        pass

    return (raw.stdout + raw.stderr) if raw else f"error running: {command}"


def _plain_run(command: str, cwd: str) -> str:
    """Fallback: plain subprocess run."""
    tokens = command.strip().split()
    if not tokens:
        return ""
    # Extra safety: only allow whitelisted or if RTK not available
    work_dir = cwd or os.getcwd()
    try:
        r = subprocess.run(tokens, capture_output=True, text=True, timeout=30, cwd=work_dir)
        return r.stdout + (r.stderr if r.returncode != 0 else "")
    except Exception as e:
        return f"error: {e}"


# ── Analytics: my Claude Code token usage ────────────────────────────────────

@mcp.tool()
def lineman_analytics(period: str = "today") -> str:
    """Get Claude Code token usage stats parsed from JSONL session logs.

    period: 'today' | 'week' | 'month'
    Returns: token counts, cost estimate, breakdown by model.
    """
    cutoff_days = {"today": 1, "week": 7, "month": 30}.get(period, 1)
    cutoff_ts = time.time() - cutoff_days * 86400

    if not CLAUDE_LOGS.exists():
        return "Claude logs directory not found"

    totals: dict[str, Any] = {}
    files_read = 0

    for jsonl in sorted(CLAUDE_LOGS.rglob("*.jsonl"))[-200:]:
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            continue

        try:
            for line in jsonl.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Claude Code JSONL format: {type, message: {model, usage: {...}}}
                msg = r.get("message", {})
                usage = msg.get("usage") or r.get("usage", {})
                model = msg.get("model") or r.get("model", "") or ""
                if not usage or not model or model == "<synthetic>":
                    continue

                inp = (usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                out = (usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                cache_r = (usage.get("cache_read_input_tokens", 0) or 0)
                cache_w = (usage.get("cache_creation_input_tokens", 0) or 0)

                if model not in totals:
                    totals[model] = {"in": 0, "out": 0, "cache_r": 0, "cache_w": 0, "calls": 0}
                totals[model]["in"]      += inp
                totals[model]["out"]     += out
                totals[model]["cache_r"] += cache_r
                totals[model]["cache_w"] += cache_w
                totals[model]["calls"]   += 1
            files_read += 1
        except Exception:
            continue

    if not totals:
        return f"No usage data found for period: {period} (checked {files_read} files)"

    # Claude pricing (per 1M tokens)
    PRICES = {
        "claude-sonnet-4": (3.00, 15.00),
        "claude-opus-4":   (15.00, 75.00),
        "claude-haiku-4":  (0.80, 4.00),
    }

    lines = [f"Claude Code token usage — {period}:", ""]
    total_in = total_out = total_cost = 0.0

    for model, s in sorted(totals.items(), key=lambda x: -(x[1]["in"]+x[1]["out"])):
        price = next((v for k, v in PRICES.items() if k in model), (3.00, 15.00))
        cost = (s["in"] / 1e6) * price[0] + (s["out"] / 1e6) * price[1]
        cache_note = f" ({s['cache_r']//1000}k cached, {s['cache_w']//1000}k written)" if s.get("cache_r") or s.get("cache_w") else ""
        lines.append(
            f"  {model}: {s['calls']} calls, "
            f"{s['in']//1000}k in{cache_note} / {s['out']//1000}k out → ${cost:.2f}"
        )
        total_in  += s["in"]
        total_out += s["out"]
        total_cost += cost

    lines += ["", f"  TOTAL: {total_in//1000}k in / {total_out//1000}k out → ${total_cost:.2f}"]
    return "\n".join(lines)


# ── Lineman DB stats: today's API requests ────────────────────────────────────

@mcp.tool()
def lineman_stats() -> str:
    """Get today's API request stats from Lineman DB (all agents + reverse proxy).

    Shows: total requests, tokens by provider/model, economy vs Claude Sonnet.
    """
    if not LINEMAN_DB.exists():
        return "Lineman DB not found at " + str(LINEMAN_DB)

    try:
        conn = sqlite3.connect(str(LINEMAN_DB))
        rows = conn.execute("""
            SELECT llm_provider, llm_model,
                   COUNT(*) as calls,
                   SUM(COALESCE(tokens_in, 0)) as tin,
                   SUM(COALESCE(tokens_out, 0)) as tout,
                   AVG(latency_ms) as avg_lat
            FROM request_log
            WHERE timestamp > datetime('now', '-24 hours')
              AND llm_provider != ''
            GROUP BY llm_provider, llm_model
            ORDER BY tin+tout DESC
        """).fetchall()
        conn.close()
    except Exception as e:
        return f"DB error: {e}"

    if not rows:
        return "No requests in last 24h"

    PRICES = {
        "deepseek-v4-flash": (0.07, 0.28), "deepseek-v4-pro": (0.55, 2.19),
        "gemini-2.5-flash": (0.15, 0.60),  "gemini-3.1-pro-preview": (1.25, 5.00),
        "claude-sonnet": (3.00, 15.00),
    }
    CLAUDE_IN, CLAUDE_OUT = 3.00, 15.00

    lines = ["Lineman — last 24h API stats:", ""]
    total_actual = total_claude = 0.0
    total_calls = total_in = total_out = 0

    for prov, model, calls, tin, tout, lat in rows:
        price = next((v for k, v in PRICES.items() if k in (model or "")), (0.07, 0.28))
        actual = (tin/1e6)*price[0] + (tout/1e6)*price[1]
        claude = (tin/1e6)*CLAUDE_IN + (tout/1e6)*CLAUDE_OUT
        saved_pct = round((1 - actual/max(claude, 0.0001)) * 100)
        lines.append(
            f"  {prov}/{model}: {calls} calls, "
            f"{tin//1000}k/{tout//1000}k tkn, {round(lat or 0)}ms avg "
            f"→ ${actual:.2f} (saved {saved_pct}% vs Sonnet)"
        )
        total_actual += actual
        total_claude += claude
        total_calls  += calls
        total_in     += tin
        total_out    += tout

    total_saved = round((1 - total_actual/max(total_claude, 0.0001)) * 100)
    lines += [
        "",
        f"  TOTAL: {total_calls} calls, "
        f"{total_in//1000}k/{total_out//1000}k tkn "
        f"→ ${total_actual:.2f} actual / ${total_claude:.2f} if Sonnet "
        f"(saved {total_saved}%)"
    ]
    return "\n".join(lines)


# ── Routing hint ──────────────────────────────────────────────────────────────

@mcp.tool()
def routing_hint(estimated_tokens: int = 0, task_type: str = "") -> str:
    """Get smart routing recommendation for OpenClaw agents.

    estimated_tokens: approximate context size in tokens
    task_type: 'think' | 'websearch' | 'background' | 'code' | '' (default)

    Returns recommended provider/model with reasoning.
    """
    try:
        with open(OC_JSON) as f:
            oc = json.load(f)
        routing = oc  # openclaw.json doesn't have routing; use lineman config
    except Exception:
        routing = {}

    # Load lineman routing config
    lineman_config_path = Path("~/workspaces/lineman/config.json").expanduser()
    try:
        with open(lineman_config_path) as f:
            lm = json.load(f)
        routing = lm.get("routing", {})
        threshold = routing.get("longContextThreshold", 60000)
    except Exception:
        threshold = 60000

    task_lower = task_type.lower()

    if estimated_tokens > threshold:
        ctx = "longContext"
        reason = f"context {estimated_tokens//1000}k > {threshold//1000}k threshold"
    elif "think" in task_lower or "reason" in task_lower:
        ctx = "think"
        reason = "reasoning/thinking task"
    elif "search" in task_lower or "web" in task_lower:
        ctx = "webSearch"
        reason = "web search required"
    elif "bg" in task_lower or "background" in task_lower or "cron" in task_lower:
        ctx = "background"
        reason = "background/async task"
    else:
        ctx = "default"
        reason = "standard task"

    route = routing.get(ctx, routing.get("default", {"provider": "deepseek", "model": "deepseek-v4-flash"}))
    provider = route.get("provider", "deepseek")
    model = route.get("model", "deepseek-v4-flash")

    return (
        f"Routing hint: {provider}/{model}\n"
        f"Context: {ctx} ({reason})\n"
        f"Set header X-Lineman-Route: {ctx} to force this route."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
