"""Инвариант: response helpers _send_json_response и _send_simple_and_close
ВСЕГДА пропускают body через mask_secrets. Belt-and-suspenders после
регрессии 2026-08-11 (fed-backup msg 24302, точечные фиксы уже в handler'ах,
но wrapper — гарантия что новый handler не забудет замаскировать).

Тесты структурные (проверка исходника). TCP-часть покрывается live smoke.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from proxy_server import ProxyServer


def test_send_json_response_uses_mask_secrets():
    src = inspect.getsource(ProxyServer._send_json_response)
    assert "mask_secrets" in src, (
        "_send_json_response не использует mask_secrets — секреты потекут через "
        "любой /api/* endpoint. Регрессия fed-backup msg 24302 воспроизводима."
    )


def test_send_simple_and_close_uses_mask_secrets():
    src = inspect.getsource(ProxyServer._send_simple_and_close)
    assert "mask_secrets" in src, (
        "_send_simple_and_close не использует mask_secrets — /api/klod/ask и др. "
        "потекут secretами при upstream-ошибках."
    )


def test_mask_secrets_import_present_in_both_helpers():
    """Импорт может быть локальный (внутри функции) — проверяем оба варианта."""
    for fn_name in ("_send_json_response", "_send_simple_and_close"):
        src = inspect.getsource(getattr(ProxyServer, fn_name))
        assert ("from secret_mask import mask_secrets" in src
                or "import secret_mask" in src), (
            f"{fn_name}: mask_secrets не импортирован — вызов упадёт NameError"
        )
