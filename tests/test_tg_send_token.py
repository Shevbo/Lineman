"""Резолв токена бота для POST /api/tg/send (account -> botToken)."""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from proxy_server import ProxyServer


def _oc(tmp_path, accounts):
    p = tmp_path / "openclaw.json"
    p.write_text(json.dumps({"channels": {"telegram": {"accounts": accounts}}}))
    return SimpleNamespace(_tg_oc_path=str(p))


def test_account_from_openclaw(tmp_path):
    fake = _oc(tmp_path, {"default": {"botToken": "111:AAA"}})
    assert ProxyServer._tg_resolve_token(fake, "default") == ("111:AAA", True)


def test_unknown_account_empty_token(tmp_path):
    fake = _oc(tmp_path, {"default": {"botToken": "111:AAA"}})
    assert ProxyServer._tg_resolve_token(fake, "nope") == ("", True)


def test_klod_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KLOD_BOT_TOKEN", "222:KLOD")
    fake = _oc(tmp_path, {"default": {"botToken": "111:AAA"}})
    assert ProxyServer._tg_resolve_token(fake, "klod") == ("222:KLOD", True)


def test_klod_without_env_stays_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("KLOD_BOT_TOKEN", raising=False)
    fake = _oc(tmp_path, {"default": {"botToken": "111:AAA"}})
    assert ProxyServer._tg_resolve_token(fake, "klod") == ("", True)


def test_openclaw_in_config_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KLOD_BOT_TOKEN", "222:KLOD")
    fake = _oc(tmp_path, {"klod": {"botToken": "333:FROMCONFIG"}})
    assert ProxyServer._tg_resolve_token(fake, "klod") == ("333:FROMCONFIG", True)


def test_broken_config_reports_not_ok(tmp_path, monkeypatch):
    monkeypatch.delenv("KLOD_BOT_TOKEN", raising=False)
    fake = SimpleNamespace(_tg_oc_path=str(tmp_path / "missing.json"))
    assert ProxyServer._tg_resolve_token(fake, "default") == ("", False)


def test_broken_config_still_serves_klod_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KLOD_BOT_TOKEN", "222:KLOD")
    fake = SimpleNamespace(_tg_oc_path=str(tmp_path / "missing.json"))
    assert ProxyServer._tg_resolve_token(fake, "klod") == ("222:KLOD", False)
