"""Тесты прокси к индексу fedrag.

Главное, что здесь проверяется, — поведение при беде. Индекс живёт на отдельном узле
и обслуживает запросы по одному, поэтому агент не должен ни вставать из-за недоступного
sdev, ни занимать демон целиком.
"""
import asyncio

import pytest

from fedrag_proxy import FALLBACK_TEXT, FedRagProxy


def run(coro):
    # Свой цикл на вызов: состояние прокси живёт в нём самом, а не в петле событий.
    return asyncio.run(coro)


@pytest.fixture
def proxy():
    return FedRagProxy({"fedrag": {"rate_per_min": 3, "cache_ttl_s": 60}})


# --------------------------------------------------------------------- валидация
def test_пустой_запрос_отвергается(proxy):
    status, body = run(proxy.search("   "))
    assert status == 400
    assert "query" in body["error"]


def test_слишком_длинный_запрос_отвергается(proxy):
    status, body = run(proxy.search("а" * 2001))
    assert status == 400


def test_выключенный_прокси_отдаёт_деградацию_а_не_ошибку(proxy):
    off = FedRagProxy({"fedrag": {"enabled": False}})
    status, body = run(off.search("где логи Lineman"))
    assert status == 200
    assert body["source"] == "disabled"
    assert body["text"] == FALLBACK_TEXT


# ------------------------------------------------------------------------- кэш
def test_повтор_запроса_берётся_из_кэша(proxy, monkeypatch):
    calls = []

    async def fake(query, depth, agent):
        calls.append(query)
        return {"text": "ответ про логи"}

    _patch_upstream(proxy, monkeypatch, fake)

    first = run(proxy.search("где логи Lineman", agent="eshkola"))
    second = run(proxy.search("где логи Lineman", agent="eshkola"))

    assert first[1]["source"] == "ragkit"
    assert second[1]["source"] == "cache"
    assert second[1]["text"] == "ответ про логи"
    assert len(calls) == 1, "второй запрос не должен доходить до демона"


def test_кэш_не_различает_регистр_и_пробелы(proxy, monkeypatch):
    async def fake(query, depth, agent):
        return {"text": "ответ"}

    _patch_upstream(proxy, monkeypatch, fake)
    run(proxy.search("Где Логи Lineman", agent="a"))
    status, body = run(proxy.search("  где логи lineman  ", agent="a"))
    assert body["source"] == "cache"


def test_кэш_не_растёт_сверх_предела(monkeypatch):
    small = FedRagProxy({"fedrag": {"cache_max": 3}})

    async def fake(query, depth, agent):
        return {"text": "x"}

    _patch_upstream(small, monkeypatch, fake)
    for i in range(10):
        run(small.search("запрос %d" % i, agent="a"))
    assert len(small._cache) <= 3


# ------------------------------------------------------------------ лимит на агента
def test_превышение_лимита_даёт_429(proxy, monkeypatch):
    async def fake(query, depth, agent):
        return {"text": "ответ"}

    _patch_upstream(proxy, monkeypatch, fake)

    for i in range(3):                       # rate_per_min = 3
        assert run(proxy.search("запрос %d" % i, agent="шумный"))[0] == 200

    status, body = run(proxy.search("запрос лишний", agent="шумный"))
    assert status == 429
    assert body["retry_after"] == 60


def test_лимит_считается_отдельно_по_агентам(proxy, monkeypatch):
    async def fake(query, depth, agent):
        return {"text": "ответ"}

    _patch_upstream(proxy, monkeypatch, fake)

    for i in range(3):
        run(proxy.search("q%d" % i, agent="шумный"))
    # Спрашиваем НОВЫЙ запрос: повтор ушёл бы в кэш и лимита не коснулся.
    assert run(proxy.search("совсем другой вопрос", agent="шумный"))[0] == 429
    # Сосед не должен страдать от чужого цикла.
    assert run(proxy.search("свой вопрос", agent="тихий"))[0] == 200


def test_ответ_из_кэша_не_тратит_лимит(proxy, monkeypatch):
    async def fake(query, depth, agent):
        return {"text": "ответ"}

    _patch_upstream(proxy, monkeypatch, fake)

    run(proxy.search("один и тот же", agent="agent"))
    for _ in range(10):
        status, body = run(proxy.search("один и тот же", agent="agent"))
        assert status == 200
        assert body["source"] == "cache"


# --------------------------------------------------------------------- деградация
def test_недоступный_индекс_не_роняет_агента(proxy, monkeypatch):
    async def boom(query, depth, agent):
        raise OSError("sdev недоступен")

    _patch_upstream(proxy, monkeypatch, boom)

    status, body = run(proxy.search("что угодно", agent="eshkola"))
    assert status == 200, "агент должен продолжить работу, а не встать"
    assert body["source"] == "fallback"
    assert body["text"] == FALLBACK_TEXT


