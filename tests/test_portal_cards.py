"""Карточки агентов федерации.

Онбординг каждого агента зовёт check_portal_card.sh: тот спрашивает карточку, а при
404 заводит её сам. Ручек не существовало вовсе, поэтому скрипт всегда сваливался в
запасной путь и слал Клоду «portal-card-missing». Клод заводил тикет, никто его не
брал, агент при следующем онбординге репортил снова — к 2026-09-22 в трекере лежали
четыре такие задачи, старейшая с 29 июня.

Тест держит контракт, которого ждёт скрипт, и главное свойство: повторный онбординг
не должен ни падать, ни плодить дубликаты.
"""
import json

import pytest

import portal_cards


@pytest.fixture
def store(tmp_path):
    return tmp_path / "cards.json"


# ------------------------------------------------------------------ заведение
def test_карточки_нет_пока_её_не_завели(store):
    assert portal_cards.get_card("career-bot", store) is None
    assert portal_cards.list_cards(store) == []


def test_первое_заведение_отдаёт_201(store):
    code, card = portal_cards.upsert_card(
        {"agent_id": "career-bot", "node": "smain", "purpose": "менеджер карьеры"},
        store)
    assert code == 201
    assert card["agent_id"] == "career-bot"
    assert card["purpose"] == "менеджер карьеры"


def test_повторный_онбординг_не_плодит_дубликаты(store):
    body = {"agent_id": "career-bot", "node": "smain", "purpose": "первая версия"}
    assert portal_cards.upsert_card(body, store)[0] == 201
    code, _ = portal_cards.upsert_card(body, store)
    assert code == 200, "второй раз это обновление, а не создание"
    assert len(portal_cards.list_cards(store)) == 1


def test_обновление_дополняет_а_не_затирает(store):
    portal_cards.upsert_card(
        {"agent_id": "klod-stl", "node": "vibe", "purpose": "торговый стенд"}, store)
    portal_cards.upsert_card({"agent_id": "klod-stl", "repo_path": "/home/x"}, store)
    card = portal_cards.get_card("klod-stl", store)
    assert card["purpose"] == "торговый стенд", "старое поле не должно пропасть"
    assert card["repo_path"] == "/home/x"


def test_агент_с_узлом_и_без_это_один_агент(store):
    """Онбординг зовёт скрипт то как `eshkola`, то как `eshkola@hoster`."""
    portal_cards.upsert_card({"agent_id": "eshkola@hoster", "node": "hoster"}, store)
    code, _ = portal_cards.upsert_card({"agent_id": "eshkola", "node": "hoster"}, store)
    assert code == 200
    assert len(portal_cards.list_cards(store)) == 1
    assert portal_cards.get_card("eshkola@hoster", store) is not None


# --------------------------------------------------------------- дисциплина полей
def test_без_agent_id_отказ(store):
    code, body = portal_cards.upsert_card({"node": "smain"}, store)
    assert code == 400 and "agent_id" in body["error"]


def test_мусорный_agent_id_отказ(store):
    for bad in ("../../etc/passwd", "агент с пробелом", "a" * 70, ""):
        code, _ = portal_cards.upsert_card({"agent_id": bad}, store)
        assert code == 400, bad


def test_посторонние_поля_игнорируются(store):
    """Карточка — визитка, а не свалка: чужие ключи внутрь не попадают."""
    portal_cards.upsert_card(
        {"agent_id": "probe-agent", "node": "pi", "token": "секрет",
         "произвольное": "поле"}, store)
    card = portal_cards.get_card("probe-agent", store)
    assert "token" not in card and "произвольное" not in card


def test_длинные_значения_обрезаются(store):
    portal_cards.upsert_card(
        {"agent_id": "agentin", "purpose": "ю" * 5000}, store)
    assert len(portal_cards.get_card("agentin", store)["purpose"]) <= 500


# --------------------------------------------------------------------- хранилище
def test_битый_файл_не_роняет_чтение(store):
    store.write_text("{это не json", encoding="utf-8")
    assert portal_cards.list_cards(store) == []
    assert portal_cards.upsert_card({"agent_id": "x", "node": "smain"}, store)[0] == 201


def test_файл_остаётся_читаемым_json(store):
    portal_cards.upsert_card({"agent_id": "eshkola", "node": "hoster"}, store)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert "eshkola" in data


def test_список_отсортирован_по_имени(store):
    for who in ("probe-agent", "agentin", "career-bot"):
        portal_cards.upsert_card({"agent_id": who, "node": "smain"}, store)
    names = [c["agent_id"] for c in portal_cards.list_cards(store)]
    assert names == sorted(names)
