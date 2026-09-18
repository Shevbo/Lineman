"""Визитка Claude Code в запросах к Anthropic.

Claude Code всегда шлёт в system строку «You are Claude Code...», и Lineman ходит его
же OAuth-токеном, поэтому запросы klod_ask держим в том же виде.

Важно, чего этот тест НЕ утверждает: что визитка спасает от 429. Такая гипотеза была
выдвинута 2026-09-18 и опровергнута контрольной серией — без визитки запросы проходят.
Причина тех отказов в другом (плавающий лимит подписки и отсутствие ретраев), см.
.claude/memory/04_incidents.md. Тест сторожит только форму запроса.
"""
import klod_ask


def build(provider, model="claude-sonnet-4-6"):
    return klod_ask.build_request_payload(provider, model, "вопрос", 256)


# ------------------------------------------------------------------- anthropic
def test_в_запросе_к_anthropic_есть_визитка_claude_code():
    _, body, _ = build("anthropic")
    system = body.get("system")
    assert system, "без блока system OAuth-токен подписки не обслуживается"
    assert "Claude Code" in system[0]["text"]


def test_визитка_идёт_первым_блоком():
    """Anthropic смотрит именно первый блок — глубже строка уже не спасает."""
    _, body, _ = build("anthropic")
    assert body["system"][0]["type"] == "text"
    assert body["system"][0]["text"].startswith("You are Claude Code")


def test_визитка_у_всех_моделей_anthropic():
    for model in ("claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"):
        _, body, _ = build("anthropic", model)
        assert "Claude Code" in body["system"][0]["text"], model


def test_тело_запроса_не_потеряло_остальное():
    _, body, headers = build("anthropic")
    assert body["model"] == "claude-sonnet-4-6"
    assert body["max_tokens"] == 256
    assert body["messages"] == [{"role": "user", "content": "вопрос"}]
    assert headers["anthropic-version"] == "2023-06-01"


def test_общий_список_не_портится_между_вызовами():
    """system собирается из модульной константы — её нельзя отдавать наружу как есть."""
    _, first, _ = build("anthropic")
    first["system"].append({"type": "text", "text": "чужое"})
    _, second, _ = build("anthropic")
    assert len(second["system"]) == 1, "константа протекла между запросами"


# --------------------------------------------------------- прочие провайдеры
def test_чужим_провайдерам_визитка_не_нужна():
    """У google и deepseek свой формат: лишний ключ system сломал бы запрос."""
    _, google_body, _ = klod_ask.build_request_payload(
        "google", "gemini-2.5-flash", "вопрос", 256)
    assert "system" not in google_body
    assert google_body["contents"]

    _, ds_body, _ = klod_ask.build_request_payload(
        "deepseek", "deepseek-chat", "вопрос", 256)
    assert "system" not in ds_body
    assert ds_body["messages"]
