"""Tests for secret_mask: ensure tokens, keys, passwords, bot tokens are redacted."""
from secret_mask import mask_row, mask_secrets


# --- URL basic-auth (регрессия 2026-08-11, fed-backup msg 24302) ---
# aiohttp прокидывал http://LOGIN:PASSWORD@proxy в тексте исключений;
# /api/klod/ask отдавал в теле ответа наружу. Инвариант: пароль всегда REDACTED,
# логин остаётся видимым (диагностика — чей ключ утёк, для оперативной ротации).

def test_http_basic_auth_url_masked():
    s = 'url=http://tfCvF1:97QsVP@45.85.162.25:8000'
    out = mask_secrets(s)
    assert "97QsVP" not in out, "пароль не должен утекать"
    assert "tfCvF1" in out, "логин должен остаться (диагностика)"
    assert "***REDACTED***" in out


def test_socks5_basic_auth_url_masked():
    s = 'proxy socks5://user:hunter2@10.66.0.9:1080/socks'
    out = mask_secrets(s)
    assert "hunter2" not in out
    assert "socks5://user:" in out


def test_ftp_basic_auth_url_masked():
    s = 'ftp://boris:sekrit@ftp.shectory.ru:21/dir'
    out = mask_secrets(s)
    assert "sekrit" not in out
    assert "ftp://boris:" in out


def test_https_no_auth_url_unchanged():
    """Обычный URL без auth не должен вообще меняться."""
    s = 'GET https://api.anthropic.com/v1/messages HTTP/1.1'
    assert mask_secrets(s) == s


def test_url_auth_in_error_json():
    """Точный случай из fed-backup msg 24302 (упс с /api/klod/ask)."""
    s = ('{"error": "llm call failed: upstream HTTP 502: '
         '{\\"error\\": \\"Upstream error: 407, url='
         'http://tfCvF1:97QsVP@45.85.162.25:8000\\"}"}')
    out = mask_secrets(s)
    assert "97QsVP" not in out
    assert "tfCvF1:***REDACTED***@" in out


def test_url_auth_preserves_rest_of_string():
    """Только пароль в URL должен меняться, остальной текст — нет."""
    s = 'before url=https://user:s3cret@host.com/path?q=1 after text'
    out = mask_secrets(s)
    assert "s3cret" not in out
    assert "before " in out and " after text" in out
    assert "user:***REDACTED***@host.com/path?q=1" in out


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
