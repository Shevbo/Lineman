"""Кто вправе писать от какого бота через /api/tg/send.

До 2026-09-14 не проверялось вовсе: любой доверенный вызывающий слал от любого из
10 ботов openclaw. Вскрылось на письме omniroute, которому нужно писать блог от
@Virtual_Boris_Bot; Борис разрешил, а заодно велел завести проверку.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from proxy_server import ProxyServer

ACL = {"virtual-boris": ["virtual-boris", "omniroute"]}


def make(acl=None):
    cfg = {"proxy_server": {}, "proxy_pool": {}}
    if acl is not None:
        cfg["tg_send"] = {"account_agents": acl}
    return ProxyServer(config=cfg)


def test_omniroute_вправе_писать_от_virtual_boris():
    assert make(ACL)._tg_account_allowed("virtual-boris", "omniroute")[0]


def test_владелец_бота_вправе():
    assert make(ACL)._tg_account_allowed("virtual-boris", "virtual-boris")[0]


def test_чужой_агент_получает_отказ_с_объяснением():
    allowed, reason = make(ACL)._tg_account_allowed("virtual-boris", "nurse")
    assert not allowed
    assert "nurse" in reason and "omniroute" in reason


def test_без_имени_агента_закрытый_бот_недоступен():
    assert not make(ACL)._tg_account_allowed("virtual-boris", "")[0]


def test_незакрытый_аккаунт_ведёт_себя_как_раньше():
    """Иначе молча сломались бы алерты и Ключник, про чьи заголовки данных нет."""
    assert make(ACL)._tg_account_allowed("default", "кто-угодно")[0]


def test_без_секции_конфига_всё_открыто():
    assert make()._tg_account_allowed("virtual-boris", "nurse")[0]


def test_chat_id_по_умолчанию_берётся_из_окружения(monkeypatch):
    monkeypatch.setenv("BORIS_TG_CHAT_ID", "42")
    assert make()._default_tg_chat_id() == "42"


def test_проверка_прав_стоит_до_лимита_и_до_отправки():
    """Отказ должен случаться до того, как сработал лимит и ушёл запрос в Telegram.

    Ищем присваивания, а не слова: «sendMessage» есть уже в докстринге обработчика.
    """
    src = inspect.getsource(ProxyServer._raw_api_tg_send)
    acl = src.index("self._tg_account_allowed(")
    rate = src.index("RATE_LIMIT_S = ")
    send = src.index("tg_url = ")
    assert acl < rate < send


def test_в_боевом_конфиге_virtual_boris_закрыт_на_владельца_и_omniroute():
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
    assert sorted(cfg["tg_send"]["account_agents"]["virtual-boris"]) == ["omniroute", "virtual-boris"]
