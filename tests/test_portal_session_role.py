"""Роль портала внутри сессии: защита от эскалации через dashboard.shectory.ru.

Дыра, ради которой это написано (аудит 2026-09-16): nginx пускает на весь `/api/`
по одному auth_request на `/api/session-check`, а дальше Lineman видит клиента как
127.0.0.1 и считает доверенным по IP-allowlist'у. Роль в сессии не хранилась, поэтому
пользователь портала с ролью `trader` получал полный админ-API федерации.

Проверяется именно граница, а не внутренности: кому Lineman выдаёт cookie, чью cookie
он признаёт админской и что происходит со старыми токенами.
"""
import asyncio

import pytest

from proxy_server import ProxyServer


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("SHECTORY_AUTH_BRIDGE_SECRET", "тестовый-секрет-подписи")
    server = ProxyServer.__new__(ProxyServer)
    server._portal_auth_cache = {}
    server._portal_auth_ttl = 300.0
    return server


def cookie(token):
    return {"cookie": "shectory_session=%s" % token}


# ------------------------------------------------------------------ токен и роль
def test_роль_переживает_круг_через_токен(srv):
    token = srv._make_session_token("boris@example.com", role="superadmin")
    assert srv._verify_session_token(token) == ("boris@example.com", "superadmin")


def test_подделка_роли_в_токене_не_проходит(srv):
    """Главное свойство: роль под подписью, а не рядом с ней."""
    token = srv._make_session_token("trader@example.com", role="trader")
    forged = token.replace("v2:trader:", "v2:superadmin:", 1)
    assert forged != token
    assert srv._verify_session_token(forged) is None


def test_чужая_подпись_не_принимается(srv, monkeypatch):
    token = srv._make_session_token("boris@example.com", role="admin")
    monkeypatch.setenv("SHECTORY_AUTH_BRIDGE_SECRET", "другой-секрет")
    assert srv._verify_session_token(token) is None


def test_просроченная_сессия_не_принимается(srv):
    # Выпуск и проверка по одной шкале: ttl отсчитывается от `now`.
    token = srv._make_session_token("boris@example.com", role="admin",
                                    ttl=10, now=1000)
    assert srv._verify_session_token(token, now=1005) is not None
    assert srv._verify_session_token(token, now=1011) is None


def test_двоеточие_в_роли_не_ломает_разбор(srv):
    """Иначе роль вида `ad:min` сдвинула бы границы полей токена."""
    token = srv._make_session_token("boris@example.com", role="ad:min")
    email, role = srv._verify_session_token(token)
    assert (email, role) == ("boris@example.com", "admin")


def test_телеграм_почта_с_двоеточием_разбирается(srv):
    """`telegram:36910539` сам содержит разделитель — частый источник таких багов."""
    token = srv._make_session_token("telegram:36910539", role="admin")
    assert srv._verify_session_token(token) == ("telegram:36910539", "admin")


# ----------------------------------------------------------------- админ-граница
def test_сессия_трейдера_не_даёт_админа(srv):
    """Ровно тот случай, что нашёл аудит: пароль верный, прав на федерацию нет."""
    token = srv._make_session_token("trader@example.com", role="trader")
    assert srv._session_email_from_cookie(cookie(token)) == "trader@example.com"
    assert srv._session_admin_email(cookie(token)) is None


def test_сессия_админа_даёт_админа(srv):
    for role in ("admin", "superadmin"):
        token = srv._make_session_token("boris@example.com", role=role)
        assert srv._session_admin_email(cookie(token)) == "boris@example.com"


def test_роль_по_умолчанию_не_админская(srv):
    """Если роль забыли передать, доступ должен сузиться, а не расшириться."""
    token = srv._make_session_token("some@example.com")
    assert srv._session_admin_email(cookie(token)) is None


def test_старый_токен_без_роли_не_считается_админским(srv):
    """Токены, выданные до фикса, роли не несут — доверять им как админским нельзя."""
    import hashlib
    import hmac
    import time

    exp = int(time.time() + 3600)
    msg = "boris@example.com:%d" % exp
    sig = hmac.new("тестовый-секрет-подписи".encode(), msg.encode(),
                   hashlib.sha256).hexdigest()
    legacy = "%s:%s" % (msg, sig)

    assert srv._verify_session_token(legacy) == ("boris@example.com", "legacy")
    assert srv._session_admin_email(cookie(legacy)) is None


def test_мусорная_cookie_не_роняет_проверку(srv):
    for bad in ("", "v2:", "v2:admin", "просто-текст", "a:b:c:d:e"):
        assert srv._session_admin_email(cookie(bad)) is None


# --------------------------------------------------- роль от портала и её кэш
def _fake_bridge(srv, monkeypatch, role, calls):
    import proxy_server as mod

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"ok": True, "role": role} if role else {"ok": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, url, headers=None, json=None):
            calls.append(json.get("email"))
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setenv("SHECTORY_PORTAL_URL", "http://127.0.0.1:3000")
    monkeypatch.setattr(mod.aiohttp, "ClientSession",
                        lambda *a, **kw: _Session(), raising=False)


def test_роль_из_портала_доезжает_до_вызывающего(srv, monkeypatch):
    calls = []
    _fake_bridge(srv, monkeypatch, "trader", calls)
    assert asyncio.run(srv._portal_role("t@example.com", "пароль")) == "trader"
    assert asyncio.run(srv._portal_admin_ok("t@example.com", "пароль")) is False


def test_админ_роль_пропускается(srv, monkeypatch):
    _fake_bridge(srv, monkeypatch, "superadmin", [])
    assert asyncio.run(srv._portal_admin_ok("b@example.com", "пароль")) is True


def test_неверный_пароль_даёт_none(srv, monkeypatch):
    _fake_bridge(srv, monkeypatch, None, [])
    assert asyncio.run(srv._portal_role("b@example.com", "не тот")) is None


def test_кэш_хранит_роль_а_не_только_факт_входа(srv, monkeypatch):
    """Кэш старого формата вернул бы True без роли — и снова пустил бы трейдера."""
    calls = []
    _fake_bridge(srv, monkeypatch, "trader", calls)
    asyncio.run(srv._portal_role("t@example.com", "пароль"))
    assert asyncio.run(srv._portal_role("t@example.com", "пароль")) == "trader"
    assert len(calls) == 1, "второй раз портал дёргать незачем"
    assert asyncio.run(srv._portal_admin_ok("t@example.com", "пароль")) is False


def test_запись_кэша_старого_формата_перепроверяется(srv, monkeypatch):
    """После рестарта в кэше может лежать голый срок — он роли не знает."""
    import hashlib
    import time

    calls = []
    _fake_bridge(srv, monkeypatch, "trader", calls)
    key = hashlib.sha256("t@example.com:пароль".encode("utf-8")).hexdigest()
    srv._portal_auth_cache[key] = time.time() + 300      # формат до фикса

    assert asyncio.run(srv._portal_role("t@example.com", "пароль")) == "trader"
    assert calls, "запись без роли обязана привести к походу в портал"


def test_портал_без_роли_считается_обычным_пользователем(srv, monkeypatch):
    _fake_bridge(srv, monkeypatch, "", [])

    import proxy_server as mod

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"ok": True}                      # роли в ответе нет

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, *a, **kw):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod.aiohttp, "ClientSession",
                        lambda *a, **kw: _Session(), raising=False)
    assert asyncio.run(srv._portal_role("x@example.com", "пароль")) == "user"
    assert asyncio.run(srv._portal_admin_ok("x@example.com", "пароль")) is False
