"""Аудит 2026-07-04, П3: /push_url — проверка владения agent_id и внутренний URL."""
import pytest
from proxy_server import ProxyServer


@pytest.fixture
def srv():
    return ProxyServer(config={
        "proxy_server": {}, "proxy_pool": {},
        "agents": {"node_map": {"smain": ["nurse", "titan"]}},
        "klod_ask": {"extra_agents": ["career-bot"]},
    })


def test_push_url_internal_accepts_wg_and_loopback(srv):
    assert srv._push_url_internal("http://10.66.0.1:9099/push")
    assert srv._push_url_internal("http://127.0.0.1:8080/x")
    assert srv._push_url_internal("http://localhost/x")
    assert srv._push_url_internal("http://smain:9099/push")   # короткое WG-имя
    assert srv._push_url_internal("http://hoster/inbox")


def test_push_url_internal_rejects_external(srv):
    assert not srv._push_url_internal("http://evil.example.com/steal")
    assert not srv._push_url_internal("http://8.8.8.8/x")
    assert not srv._push_url_internal("https://1.2.3.4:443/outbox")
    assert not srv._push_url_internal("not-a-url")
    assert not srv._push_url_internal("")


def test_push_url_agent_must_be_in_registry(srv):
    import klod_ask
    assert klod_ask.is_agent_allowed("nurse", srv._klod_ask_allowed)
    assert klod_ask.is_agent_allowed("career-bot", srv._klod_ask_allowed)
    assert not klod_ask.is_agent_allowed("evil", srv._klod_ask_allowed)
