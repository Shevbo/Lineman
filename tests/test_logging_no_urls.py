"""Библиотечные логи с URL не попадают в stderr: там токен бота.

Аудит 2026-09-15: в ~/.pm2/logs/lineman-gateway-error.log четыре строки
`HTTP Request: GET https://api.telegram.org/bot<TOKEN>/getMe` — httpx на уровне
INFO печатает полный URL, а проверка checks/telegram.py ходит в getMe с токеном
в пути. Маскировщик secret_mask на stdlib-логи не распространяется.
"""
from __future__ import annotations

import logging

import main


def _emit_and_capture(logger_name: str, message: str) -> list[str]:
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    sink = _Sink()
    root = logging.getLogger()
    root.addHandler(sink)
    try:
        logging.getLogger(logger_name).info(message)
    finally:
        root.removeHandler(sink)
    return records


def test_httpx_url_на_info_не_пишется(monkeypatch):
    main.configure_logging(debug_mode=False)
    line = 'HTTP Request: GET https://api.telegram.org/bot1234567890:AAF-token-token-token-token/getMe "HTTP/1.1 200 OK"'
    for lib in ("httpx", "httpcore"):
        assert _emit_and_capture(lib, line) == [], lib


def test_собственные_info_логи_остаются():
    """Под pytest у root уже есть handler, и basicConfig уровень не трогает: задаём
    его явно, чтобы проверить именно разницу между своими логами и httpx."""
    main.configure_logging(debug_mode=False)
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.INFO)
    try:
        assert _emit_and_capture("lineman.test", "pool_selected") == ["pool_selected"]
        assert _emit_and_capture("httpx", "HTTP Request: GET https://x/bot1:t/getMe") == []
    finally:
        root.setLevel(prev)


def test_в_debug_режиме_библиотеки_всё_равно_молчат():
    """LINEMAN_DEBUG=1 поднимает уровень процесса, но не возвращает URL в лог."""
    main.configure_logging(debug_mode=True)
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
