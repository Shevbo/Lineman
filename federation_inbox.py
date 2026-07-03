"""Файловый inbox федерации — fallback для push-доставки сообщений.

История: Lineman исторически толкал сообщения в локальных smain-агентов через
`openclaw agent --agent <id> --message <text> --json` (subprocess). После
ликвидации Tank (2026-06-16) openclaw CLI снесён, и этот вызов начал валить
HTTP 500 для всех 13 агентов в `node_map`. Параллельно агенты вне `node_map`
(career-bot, garden-claude, codex) получали HTTP 404 — у Lineman не было
catch-all'а.

Решение: единый файловый inbox `~/.federation-inbox/<agent_id>/inbox.jsonl`.
Любой агент знает где забрать свои сообщения — независимо от того в node_map
он или нет, и независимо от установлен openclaw CLI или нет.

Формат строки JSONL:
    {"id": <int>, "ts": "<ISO8601>", "from": "<sender_id>", "to": "<agent_id>",
     "message": "<text>", "via": "lineman-file-fallback"}

Также пишет в `~/klod-access/delivery_log.jsonl` — централизованный журнал
доставок, источник правды для ежедневного messaging-отчёта (см.
scripts/daily_messaging_report.py).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INBOX_ROOT = Path(os.path.expanduser("~/.federation-inbox"))
DELIVERY_LOG = Path(os.path.expanduser("~/klod-access/delivery_log.jsonl"))
_AGENT_ID_OK = re.compile(r"^[A-Za-z0-9_.@\-]{1,64}$")


def _safe_agent_id(agent_id: str) -> str | None:
    """Возвращает agent_id если он безопасен для использования как имя
    директории (буквы/цифры/`_.@-`, до 64 символов). Иначе None.

    Защита от path traversal: даже если кто-то пришлёт `../` в URL'е,
    мы откажем до создания директории."""
    if not agent_id or not _AGENT_ID_OK.match(agent_id):
        return None
    if agent_id in (".", "..") or "/" in agent_id or "\\" in agent_id:
        return None
    return agent_id


def _agent_dir(agent_id: str) -> Path:
    return INBOX_ROOT / agent_id


def _next_id(counter_path: Path) -> int:
    try:
        cur = int(counter_path.read_text().strip())
    except Exception:
        cur = 0
    nxt = cur + 1
    counter_path.write_text(str(nxt))
    return nxt


def deliver_to_local_agent(
    agent_id: str,
    from_id: str,
    message: str,
    in_node_map: bool,
) -> dict[str, Any]:
    """Записывает сообщение в файловый inbox агента.

    Возвращает dict вида:
      {"status": "ok", "id": 42, "to": "nurse", "via": "lineman-file-fallback",
       "inbox_path": "/home/.../inbox.jsonl"}
    либо
      {"status": "error", "message": "<reason>"}

    in_node_map=True означает: агент явно зарегистрирован в config.json
    `agents.node_map`. Влияет только на поле `via` для трассировки —
    физически путь одинаковый. Это нужно для daily-отчёта: дифференцируем
    push в известных агентов vs catch-all для неизвестных."""
    safe = _safe_agent_id(agent_id)
    if not safe:
        return {"status": "error", "message": f"Unsafe agent_id: {agent_id!r}"}
    sender = _safe_agent_id(from_id) or "(unknown)"
    if not isinstance(message, str) or not message.strip():
        return {"status": "error", "message": "Empty message"}

    adir = _agent_dir(safe)
    try:
        adir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"status": "error", "message": f"Cannot create inbox dir: {e}"}

    counter = adir / "counter.txt"
    inbox = adir / "inbox.jsonl"
    msg_id = _next_id(counter)
    ts = datetime.now(timezone.utc).isoformat()
    via = "lineman-file-node-map" if in_node_map else "lineman-file-catchall"

    record = {
        "id": msg_id,
        "ts": ts,
        "from": sender,
        "to": safe,
        "message": message,
        "via": via,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        return {"status": "error", "message": f"Cannot write inbox: {e}"}

    # delivery_log — общий журнал для отчётов, best-effort (не валим доставку)
    try:
        DELIVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DELIVERY_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

    return {
        "status": "ok",
        "id": msg_id,
        "to": safe,
        "from": sender,
        "ts": ts,
        "via": via,
        "inbox_path": str(inbox),
    }


def read_inbox(agent_id: str, since_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    """Читает inbox агента, возвращает записи с id > since_id."""
    safe = _safe_agent_id(agent_id)
    if not safe:
        return []
    inbox = _agent_dir(safe) / "inbox.jsonl"
    if not inbox.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(inbox, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec.get("id"), int) and rec["id"] > since_id:
                    out.append(rec)
                    if len(out) >= limit:
                        break
    except Exception:
        return out
    return out
