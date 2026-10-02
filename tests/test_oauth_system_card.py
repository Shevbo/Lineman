"""Визитка Claude Code подставляется Lineman вместе с OAuth-токеном.

Токен подписки обслуживает запросы, которые выглядят как Claude Code. Клиент со
своим системным промптом без неё устойчиво получает rate_limit_error там, где тот
же запрос с визиткой проходит: 2026-10-01 агент openclaw на sdev получил пять
отказов подряд, а прямая проба с визиткой в те же минуты отвечала 200.

Подстановка живёт в Lineman рядом с инжектом токена: он единственный, кто знает
про требования OAuth, и так совместимость получают все клиенты разом — openclaw,
внешние агенты по ltx-паттерну, будущие интеграции.

Главное, что проверяется: чужой системный промпт не теряется. Мы переписываем тело
чужого запроса, и молча проглоченный промпт агента был бы хуже отказа по лимиту —
агент продолжал бы работать, но не тем, кем себя считает.
"""
import json

from reverse_proxy import ANTHROPIC_OAUTH_CARD, ensure_oauth_system_card


def body(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def parsed(raw):
    return json.loads(raw)


# ------------------------------------------------------------------ добавление
def test_визитка_появляется_когда_system_нет():
    out = parsed(ensure_oauth_system_card(body({"model": "opus", "messages": []})))
    assert out["system"] == [{"type": "text", "text": ANTHROPIC_OAUTH_CARD}]


def test_строковый_system_сохраняется_вторым_блоком():
    """Клиент мог прислать system строкой — его промпт обязан уцелеть."""
    out = parsed(ensure_oauth_system_card(
        body({"system": "Ты Клод, инженер федерации.", "messages": []})))
    assert out["system"][0]["text"] == ANTHROPIC_OAUTH_CARD
    assert out["system"][1]["text"] == "Ты Клод, инженер федерации."


def test_список_блоков_сохраняется_целиком():
    out = parsed(ensure_oauth_system_card(body({
        "system": [{"type": "text", "text": "первый"},
                   {"type": "text", "text": "второй"}],
        "messages": [],
    })))
    assert [b["text"] for b in out["system"]] == [
        ANTHROPIC_OAUTH_CARD, "первый", "второй"]


def test_визитка_идёт_именно_первой():
    """Anthropic смотрит начало system — глубже строка уже не спасает."""
    out = parsed(ensure_oauth_system_card(
        body({"system": [{"type": "text", "text": "чужое"}], "messages": []})))
    assert out["system"][0]["text"] == ANTHROPIC_OAUTH_CARD


# ------------------------------------------------------------- без дублирования
def test_повторная_подстановка_ничего_не_добавляет():
    once = ensure_oauth_system_card(body({"messages": []}))
    twice = ensure_oauth_system_card(once)
    assert parsed(twice)["system"] == parsed(once)["system"]
    assert twice == once, "тело не должно меняться впустую"


def test_клиент_со_своей_визиткой_не_получает_вторую():
    """Claude Code шлёт её сам — дубль раздувал бы каждый его запрос."""
    original = body({"system": [{"type": "text", "text": ANTHROPIC_OAUTH_CARD},
                                {"type": "text", "text": "и ещё"}],
                     "messages": []})
    assert ensure_oauth_system_card(original) == original


def test_визитка_внутри_строкового_system_тоже_считается():
    original = body({"system": ANTHROPIC_OAUTH_CARD + "\n\nи дальше своё",
                     "messages": []})
    assert ensure_oauth_system_card(original) == original


# --------------------------------------------------------------- не навреди
def test_остальное_тело_не_меняется():
    src = {"model": "claude-opus-4-8", "max_tokens": 64, "stream": True,
           "messages": [{"role": "user", "content": "вопрос"}],
           "tools": [{"name": "grep"}]}
    out = parsed(ensure_oauth_system_card(body(src)))
    for key in ("model", "max_tokens", "stream", "messages", "tools"):
        assert out[key] == src[key], key


def test_не_json_проходит_нетронутым():
    """Через reverse-proxy идёт и то, что телом JSON не является."""
    raw = b"\x89PNG\r\n\x1a\n\x00\x00"
    assert ensure_oauth_system_card(raw) == raw


def test_пустое_тело_не_роняет():
    assert ensure_oauth_system_card(b"") == b""


def test_json_не_объект_проходит_нетронутым():
    raw = '["список", "а не объект"]'.encode("utf-8")
    assert ensure_oauth_system_card(raw) == raw


def test_неизвестная_форма_system_не_трогается():
    """Лучше оставить как есть, чем сломать запрос догадкой о формате."""
    raw = body({"system": {"непонятный": "объект"}, "messages": []})
    assert ensure_oauth_system_card(raw) == raw


def test_русский_текст_не_экранируется_в_юникод_коды():
    """ensure_ascii сломал бы читаемость промптов в логах и в самом запросе."""
    out = ensure_oauth_system_card(body({"system": "Клод", "messages": []}))
    assert "Клод".encode("utf-8") in out
