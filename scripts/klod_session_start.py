#!/usr/bin/env python3
"""SessionStart hook для Klod-Access: показывает свежие inbox-сообщения в
additionalContext, обновляет last_seen_id, чтобы не повторяться.

Wired via .claude/settings.json hooks.SessionStart.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

INBOX = Path.home() / "klod-access" / "inbox.jsonl"
LAST_SEEN = Path.home() / "klod-access" / "last_seen_id.txt"
MAX_SHOW = 10
MAX_MSG_CHARS = 400


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    # Consume hook input (we don't use it, but read so the producer doesn't block).
    try:
        sys.stdin.read()
    except Exception:
        pass

    if not INBOX.exists():
        return 0

    last_seen = 0
    if LAST_SEEN.exists():
        try:
            last_seen = int(LAST_SEEN.read_text().strip() or "0")
        except Exception:
            last_seen = 0

    new = []
    with INBOX.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("id", 0) > last_seen:
                new.append(d)

    if not new:
        return 0

    max_id = max(d.get("id", 0) for d in new)
    parts = [f"## Klod-Access inbox ({len(new)} new since id={last_seen})", ""]
    for d in new[-MAX_SHOW:]:
        ts = d.get("ts", "?")
        frm = d.get("from", "?")
        node = d.get("node", "?")
        msg = d.get("message", "")
        if len(msg) > MAX_MSG_CHARS:
            msg = msg[:MAX_MSG_CHARS] + "…"
        parts.append(f"- **id={d.get('id')}** from `{frm}` (node {node}) at {ts}:")
        for L in msg.splitlines():
            parts.append(f"  > {L}")
        parts.append("")
    parts.append(
        f"Read more: `curl http://127.0.0.1:9090/api/agent/klod-access/inbox?since={last_seen}`"
    )
    parts.append(
        "Reply: `curl -X POST 'http://127.0.0.1:9090/api/agent/klod-access/reply?to=<id>&in_reply_to=<n>' --data '<text>'`"
    )
    ctx = "\n".join(parts)

    LAST_SEEN.write_text(str(max_id))

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
