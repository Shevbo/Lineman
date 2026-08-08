"""Pure-function тесты для POST /api/sms/message (обёртка над send-sms.sh).

TCP-часть (_raw_api_sms_send) отдельно покрывается live-каналом (canary через
curl). Здесь — только парсинг body и извлечение agent-id из headers.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from proxy_server import ProxyServer


# --- _sms_parse_body ---

def test_parse_body_gateway_shape():
    """Контракт SMS Gateway app: {message, phoneNumbers:[...]} — garden уже так шлёт."""
    body = json.dumps({"message": "hi", "phoneNumbers": ["+79001234567"]}).encode()
    text, phones, err = ProxyServer._sms_parse_body(body)
    assert err is None
    assert text == "hi"
    assert phones == ["+79001234567"]


def test_parse_body_short_shape():
    """Короткая форма {message, phone:'...'}."""
    body = json.dumps({"message": "hi", "phone": "+79001234567"}).encode()
    text, phones, err = ProxyServer._sms_parse_body(body)
    assert err is None
    assert text == "hi"
    assert phones == ["+79001234567"]


def test_parse_body_text_alias():
    """`text` работает как алиас `message` (для SMS Gateway 3rdparty-совместимости)."""
    body = json.dumps({"text": "hi", "phoneNumbers": ["+79001234567"]}).encode()
    text, phones, err = ProxyServer._sms_parse_body(body)
    assert err is None
    assert text == "hi"


def test_parse_body_missing_message():
    body = json.dumps({"phoneNumbers": ["+79001234567"]}).encode()
    _, _, err = ProxyServer._sms_parse_body(body)
    assert err == "message and phone(s) required"


def test_parse_body_missing_phone():
    body = json.dumps({"message": "hi"}).encode()
    _, _, err = ProxyServer._sms_parse_body(body)
    assert err == "message and phone(s) required"


def test_parse_body_invalid_json():
    _, _, err = ProxyServer._sms_parse_body(b"not json {")
    assert err == "invalid JSON"


def test_parse_body_json_array_rejected():
    """Root JSON не dict — не body Gateway'а."""
    _, _, err = ProxyServer._sms_parse_body(b'["not","dict"]')
    assert err == "invalid JSON"


def test_parse_body_phones_not_array():
    body = json.dumps({"message": "hi", "phoneNumbers": "+79001234567"}).encode()
    _, _, err = ProxyServer._sms_parse_body(body)
    assert err == "phoneNumbers must be array"


def test_parse_body_multiple_phones():
    body = json.dumps({"message": "hi", "phoneNumbers": ["+79000000001", "+79000000002"]}).encode()
    text, phones, err = ProxyServer._sms_parse_body(body)
    assert err is None
    assert phones == ["+79000000001", "+79000000002"]


# --- _sms_extract_agent ---

def test_agent_from_x_agent_name():
    assert ProxyServer._sms_extract_agent({"x-agent-name": "garden-manager-sdev"}) == "garden-manager-sdev"


def test_agent_from_x_lineman_agent_fallback():
    """Если X-Agent-Name пуст — берём X-Lineman-Agent."""
    assert ProxyServer._sms_extract_agent({"x-lineman-agent": "stl"}) == "stl"


def test_agent_from_basic_auth_username():
    """Garden сейчас шлёт только Basic — user часть = agent-id."""
    b64 = base64.b64encode(b"garden:noauth").decode()
    hdrs = {"authorization": f"Basic {b64}"}
    assert ProxyServer._sms_extract_agent(hdrs) == "garden"


def test_agent_basic_over_none_but_below_x_agent_name():
    """X-Agent-Name приоритетнее Basic-username."""
    b64 = base64.b64encode(b"garden:noauth").decode()
    hdrs = {"x-agent-name": "explicit", "authorization": f"Basic {b64}"}
    assert ProxyServer._sms_extract_agent(hdrs) == "explicit"


def test_agent_no_headers_unknown():
    assert ProxyServer._sms_extract_agent({}) == "unknown"


def test_agent_malformed_basic_auth():
    hdrs = {"authorization": "Basic notbase64!!"}
    assert ProxyServer._sms_extract_agent(hdrs) == "unknown"


def test_agent_bearer_ignored():
    """Только Basic парсим, Bearer не для нас."""
    assert ProxyServer._sms_extract_agent({"authorization": "Bearer xyz"}) == "unknown"
