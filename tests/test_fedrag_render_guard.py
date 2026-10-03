"""Замок рендерера реестров: секрет не должен уехать в корпус индекса.

Регрессия 2026-10-01: значение SMSGATEWAY_LOCAL_SERVER лежало в поле desc компонента
sms-gateway в federation_registry.json, рендерер перенёс его в federation_registry.md,
и гейт Ключника выбросил файл из корпуса. Файл исчез из индекса МОЛЧА: recall@3 упал
0.867 -> 0.800, перестал находиться даже ответ на «какой компонент отвечает за бэкапы».
Поэтому проверка стоит до записи файла, а не после потери в поиске.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

RENDER = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "fedrag_render_registry.py"


def _load():
    spec = importlib.util.spec_from_file_location("fedrag_render_registry", RENDER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fedrag_render_registry"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_clean_text_allowed(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: _Resp({"checked": 1, "flagged": []}))
    assert mod.secret_free("безобидный текст", "x.md") is True


def test_flagged_text_refused(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod.urllib.request, "urlopen",
        lambda *a, **k: _Resp({"checked": 1,
                               "flagged": [{"id": "text",
                                            "names": ["SMSGATEWAY_LOCAL_SERVER"]}]}))
    assert mod.secret_free("внутри значение", "x.md") is False


def test_keymaster_down_counts_as_refusal(monkeypatch):
    """«Не смог проверить» не равно «проверено» — то же правило, что в гейте синка."""
    mod = _load()

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod.secret_free("что угодно", "x.md") is False


def test_file_not_written_when_refused(monkeypatch, tmp_path):
    """Главное: отказ означает отсутствие файла, а не файл с секретом внутри."""
    mod = _load()
    monkeypatch.setattr(mod, "secret_free", lambda text, label: False)
    monkeypatch.setattr(sys, "argv", ["fedrag_render_registry.py", str(tmp_path)])
    rc = mod.main()
    assert rc == 1, "нечего не записав, рендерер обязан вернуть ненулевой код"
    assert list(tmp_path.iterdir()) == [], "при отказе файлов быть не должно"


def test_real_registry_is_secret_free():
    """Живой реестр в репозитории не содержит значений секретов.

    Ходит в Ключника по-настоящему: без него тест пропускается, но не врёт зелёным.
    """
    mod = _load()
    reg = pathlib.Path(mod.LINEMAN) / "federation_registry.json"
    if not reg.exists():
        pytest.skip("federation_registry.json недоступен")
    try:
        import urllib.request
        urllib.request.urlopen(mod.KEYMASTER + "/health", timeout=3)
    except Exception:
        pytest.skip("Ключник недоступен — проверка значений невозможна")
    assert mod.secret_free(reg.read_text(encoding="utf-8"), "federation_registry.json") is True