def test_ошибка_не_кэшируется(proxy, monkeypatch):
    state = {"fail": True}

    async def flaky(query, depth, agent):
        if state["fail"]:
            raise OSError("временно недоступен")
        return {"text": "живой ответ"}

    _patch_upstream(proxy, monkeypatch, flaky)

    assert run(proxy.search("вопрос", agent="a"))[1]["source"] == "fallback"
    state["fail"] = False
    status, body = run(proxy.search("вопрос", agent="a"))
    assert body["source"] == "ragkit", "деградация не должна застревать в кэше"
    assert body["text"] == "живой ответ"


# ------------------------------------------------------------------------ подмена
def _patch_upstream(proxy, monkeypatch, fake):
    """Подменить HTTP-поход в ragkit.

    Подменяется только aiohttp: кэш, лимит на агента и путь деградации остаются
    настоящими, иначе тест проверял бы заглушку, а не поведение прокси.
    """
    import fedrag_proxy as mod

    class _Resp:
        def __init__(self, payload_coro):
            self._coro = payload_coro
            self._payload = None
            self.status = 200

        async def __aenter__(self):
            # Исключение из fake всплывает здесь — как настоящий сетевой сбой.
            # Корутина ждётся ровно один раз, результат отдаётся из json().
            self._payload = await self._coro
            return self

        async def json(self):
            return self._payload

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, url, json=None):
            body = json or {}
            return _Resp(fake(body.get("query", ""), body.get("depth", ""), ""))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod.aiohttp, "ClientSession",
                        lambda *a, **kw: _Session(), raising=False)


# ------------------------------------------------- аварийный запас: устаревший кэш
def _age_cache(proxy, seconds):
    """Состарить записи кэша.

    Менять cache_ttl_s постфактум бесполезно: срок годности записи фиксируется
    в момент вставки, поэтому двигаем именно его.
    """
    for k, (expires, payload) in list(proxy._cache.items()):
        proxy._cache[k] = (expires - seconds, payload)



def test_при_отказе_индекса_отдаётся_устаревший_кэш(proxy, monkeypatch):
    """Ответ часовой давности по канону полезнее, чем «индекс недоступен».

    Канон федерации меняется раз в сутки, а не ежеминутно, поэтому устаревший ответ
    почти всегда остаётся верным. Пустой фолбэк оставляет агента вообще без знаний.
    """
    state = {"fail": False}

    async def flaky(query, depth, agent):
        if state["fail"]:
            raise OSError("sdev недоступен")
        return {"text": "секрет запрашивается у Ключника через approval-flow"}

    _patch_upstream(proxy, monkeypatch, flaky)

    run(proxy.search("как получить секрет", agent="eshkola"))      # наполнили кэш
    _age_cache(proxy, 7200)                                        # прошло два часа
    state["fail"] = True

    status, body = run(proxy.search("как получить секрет", agent="eshkola"))
    assert status == 200
    assert body["source"] == "stale"
    assert "approval-flow" in body["text"], "должен прийти прежний ответ, а не заглушка"
    assert "возраст" in body["warning"], "агент обязан видеть, что ответ не свежий"


def test_без_кэша_при_отказе_остаётся_текст_деградации(proxy, monkeypatch):
    async def boom(query, depth, agent):
        raise OSError("sdev недоступен")

    _patch_upstream(proxy, monkeypatch, boom)
    status, body = run(proxy.search("вопрос которого не было", agent="eshkola"))
    assert status == 200
    assert body["source"] == "fallback"
    assert body["text"] == FALLBACK_TEXT


def test_на_лимите_отдаётся_кэш_вместо_отказа(proxy, monkeypatch):
    """429 агенту, которому мы можем ответить бесплатно, — это отказ без причины."""
    async def fake(query, depth, agent):
        return {"text": "ответ про эскалацию"}

    _patch_upstream(proxy, monkeypatch, fake)

    run(proxy.search("порядок эскалации", agent="шумный"))
    _age_cache(proxy, 7200)
    for i in range(3):                                   # выбираем лимит новыми запросами
        run(proxy.search("иной вопрос %d" % i, agent="шумный"))

    status, body = run(proxy.search("порядок эскалации", agent="шумный"))
    assert status == 200, "на известный вопрос отвечаем даже за лимитом"
    assert body["source"] == "stale"
    assert body["throttled"] is True
    assert "ответ про эскалацию" in body["text"]


def test_неизвестный_вопрос_за_лимитом_всё_равно_отбивается(proxy, monkeypatch):
    """Иначе лимит перестаёт защищать демон, ради которого он и введён."""
    async def fake(query, depth, agent):
        return {"text": "ответ"}

    _patch_upstream(proxy, monkeypatch, fake)
    for i in range(3):
        run(proxy.search("вопрос %d" % i, agent="шумный"))

    status, body = run(proxy.search("ничего похожего раньше не спрашивали", agent="шумный"))
    assert status == 429
    assert body["retry_after"] == 60


def test_свежий_кэш_не_помечается_устаревшим(proxy, monkeypatch):
    async def fake(query, depth, agent):
        return {"text": "ответ"}

    _patch_upstream(proxy, monkeypatch, fake)
    run(proxy.search("вопрос", agent="a"))
    status, body = run(proxy.search("вопрос", agent="a"))
    assert body["source"] == "cache"
    assert "warning" not in body
