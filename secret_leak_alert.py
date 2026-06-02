"""Протокол немедленного оповещения об утечке секрета.

Триггерится в трёх местах:
1. Lineman runtime — когда `secret_mask` нашёл реальный live-секрет
   (т.е. строка совпала с известным значением из Keymaster manifest).
2. Через явный endpoint `/api/keymaster/leak_alert` — любой агент может
   сообщить, что увидел утечку.
3. Federation sweep — sweep_leaks kind находит паттерн в коде.

Действия по триггеру:
- Запись в klod-access inbox с kind='secret_leak'.
- TG-уведомление Борису через Lineman /api/tg/send с пометкой ROTATION_NEEDED.
- Запуск `~/keymaster/skills/auto_rotate.py SECRET_NAME` если возможно
  (для секретов с `rotate_with` ≠ "manual" и работающим скиллом).
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LINEMAN = os.environ.get("LINEMAN_URL", "http://127.0.0.1:9090")
KEYMASTER = os.environ.get("KEYMASTER_URL", "http://127.0.0.1:9093")
BORIS_TG = "36910539"
LEAK_LOG = Path.home() / ".keymaster/leak_alerts.log"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _audit_log(payload: dict) -> None:
    LEAK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LEAK_LOG.open("a") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _tg_send(text: str) -> None:
    try:
        body = json.dumps({"account": "default", "chat_id": BORIS_TG, "text": text}).encode()
        req = urllib.request.Request(
            f"{LINEMAN}/api/tg/send", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        _NOPROXY.open(req, timeout=8).read()
    except Exception:
        pass


def _klod_inbox(text: str, meta: dict) -> None:
    try:
        from urllib.parse import urlencode
        params = urlencode({"from": "secret-leak-alert", "node": "smain"})
        req = urllib.request.Request(
            f"{LINEMAN}/api/agent/klod-access/message?{params}",
            data=text.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/plain"},
        )
        _NOPROXY.open(req, timeout=6).read()
    except Exception:
        pass


def _trigger_auto_rotate(secret_name: str) -> dict:
    """Зовёт keymaster/skills/auto_rotate.py SECRET — без LLM, через CLI скилл.

    Возвращает {triggered: bool, status: ..., output: ...}.
    """
    skill = Path.home() / "keymaster/skills/auto_rotate.py"
    if not skill.exists():
        return {"triggered": False, "reason": "auto_rotate skill not installed"}
    try:
        import subprocess
        proc = subprocess.run(
            ["/usr/bin/python3", str(skill), secret_name],
            capture_output=True, text=True, timeout=60,
        )
        return {
            "triggered": True, "rc": proc.returncode,
            "stdout": (proc.stdout or "")[:400],
            "stderr": (proc.stderr or "")[:400],
        }
    except Exception as e:
        return {"triggered": False, "reason": str(e)}


def report_leak(*, secret_name: str | None, where: str, snippet: str,
                source_agent: str = "?", severity: str = "high") -> dict:
    """Главная точка входа. secret_name может быть None если не атрибутирован.

    where — `request_log:row_id` / `file:path:line` / `chat:agent` / etc.
    """
    ts = datetime.now(timezone.utc).isoformat()
    rec = {
        "ts": ts, "secret_name": secret_name, "where": where,
        "source_agent": source_agent, "severity": severity,
        "snippet_prefix": (snippet or "")[:24] + "***",
    }
    _audit_log(rec)
    # 1) Klod-Access inbox
    _klod_inbox(
        f"[SECRET LEAK detected]\n"
        f"name: {secret_name or '?'} (severity={severity})\n"
        f"where: {where}\n"
        f"prefix: {rec['snippet_prefix']}\n"
        f"source: {source_agent}\n"
        f"ROTATION_NEEDED",
        rec,
    )
    # 2) TG Борису
    _tg_send(
        "🚨 SECRET LEAK\n"
        f"name: {secret_name or '(не атрибутирован)'}\n"
        f"where: {where}\n"
        f"prefix: {rec['snippet_prefix']}\n"
        f"source: {source_agent}\n\n"
        "Ключник сейчас инициирует ротацию (если возможно). "
        "Иначе — пришли новое значение через @ShectoryKeyMasterBot."
    )
    # 3) Auto-rotation, если есть скилл
    if secret_name:
        rec["auto_rotate"] = _trigger_auto_rotate(secret_name)
    return rec


def scan_leak_against_keymaster(text: str) -> str | None:
    """Если в тексте есть точное совпадение с одним из known secret values из
    `~/.keymaster/credentials/*` — вернёт имя секрета. Иначе None.

    Используется в Lineman runtime для отличия настоящей утечки от ложно-положительной
    реакции secret_mask на чужие AIza/sk- из тела пользовательского промпта.
    """
    creds_dir = Path.home() / ".keymaster/credentials"
    if not creds_dir.exists() or not text:
        return None
    for f in creds_dir.iterdir():
        if not f.is_file():
            continue
        try:
            value = f.read_text().strip()
        except Exception:
            continue
        if len(value) < 16:
            continue
        # Точное появление в text
        if value in text:
            return f.name.upper()
    return None
