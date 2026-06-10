"""Тесты валидации Telegram Mini App initData."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tg_miniapp import validate_init_data, user_id_allowed


BOT = "123456:TESTTOKENabcdef"


def _make_init_data(bot_token, user, auth_date=None, extra=None, with_signature=False):
    auth_date = auth_date if auth_date is not None else int(time.time())
    data = {"user": json.dumps(user, separators=(",", ":")),
            "auth_date": str(auth_date), "query_id": "AAA"}
    if extra:
        data.update(extra)
    if with_signature:
        data["signature"] = "ed25519stub"
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_valid_init_data():
    init = _make_init_data(BOT, {"id": 36910539, "first_name": "Boris"})
    p = validate_init_data(init, BOT)
    assert p is not None
    assert p["user"]["id"] == 36910539


def test_tampered_hash_rejected():
    init = _make_init_data(BOT, {"id": 36910539})
    init = init.replace("query_id=AAA", "query_id=EVIL")
    assert validate_init_data(init, BOT) is None


def test_wrong_token_rejected():
    init = _make_init_data(BOT, {"id": 36910539})
    assert validate_init_data(init, "999:WRONG") is None


def test_signature_field_present_still_validates():
    init = _make_init_data(BOT, {"id": 36910539}, with_signature=True)
    assert validate_init_data(init, BOT) is not None


def test_stale_auth_date_rejected():
    init = _make_init_data(BOT, {"id": 36910539}, auth_date=int(time.time()) - 100000)
    assert validate_init_data(init, BOT, max_age=86400) is None


def test_allowlist_gate():
    init = _make_init_data(BOT, {"id": 36910539})
    assert user_id_allowed(validate_init_data(init, BOT), {"36910539"}) is True
    init2 = _make_init_data(BOT, {"id": 555})
    assert user_id_allowed(validate_init_data(init2, BOT), {"36910539"}) is False


def test_empty_inputs():
    assert validate_init_data("", BOT) is None
    assert validate_init_data("hash=x", "") is None
