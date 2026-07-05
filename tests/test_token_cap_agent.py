"""Per-agent дневной кап токенов (2026-07-05, защита от career-bot 71M/сутки)."""
from token_cap import DailyTokenCap

DAY = "2026-07-05"


def test_per_agent_cap_blocks_only_that_agent():
    c = DailyTokenCap({"anthropic": 1_000_000_000},
                      {"career-bot": {"anthropic": 100}})
    # career-bot упирается в свой кап, остальные — нет
    c.record("anthropic", 100, DAY, agent="career-bot")
    assert not c.allow_agent("career-bot", "anthropic", DAY)
    assert c.allow_agent("nurse", "anthropic", DAY)       # без per-agent капа
    assert c.allow("anthropic", DAY)                       # провайдер-кап огромный


def test_per_agent_cap_absent_allows():
    c = DailyTokenCap({}, {})
    assert c.allow_agent("anyone", "anthropic", DAY)


def test_seed_and_roll_reset():
    c = DailyTokenCap({}, {"career-bot": {"anthropic": 100}})
    c.seed("anthropic", 90, DAY, agent="career-bot")
    assert c.allow_agent("career-bot", "anthropic", DAY)   # 90 < 100
    c.record("anthropic", 20, DAY, agent="career-bot")     # 110 >= 100
    assert not c.allow_agent("career-bot", "anthropic", DAY)
    # новый день — счётчик обнуляется
    assert c.allow_agent("career-bot", "anthropic", "2026-07-06")


def test_provider_and_agent_independent():
    c = DailyTokenCap({"deepseek": 50}, {"career-bot": {"anthropic": 50}})
    c.record("anthropic", 50, DAY, agent="career-bot")
    assert not c.allow_agent("career-bot", "anthropic", DAY)
    assert c.allow("deepseek", DAY)  # deepseek провайдер-кап не тронут
