"""Анти-спам для proxy circuit-breaker алертов в ТГ (api.telegram.org шумел сотнями)."""
from pool import ProxyPool


def test_telegram_trip_alert_suppressed_by_default():
    p = ProxyPool({})
    # api.telegram.org хронически срывается (fallback=direct работает) → не алертить.
    assert p._is_alert_suppressed("api.telegram.org") is True
    assert p._is_alert_suppressed("api.deepseek.com") is False


def test_alert_suppress_hosts_configurable():
    p = ProxyPool({"host_circuit_breaker": {"alert_suppress_hosts": ["foo.bar"]}})
    assert p._is_alert_suppressed("foo.bar") is True
    assert p._is_alert_suppressed("api.telegram.org") is False  # явный список заменяет дефолт


def test_alert_cooldown_default_raised():
    # дефолтный кулдаун поднят с 300с, чтобы не спамить даже не-suppressed хосты.
    p = ProxyPool({})
    assert p._cb_alert_cooldown >= 3600
