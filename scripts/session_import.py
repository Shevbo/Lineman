#!/usr/bin/env python3
"""Импорт ~/.claude/projects/*/session_*.jsonl в request_log Lineman.

Закрывает слепую зону: 99% LLM-вызовов идут через Claude CLI с CONNECT-туннелем,
Lineman не видит тела/токенов. Эти данные есть в локальных session-файлах
Claude Code — берём их и заливаем в request_log как `source_agent=claude-cli:<cwd>`.

Идемпотентность: ключ дедупа = (session_id, message_index). Метка хранится
в ~/.cache/lineman-session-import-state.json (last_processed_offset per file).

Cron: 0 */2 * * * /usr/bin/python3 .../session_import.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS = Path(__file__).resolve().parent
DB = THIS.parent / "lineman.db"
SESSIONS_ROOT = Path.home() / ".claude/projects"
STATE = Path.home() / ".cache/lineman-session-import-state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _agent_from_cwd(cwd: str) -> str:
    """Простая эвристика: имя последнего компонента cwd → source_agent.
    e.g. /home/shectory/workspaces/infra/lineman → 'lineman-cli'.
    """
    if not cwd:
        return "claude-cli"
    base = Path(cwd).name or "home"
    return f"claude-cli:{base}"


def _llm_provider_from_model(model: str) -> str:
    if not model:
        return ""
    m = model.lower()
    if "claude" in m or "opus" in m or "sonnet" in m or "haiku" in m:
        return "anthropic"
    if "gemini" in m or "google" in m:
        return "google"
    if "deepseek" in m:
        return "deepseek"
    if "gpt" in m or "o1" in m or "o3" in m:
        return "openai"
    return ""


def process_file(path: Path, start_offset: int) -> tuple[int, int]:
    """Возвращает (new_offset, rows_inserted)."""
    inserted = 0
    new_offset = start_offset
    con = sqlite3.connect(str(DB))
    try:
        with path.open() as fh:
            fh.seek(start_offset)
            # `for line in fh` блокирует tell(); используем readline в цикле.
            while True:
                line = fh.readline()
                if not line:
                    new_offset = fh.tell()
                    break
                new_offset = fh.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                tin = int(u.get("input_tokens") or 0)
                tout = int(u.get("output_tokens") or 0)
                cache_r = int(u.get("cache_read_input_tokens") or 0)
                cache_c = int(u.get("cache_creation_input_tokens") or 0)
                model = msg.get("model") or ""
                if model == "<synthetic>" or (tin == 0 and tout == 0 and cache_r == 0):
                    continue  # пустой/синтетический pass
                ts = d.get("timestamp") or datetime.now(timezone.utc).isoformat()
                provider = _llm_provider_from_model(model)
                agent = _agent_from_cwd(d.get("cwd", ""))
                effective_in = tin + cache_r + cache_c
                con.execute(
                    """INSERT INTO request_log
                       (timestamp, source_host, source_agent, llm_provider, llm_model,
                        tokens_in, tokens_out, cache_hit, route_applied, status_code)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (ts, "smain", agent, provider, model,
                     effective_in, tout, 1 if cache_r > 0 else 0,
                     "session-import", 200),
                )
                inserted += 1
        con.commit()
    finally:
        con.close()
    return new_offset, inserted


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1
    state = _load_state()
    files = list(SESSIONS_ROOT.rglob("*.jsonl"))
    total_inserted = 0
    total_files = 0
    for f in files:
        key = str(f)
        try:
            mtime = f.stat().st_mtime
        except Exception:
            continue
        prev = state.get(key, {"offset": 0, "mtime": 0})
        # Если файл укоротился (ротация) — начнём с 0
        if f.stat().st_size < prev.get("offset", 0):
            prev = {"offset": 0, "mtime": 0}
        # Если ничего нового — пропустить
        if mtime <= prev.get("mtime", 0) and f.stat().st_size <= prev.get("offset", 0):
            continue
        try:
            new_offset, inserted = process_file(f, prev.get("offset", 0))
        except Exception as e:
            print(f"[session-import] error {f.name}: {e}", file=sys.stderr)
            continue
        state[key] = {"offset": new_offset, "mtime": mtime}
        total_inserted += inserted
        if inserted > 0:
            total_files += 1
    _save_state(state)
    print(f"[session-import] {total_inserted} rows imported from {total_files} files (of {len(files)} scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
