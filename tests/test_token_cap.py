"""Жёсткий дневной кап токенов на провайдера (deepseek жёг миллионы/день бесконтрольно)."""
from token_cap import DailyTokenCap


def test_allow_until_cap_then_block():
    c = DailyTokenCap({"deepseek": 1000})
    d = "2026-06-10"
    assert c.allow("deepseek", d) is True
    c.record("deepseek", 600, d)
    assert c.allow("deepseek", d) is True          # 600 < 1000
    c.record("deepseek", 500, d)                    # 1100 >= 1000
    assert c.allow("deepseek", d) is False          # перебор → блок


def test_no_cap_provider_always_allowed():
    c = DailyTokenCap({"deepseek": 1000})
    d = "2026-06-10"
    c.record("google", 999999, d)
    assert c.allow("google", d) is True             # нет капа → всегда можно


def test_daily_reset_on_new_day():
    c = DailyTokenCap({"deepseek": 1000})
    c.record("deepseek", 2000, "2026-06-10")
    assert c.allow("deepseek", "2026-06-10") is False
    assert c.allow("deepseek", "2026-06-11") is True   # новый день — счётчик сброшен


def test_seed_from_history():
    c = DailyTokenCap({"deepseek": 1000})
    d = "2026-06-10"
    c.seed("deepseek", 900, d)                       # уже потрачено сегодня (из request_log)
    assert c.allow("deepseek", d) is True
    c.record("deepseek", 200, d)                     # 1100 >= 1000
    assert c.allow("deepseek", d) is False


def test_status_report():
    c = DailyTokenCap({"deepseek": 1000})
    d = "2026-06-10"
    c.record("deepseek", 300, d)
    s = c.status(d)
    assert s["deepseek"] == {"used": 300, "cap": 1000, "remaining": 700}


def test_zero_or_missing_cap_disabled():
    c = DailyTokenCap({"deepseek": 0, "openai": "abc"})  # 0 и невалидное игнорируются
    d = "2026-06-10"
    c.record("deepseek", 10_000_000, d)
    assert c.allow("deepseek", d) is True
