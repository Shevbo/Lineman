"""Tests for secret_mask: ensure tokens, keys, passwords, bot tokens are redacted."""
from secret_mask import mask_row, mask_secrets


def test_json_api_key():
    s = '{"api_key":"AIzaSyB-fake-very-long-google-key-value-12345"}'
    out = mask_secrets(s)
    assert "AIzaSyB-fake-very-long" not in out
    assert "***REDACTED***" in out


def test_json_apikey_camelcase():
    s = '{"apiKey":"AIzaSyB-camelcase-key-very-long-1234567"}'
    out = mask_secrets(s)
    assert "AIzaSyB-camelcase" not in out
    assert "***REDACTED***" in out


def test_authorization_header():
    s = 'Authorization: Bearer sk-proj-abcdef0123456789abcdef0123'
    out = mask_secrets(s)
    assert "sk-proj-abcdef" not in out
    assert "***REDACTED***" in out


def test_sk_openai_prefix_standalone():
    s = 'oops sk-proj-1234567890abcdef0123456789abcdef end'
    out = mask_secrets(s)
    assert "sk-proj-1234567890" not in out
    assert "***REDACTED***" in out


def test_google_aiza_prefix():
    s = 'curl https://api/?key=AIzaSyDfake-google-key-very-long-12345&q=hi'
    out = mask_secrets(s)
    assert "AIzaSyDfake-google" not in out
    assert "***REDACTED***" in out


def test_telegram_bot_token():
    s = 'https://api.telegram.org/bot8734567890:AAFakeTelegramBotToken_abcd1234efgh/sendMessage'
    out = mask_secrets(s)
    assert "AAFakeTelegramBotToken" not in out
    assert "***REDACTED***" in out


def test_telegram_bot_token_standalone():
    s = 'token=8734567890:AAFakeTelegramBotToken_abcdefghijklmnopqrstuv'
    out = mask_secrets(s)
    assert "AAFakeTelegramBotToken" not in out
    assert "***REDACTED***" in out


def test_github_token():
    s = 'token: ghp_abcdef0123456789abcdef0123456789ABCD'
    out = mask_secrets(s)
    assert "ghp_abcdef" not in out
    assert "***REDACTED***" in out


def test_none_returns_none():
    assert mask_secrets(None) is None
    assert mask_secrets("") == ""


def test_safe_text_unchanged():
    s = '{"messages":[{"role":"user","content":"hello world"}]}'
    out = mask_secrets(s)
    assert out == s


def test_mask_row_request_body_and_url():
    row = {
        "request_body": '{"api_key":"AIzaSyB-fake-key-here-long-enough-123"}',
        "target_url": "https://api.openai.com/v1/chat?api_key=sk-proj-abcdef0123456789abcdef",
        "error": "401: Bearer sk-proj-bad0123456789abcdef0123456789",
        "status_code": 401,
    }
    mask_row(row)
    assert "AIzaSyB-fake" not in row["request_body"]
    assert "sk-proj-abcdef" not in row["target_url"]
    assert "sk-proj-bad" not in row["error"]
    assert row["status_code"] == 401
