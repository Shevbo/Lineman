#!/usr/bin/env python3
"""Federation Sweep — фоновый обход кодовой базы для оценки качества.

Когда Lazy Queue простаивает (нет user-задач), этот скрипт находит **один**
файл/директорию и постит в Lazy Queue задачу низкого приоритета (5):

- sweep_doc       — оценка качества документации (README/AGENTS/CLAUDE.md)
- sweep_deadcode  — поиск мёртвого кода (неиспользуемые функции)
- sweep_hardcode  — поиск hardcoded URL/IP/path
- sweep_leaks     — паттерны утечек (api_key/sk-/Bearer/AIza/TG-bot)
- sweep_secsan    — простая security-проверка (eval, shell=True, sql concat)

Запускается из cron каждые 10-15 минут. Лимит: не более N активных
sweep-задач одновременно в очереди, чтобы не забить worker user-задачами.

Cron:
    7,17,27,37,47,57 * * * * /usr/bin/python3 /home/shectory/workspaces/infra/lineman/scripts/federation_sweep.py
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

THIS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(THIS))
import lazy_queue as lq  # noqa: E402

LINEMAN = "http://127.0.0.1:9090"
MAX_QUEUED_SWEEP = 5  # не плодим
WORKSPACES = [
    Path.home() / "workspaces/infra/lineman",
    Path.home() / "workspaces/infra/censor",
    Path.home() / "workspaces/eshkola",
    Path.home() / "workspaces/career-bot",
    Path.home() / "workspaces/nurse",
    Path.home() / "keymaster",
]
STATE = Path.home() / ".cache/lineman-sweep-state.json"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _queue_size() -> int:
    try:
        req = urllib.request.Request(f"{LINEMAN}/api/queue/lazy?status=queued&limit=50")
        with _NOPROXY.open(req, timeout=4) as r:
            return len(json.loads(r.read()).get("jobs", []))
    except Exception:
        return 999  # paranoid: skip if can't see queue


def _lazy_busy() -> bool:
    """Есть ли user-задачи (priority 1-4)? Если да — не плодим sweep."""
    try:
        req = urllib.request.Request(f"{LINEMAN}/api/queue/lazy?status=queued&limit=10")
        with _NOPROXY.open(req, timeout=4) as r:
            jobs = json.loads(r.read()).get("jobs", [])
        return any(int(j.get("priority", 5)) < 5 for j in jobs)
    except Exception:
        return True


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _pick_next_file(state: dict) -> tuple[str, Path] | None:
    """Выбрать следующий файл для проверки, round-robin по типу задачи."""
    kinds = ["sweep_doc", "sweep_deadcode", "sweep_hardcode", "sweep_leaks", "sweep_secsan"]
    kind_idx = state.get("kind_idx", 0)
    kind = kinds[kind_idx % len(kinds)]
    state["kind_idx"] = (kind_idx + 1) % len(kinds)

    if kind == "sweep_doc":
        candidates: list[Path] = []
        for ws in WORKSPACES:
            if ws.exists():
                candidates += list(ws.glob("*.md"))
                candidates += list(ws.glob(".claude/memory/*.md"))
        if not candidates:
            return None
        return kind, random.choice(candidates)

    # для code-проверок берём .py файлы (Lineman, Censor, eshkola и пр.)
    candidates = []
    for ws in WORKSPACES:
        if not ws.exists():
            continue
        for f in ws.rglob("*.py"):
            if any(s in str(f) for s in (".venv", "__pycache__", "site-packages", ".git", "tests/")):
                continue
            if f.stat().st_size > 50_000:  # пропускаем гигантские
                continue
            candidates.append(f)
    if not candidates:
        return None
    return kind, random.choice(candidates)


PROMPTS: dict[str, str] = {
    "sweep_doc":      "Оцени качество документации этого файла по шкале 1-5 (полнота, актуальность, структура). Назови 3 главных слабых места кратко.",
    "sweep_deadcode": "Найди функции/классы/импорты которые скорее всего не используются (внутри файла или не экспортируются). Возможны false-positive, отмечай confidence.",
    "sweep_hardcode": "Найди hardcoded URL/IP/path/токены/имена пользователей. Каждое: строка, что подозрительно, как параметризовать.",
    "sweep_leaks":    "Найди потенциальные утечки секретов: подстроки api_key/sk-/Bearer/AIza/0-9+:35char (TG bot token), пароли, токены. False-positive отмечай.",
    "sweep_secsan":   "Найди простые security-проблемы: eval/exec, shell=True, SQL string concatenation, открытые file:// load, deserialization без проверки.",
}


def main() -> int:
    if _lazy_busy():
        print("[sweep] queue has user-priority jobs — skip")
        return 0
    if _queue_size() >= MAX_QUEUED_SWEEP:
        print(f"[sweep] queue has >= {MAX_QUEUED_SWEEP} jobs — skip")
        return 0

    state = _load_state()
    pick = _pick_next_file(state)
    if not pick:
        print("[sweep] no candidates")
        return 0
    kind, path = pick
    try:
        content = path.read_text(errors="replace")[:20_000]  # лимит 20K chars
    except Exception as e:
        print(f"[sweep] read {path}: {e}", file=sys.stderr)
        return 1

    prompt = (
        f"Файл: `{path}`\n\n"
        f"```\n{content}\n```\n\n"
        f"{PROMPTS[kind]}\n\n"
        "Формат ответа: краткий markdown, не более 300 слов."
    )

    job_id = lq.submit_job(
        from_agent="federation-sweep", from_node="smain", kind=kind,
        user_prompt=prompt, system_prompt="Ты code reviewer. Отвечай на русском, кратко.",
        max_tokens=400, priority=5, deadline_hint_minutes=120,
    )
    print(f"[sweep] queued kind={kind} job_id={job_id} file={path.relative_to(Path.home())}")
    state.setdefault("history", []).append({"kind": kind, "file": str(path), "job_id": job_id})
    state["history"] = state["history"][-100:]
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
