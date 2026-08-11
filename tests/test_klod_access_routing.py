"""Инвариант: любое сообщение ОТ klod-access ДОЛЖНО попадать в klod-access
outbox (`~/klod-access/outbox.jsonl`), а не в federation-inbox catch-all.

Регрессия 2026-08-11: klod-access писал ответ STL через
`POST /api/agent/klod-stl/message?from=klod-access` — сообщение ушло в
`~/.federation-inbox/klod-stl/inbox.jsonl` (federation-inbox), а klod-stl
поллит `~/klod-access/outbox.jsonl` через
`GET /api/agent/klod-access/outbox?to=klod-stl&since=<cursor>`. Каналы
разные, ответ потерялся молча.

После патча (2026-08-11): from=klod-access → выделенная ветка, вызывает
klod_inbox.write_outbox() напрямую, минуя federation-inbox catch-all.
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_server


def _handler_source() -> str:
    """Возвращает исходник _raw_api_agent_message. Тесты структурные — TCP
    handler покрывается live smoke, здесь проверяем инвариант в коде."""
    return inspect.getsource(proxy_server.ProxyServer._raw_api_agent_message)


def test_from_klod_access_branch_exists():
    """Обязательная спецветка для klod-access должна присутствовать."""
    src = _handler_source()
    assert 'if from_agent_id == "klod-access":' in src, \
        "Ветка `from_agent_id == \"klod-access\"` удалена/переименована — регрессия каналов доставки"


def test_from_klod_access_uses_write_outbox():
    """Ветка klod-access ДОЛЖНА писать через klod_inbox.write_outbox."""
    src = _handler_source()
    branch_start = src.index('if from_agent_id == "klod-access":')
    branch = src[branch_start:branch_start + 1500]
    assert "klod_inbox.write_outbox" in branch, \
        "klod-access ветка не использует write_outbox — сообщения снова уйдут в federation-inbox"


def test_from_klod_access_returns_via_marker():
    """Ответ должен помечать via='klod-access-outbox' (чтобы клиенты видели, куда легло)."""
    src = _handler_source()
    branch_start = src.index('if from_agent_id == "klod-access":')
    branch = src[branch_start:branch_start + 1500]
    assert '"via": "klod-access-outbox"' in branch, \
        "via-маркер потерян — клиенты не смогут отличить outbox-путь от catch-all"


def test_from_klod_access_short_circuits():
    """Ветка klod-access ДОЛЖНА завершать хендлер (return), не проваливаясь
    в federation-inbox catch-all путь ниже."""
    src = _handler_source()
    branch_start = src.index('if from_agent_id == "klod-access":')
    branch_end_candidates = [
        src.find("agent_meta = self._agents_meta.get", branch_start),
        src.find("in_node_map = agent_meta", branch_start),
    ]
    catchall_start = min(x for x in branch_end_candidates if x > 0)
    branch = src[branch_start:catchall_start]
    # Должен быть await wr.drain(); wr.close(); return ПЕРЕД catch-all
    assert "return" in branch, "klod-access ветка не завершается return — свалится в catch-all"
    assert "wr.close()" in branch, "klod-access ветка не закрывает соединение"


def test_from_klod_access_supports_in_reply_to():
    """Ветка должна парсить in_reply_to из query (чтобы reply-цепочки не рвались)."""
    src = _handler_source()
    branch_start = src.index('if from_agent_id == "klod-access":')
    branch = src[branch_start:branch_start + 1500]
    assert "in_reply_to" in branch, "in_reply_to не пробрасывается — reply-цепочки рвутся"
