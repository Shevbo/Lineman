"""Повторы при лимите подписки и честный отказ вместо тихой деградации.

Инцидент 2026-09-18. Лимит подписки плавающий: срабатывает на всплеске и отпускает
за секунды. Замер — из восьми запросов подряд 429 получил только первый. Claude Code
это переживает, потому что повторяет попытку; klod_ask сдавался с первого отказа и
уводил агента на слабую модель. В итоге Клод отвечал федерации хуже, чем позволяет
подписка, и никто об этом не знал.

Решение Бориса: повторять с отступом 2/4/8/16, а исчерпав попытки — отказывать
внятно, НЕ подменяя модель.
"""
import asyncio

import pytest

import klod_ask
from proxy_server import ProxyServer


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def srv(monkeypatch):
    server = ProxyServer.__new__(ProxyServer)
    # Отступы обнуляем: проверяем логику повторов, а не умение ждать полминуты.
    monkeypatch.setattr(ProxyServer, "_KLOD_ASK_RETRY_BACKOFF_S", (0, 0, 0, 0))
    return server


def attach(srv, monkeypatch, outcomes):
    """Подменить одиночную попытку заранее заданной чередой исходов."""
    calls = []

    async def fake_once(path, body, headers, provider):
        calls.append(path)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, 12.0

    monkeypatch.setattr(srv, "_klod_ask_invoke_once", fake_once, raising=False)
    return calls


def limit():
    return RuntimeError('upstream HTTP 429: {"type":"error",'
                        '"error":{"type":"rate_limit_error","message":"Error"}}')


# ------------------------------------------------------------------- повторы
def test_первый_отказ_по_лимиту_не_роняет_запрос(srv, monkeypatch):
    calls = attach(srv, monkeypatch, [limit(), "ответ старшей модели"])
    text, _ = run(srv._klod_ask_invoke("/p", {}, {}, "anthropic"))
    assert text == "ответ старшей модели"
    assert len(calls) == 2, "после 429 обязана быть вторая попытка"


def test_повторов_ровно_пять_попыток(srv, monkeypatch):
    """Четыре отступа 2/4/8/16 — это исходная попытка плюс четыре повтора."""
    calls = attach(srv, monkeypatch, [limit()])
    with pytest.raises(RuntimeError) as exc:
        run(srv._klod_ask_invoke("/p", {}, {}, "anthropic"))
    assert len(calls) == 5
    assert "rate_limited" in str(exc.value)


def test_успех_на_последней_попытке_засчитывается(srv, monkeypatch):
    calls = attach(srv, monkeypatch,
                   [limit(), limit(), limit(), limit(), "успел"])
    text, _ = run(srv._klod_ask_invoke("/p", {}, {}, "anthropic"))
    assert text == "успел"
    assert len(calls) == 5


def test_отступы_именно_2_4_8_16():
    """Цифры заданы Борисом; суммарные 30с должны укладываться в таймаут клиента."""
    assert ProxyServer._KLOD_ASK_RETRY_BACKOFF_S == (2, 4, 8, 16)
    assert sum(ProxyServer._KLOD_ASK_RETRY_BACKOFF_S) == 30


def test_ждём_между_попытками_а_не_долбим_подряд(monkeypatch):
    """Без пауз повторы только усугубили бы всплеск, из-за которого лимит и сработал."""
    srv = ProxyServer.__new__(ProxyServer)
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    attach(srv, monkeypatch, [limit()])
    with pytest.raises(RuntimeError):
        run(srv._klod_ask_invoke("/p", {}, {}, "anthropic"))
    assert slept == [2, 4, 8, 16]


# --------------------------------------------------------- не-лимитные ошибки
def test_обычная_ошибка_не_повторяется(srv, monkeypatch):
    """Повторять сломанный запрос бессмысленно: ответ не изменится."""
    calls = attach(srv, monkeypatch, [RuntimeError("upstream HTTP 400: bad request")])
    with pytest.raises(RuntimeError) as exc:
        run(srv._klod_ask_invoke("/p", {}, {}, "anthropic"))
    assert len(calls) == 1
    assert "rate_limited" not in str(exc.value)


def test_таймаут_не_повторяется(srv, monkeypatch):
    calls = attach(srv, monkeypatch, [RuntimeError("upstream timeout (>90s) on /p")])
    with pytest.raises(RuntimeError):
        run(srv._klod_ask_invoke("/p", {}, {}, "anthropic"))
    assert len(calls) == 1


# ------------------------------------------------------- распознавание лимита
def test_лимит_узнаётся_по_обеим_приметам():
    assert klod_ask.is_rate_limited("upstream HTTP 429: ...")
    assert klod_ask.is_rate_limited('{"type":"rate_limit_error"}')
    assert not klod_ask.is_rate_limited("upstream HTTP 500: boom")
    assert not klod_ask.is_rate_limited("")


def test_сообщение_об_отказе_называет_модель_и_не_врёт():
    msg = klod_ask.rate_limit_message("claude-opus-4-8")
    assert "claude-opus-4-8" in msg
    assert "Попробуйте позже" in msg
    # Главное: ни намёка на то, что ответ дала другая модель.
    for weaker in ("haiku", "deepseek", "gemini", "flash"):
        assert weaker not in msg.lower()
