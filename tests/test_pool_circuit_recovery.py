"""Восстановление per-host circuit breaker в пуле прокси.

Инцидент 2026-09-12: eschool-bot на hoster получал плавающие ReadTimeout к Telegram.
В логах на каждый запрос — `pool_all_circuits_tripped host=api.telegram.org
proxies=['proxy6'] fallback=direct`, то есть весь трафик Telegram шёл мимо пула
напрямую, а прямой канал до Telegram нестабилен.

Корень: цепь пробивалась и больше не восстанавливалась. Проверка `recovery_secs`
жила ТОЛЬКО внутри `_record_host`, а он вызывается из `record()`, где первой строкой
стоит ранний выход для `proxy_id == "direct"`. После пробоя весь трафик уходил на
direct, записей по паре (proxy6, api.telegram.org) больше не поступало, и таймер
восстановления не срабатывал никогда. Цепь снималась только перезапуском Lineman.
"""
import time

import pytest

from pool import ProxyPool


def make_pool(recovery_secs=1800, threshold=3):
    return ProxyPool({
        "proxies": [
            {"id": "proxy6", "name": "Proxy6", "url": "http://user:pass@127.0.0.1:8000",
             "priority": 1, "enabled": True},
        ],
        "routes": [{"hosts": ["*"], "proxies": ["proxy6"]}],
        "host_circuit_breaker": {
            "enabled": True,
            "window_secs": 300,
            "error_threshold": threshold,
            "recovery_secs": recovery_secs,
            "alert_cooldown_secs": 300,
        },
    })


def trip(pool, host="api.telegram.org", n=3):
    """Набить ошибок до пробоя цепи."""
    for _ in range(n):
        pool.record("proxy6", False, 100.0, host=host)


def test_цепь_пробивается_после_порога_ошибок():
    pool = make_pool()
    trip(pool)
    use_proxy, proxy_id = pool.select("api.telegram.org")
    assert proxy_id == "direct", "после порога ошибок пул обязан уйти на fallback"


def test_другой_хост_не_страдает_от_пробоя():
    pool = make_pool()
    trip(pool, host="api.telegram.org")
    _, proxy_id = pool.select("api.ipify.org")
    assert proxy_id == "proxy6", "circuit breaker пер-хостовый, соседа он трогать не должен"


def test_цепь_восстанавливается_сама_без_новых_записей(monkeypatch):
    """Главный тест инцидента.

    После пробоя записей по этой паре больше нет — весь трафик ушёл на direct.
    Цепь обязана восстановиться по времени, иначе прокси похоронен до рестарта.
    """
    pool = make_pool(recovery_secs=1800)
    trip(pool)
    assert pool.select("api.telegram.org")[1] == "direct"

    # Прошло больше recovery_secs. Ни одной новой записи не поступало — и не могло:
    # fallback на direct делает record() no-op.
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 1801)

    use_proxy, proxy_id = pool.select("api.telegram.org")
    assert proxy_id == "proxy6", (
        "цепь должна была восстановиться по таймеру; если здесь direct — "
        "прокси заблокирован для хоста навсегда, до перезапуска Lineman"
    )


def test_до_истечения_recovery_цепь_остаётся_пробитой(monkeypatch):
    pool = make_pool(recovery_secs=1800)
    trip(pool)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 60)
    assert pool.select("api.telegram.org")[1] == "direct", \
        "раннее восстановление вернуло бы трафик на ещё нерабочий прокси"


def test_после_восстановления_счётчик_ошибок_обнулён(monkeypatch):
    """Иначе одна новая ошибка мгновенно пробивает цепь заново."""
    pool = make_pool(recovery_secs=1800, threshold=3)
    trip(pool)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 1801)
    assert pool.select("api.telegram.org")[1] == "proxy6"

    pool.record("proxy6", False, 100.0, host="api.telegram.org")
    assert pool.select("api.telegram.org")[1] == "proxy6", \
        "одна ошибка после восстановления не должна пробивать цепь"
