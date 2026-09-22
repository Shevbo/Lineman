"""Карточки агентов федерации: кто, на каком узле и зачем.

Зачем модуль появился. Онбординг каждого агента вызывает check_portal_card.sh,
который спрашивает GET /api/portal/agents/<id>, а при 404 создаёт карточку через
POST /api/portal/agents. Ручек не существовало вовсе — скрипт всегда сваливался в
запасной путь и слал Клоду письмо «portal-card-missing». Клод заводил тикет, никто
его не брал, агент при следующем онбординге репортил снова. К 2026-09-22 в трекере
лежали четыре такие задачи (career-bot дважды, klod-stl@vibe, probe-agent), а самая
старая ждала с 29 июня.

Хранилище намеренно простое — JSON-файл. Карточек десятки, пишутся они раз в жизни
агента, и заводить ради них таблицу в портале значило бы связать онбординг с базой,
до которой у половины узлов нет доступа.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

DEFAULT_PATH = pathlib.Path(
    os.environ.get("PORTAL_CARDS_FILE",
                   str(pathlib.Path.home() / ".klod/portal_cards.json")))

# Поля, которые принимаем от агента. Всё остальное игнорируем: карточка —
# это визитка, а не свалка произвольных данных.
ALLOWED_FIELDS = ("agent_id", "node", "purpose", "source", "repo_path", "contact")
MAX_FIELD_LEN = 500


def _load(path: pathlib.Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: pathlib.Path, cards: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cards, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)          # замена целиком: полукарточек не бывает


def normalize_id(agent_id: str) -> str:
    """`eshkola@hoster` и `eshkola` — один агент.

    Онбординг зовёт скрипт то с чистым id, то с `agent@node`, и без приведения
    один и тот же агент заводил бы себе по карточке на каждую форму записи.
    """
    return str(agent_id or "").strip().lower().split("@", 1)[0]


def get_card(agent_id: str, path: pathlib.Path | None = None) -> dict | None:
    return _load(path or DEFAULT_PATH).get(normalize_id(agent_id))


def list_cards(path: pathlib.Path | None = None) -> list[dict]:
    cards = _load(path or DEFAULT_PATH)
    return sorted(cards.values(), key=lambda c: str(c.get("agent_id", "")))


def upsert_card(payload: dict[str, Any],
                path: pathlib.Path | None = None) -> tuple[int, dict]:
    """Завести или обновить карточку. Возвращает (http-статус, тело ответа).

    Повторный вызов с теми же данными ничего не портит: онбординг запускают
    многократно, и падать на этом он не должен.
    """
    path = path or DEFAULT_PATH
    agent_id = normalize_id(payload.get("agent_id", ""))
    if not agent_id:
        return 400, {"error": "agent_id обязателен"}
    if len(agent_id) > 64 or not all(c.isalnum() or c in "-_." for c in agent_id):
        return 400, {"error": "agent_id: только буквы, цифры, дефис, точка, подчёркивание"}

    cards = _load(path)
    now = int(time.time())
    card = cards.get(agent_id) or {"agent_id": agent_id, "created_at": now}
    for field in ALLOWED_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            card[field] = value.strip()[:MAX_FIELD_LEN]
    card["agent_id"] = agent_id
    card["updated_at"] = now
    existed = agent_id in cards
    cards[agent_id] = card
    _save(path, cards)
    return (200 if existed else 201), card
