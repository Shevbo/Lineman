"""Parse OpenClaw agents config → metadata dict for federation dashboard."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_OPENCLAW_JSON = Path.home() / ".openclaw" / "openclaw.json"


def load_agents_meta(
    openclaw_path: Path | str | None = None,
    node_map: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return {agent_id: {id, name, emoji, model, node, description}}.

    node_map: {"smain": ["main", "selfcoder", ...]} — which agents live where.
    """
    path = Path(openclaw_path) if openclaw_path else _OPENCLAW_JSON
    try:
        with open(path) as f:
            oc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    agent_list: list[dict[str, Any]] = oc.get("agents", {}).get("list", [])
    if not isinstance(agent_list, list):
        return {}

    agent_to_node: dict[str, str] = {}
    if node_map:
        for node, ids in node_map.items():
            for aid in ids:
                agent_to_node[aid] = node

    result: dict[str, dict[str, Any]] = {}
    for ag in agent_list:
        aid = ag.get("id", "")
        if not aid:
            continue
        identity = ag.get("identity") or {}
        name = identity.get("name") or ag.get("name") or aid
        emoji = identity.get("emoji", "🤖")
        model_raw = ag.get("model", "")
        model = model_raw.get("primary", "") if isinstance(model_raw, dict) else model_raw
        node = agent_to_node.get(aid, "smain")
        description = _extract_description(ag)
        result[aid] = {
            "id": aid,
            "name": name,
            "emoji": emoji,
            "model": model,
            "node": node,
            "description": description,
        }

    return result


def _extract_description(ag: dict[str, Any]) -> str:
    """Return short text about the agent from any available field."""
    for key in ("system",):
        val = ag.get(key, "")
        if isinstance(val, str) and val:
            return val[:200]
        if isinstance(val, dict):
            text = val.get("text", "") or val.get("content", "")
            if text:
                return str(text)[:200]

    identity = ag.get("identity") or {}
    for key in ("description", "bio", "about"):
        val = identity.get(key, "")
        if val:
            return str(val)[:200]

    return ag.get("name") or ag.get("id", "")
