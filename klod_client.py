"""Helper для агентов federation: одна функция, чтобы пожаловаться Klod-Access.

Использование (из любого Python агента):

    from klod_client import complain, notify, ask
    complain("titan", "401 на gemini-flash уже 10 минут")
    # → отправляет жалобу, Lineman автотриаж подтянет ваши последние ошибки
    # из request_log, я (Klod-Access) увижу это в начале следующей сессии.

Также:
    notify("titan", "запустил миграцию X на cloud", node="smain")
    # → просто информационное сообщение, без триажа.

Async вариант: `await async_complain(...)`.

LINEMAN env override: переменная `LINEMAN_URL` (default http://127.0.0.1:9090).
Из WG-узлов используется http://10.66.0.1:9090.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

LINEMAN_URL = os.environ.get("LINEMAN_URL", "http://127.0.0.1:9090")
TIMEOUT_S = 8


def _post(path: str, body: str, params: dict[str, str]) -> dict[str, Any]:
    from urllib.parse import urlencode
    url = f"{LINEMAN_URL}{path}?{urlencode(params)}"
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"error": str(e)}


def complain(agent_id: str, message: str, *, node: str = "smain") -> dict[str, Any]:
    """Послать жалобу Klod-Access (Lineman автоматически приложит triage из БД).

    Возвращает {"status": "ok", "id": N} или {"error": "..."}.
    """
    return _post("/api/agent/klod-access/message",
                 message,
                 {"from": agent_id, "node": node})


def notify(agent_id: str, message: str, *, node: str = "smain") -> dict[str, Any]:
    """Информационное сообщение (НЕ жалоба, без триажа). Можно с любой темой."""
    return _post("/api/agent/klod-access/message",
                 message,
                 {"from": agent_id, "node": node})


def ask(agent_id: str, question: str, *, node: str = "smain") -> dict[str, Any]:
    """Задать вопрос. Klod-Access отвечает в следующую сессию через /reply."""
    return _post("/api/agent/klod-access/message",
                 f"[QUESTION] {question}",
                 {"from": agent_id, "node": node})


# Async helpers (если у вас asyncio loop)
async def async_complain(agent_id: str, message: str, *, node: str = "smain") -> dict[str, Any]:
    import asyncio
    return await asyncio.to_thread(complain, agent_id, message, node=node)


async def async_notify(agent_id: str, message: str, *, node: str = "smain") -> dict[str, Any]:
    import asyncio
    return await asyncio.to_thread(notify, agent_id, message, node=node)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: klod_client.py <agent_id> <message> [complain|notify|ask]")
        sys.exit(2)
    aid, msg = sys.argv[1], sys.argv[2]
    kind = sys.argv[3] if len(sys.argv) > 3 else "complain"
    fn = {"complain": complain, "notify": notify, "ask": ask}.get(kind, complain)
    print(json.dumps(fn(aid, msg), ensure_ascii=False))
