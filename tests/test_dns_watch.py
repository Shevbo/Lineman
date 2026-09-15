"""Сторож DNS-записей: без ложных тревог и без пропусков.

2026-09-15: из зоны shectory.ru пропало делегирование unisenderlinks на NS Unisender,
узнали только из письма Unisender. Самые опасные ошибки сторожа — ложно «пропало»
после починки (referral в AUTHORITY) и тревога, когда сервер просто не ответил.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "dns_watch", Path(__file__).resolve().parent.parent / "scripts" / "dns_watch.py")
dw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dw)

NS = {"id": "unisender-links-ns", "title": "Делегирование unisenderlinks", "name": "unisenderlinks.shectory.ru",
      "type": "NS", "contains": ["uns1.unisender.com", "uns2.unisender.com", "uns3.unisender.com"],
      "hint": "добавить NS"}
CFG = {"servers": ["ns10", "ns11"], "checks": [NS]}

REFERRAL = """;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1
;; AUTHORITY SECTION:
unisenderlinks.shectory.ru. 3600 IN NS uns1.unisender.com.
unisenderlinks.shectory.ru. 3600 IN NS uns2.unisender.com.
unisenderlinks.shectory.ru. 3600 IN NS uns3.unisender.com.
"""
NXDOMAIN = """;; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 2
;; AUTHORITY SECTION:
shectory.ru. 1800 IN SOA ns10.hoster.ru. support.hoster.ru. 1 3600 300 604800 1800
"""


def dig_with(text, status=None):
    def _dig(server, name, rtype):
        st = status or text.split("status:")[1].split(",")[0].strip()
        return st, text
    return _dig


def test_делегирование_в_authority_считается_на_месте():
    """Иначе после починки сторож кричал бы «пропало» вечно."""
    assert dw.evaluate(NS, ["ns10"], dig_with(REFERRAL))["ok"] is True


def test_nxdomain_это_пропавшая_запись():
    r = dw.evaluate(NS, ["ns10"], dig_with(NXDOMAIN))
    assert r["ok"] is False and "uns1.unisender.com" in r["detail"]


def test_неответивший_сервер_не_тревога():
    assert dw.evaluate(NS, ["ns10", "ns11"], lambda s, n, t: ("UNREACHABLE", ""))["ok"] is None


def test_один_сервер_молчит_другой_отвечает():
    def dig(server, name, rtype):
        return ("UNREACHABLE", "") if server == "ns10" else ("NOERROR", REFERRAL)
    assert dw.evaluate(NS, ["ns10", "ns11"], dig)["ok"] is True


def test_неполный_набор_ns_это_поломка():
    partial = REFERRAL.replace("unisenderlinks.shectory.ru. 3600 IN NS uns3.unisender.com.\n", "")
    r = dw.evaluate(NS, ["ns10"], dig_with(partial))
    assert r["ok"] is False and "uns3" in r["detail"]


def test_txt_из_нескольких_строк_склеивается():
    chk = {"id": "spf", "title": "SPF", "name": "shectory.ru", "type": "TXT",
           "contains": ["include:spf.unisender.ru"]}
    text = ";; ->>HEADER<<- status: NOERROR,\nshectory.ru. 60 IN TXT \"v=spf1 mx include:spf.\" \"unisender.ru ~all\"\n"
    assert dw.evaluate(chk, ["ns10"], dig_with(text))["ok"] is True


def _run(dig, state, now):
    sent = []
    out = dw.run(CFG, state, now, dig=dig, send=lambda m: sent.append(m) or True)
    return out, sent


def test_первый_запуск_алертит_только_сломанное(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "LOG_FILE", tmp_path / "log.jsonl")
    _, sent = _run(dig_with(REFERRAL), {}, 1000)
    assert sent == [], "исправное на первом запуске не должно слать сообщение"
    _, sent = _run(dig_with(NXDOMAIN), {}, 1000)
    assert len(sent) == 1 and "пропали" in sent[0] and "добавить NS" in sent[0]


def test_поломка_потом_починка_два_сообщения_без_спама(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "LOG_FILE", tmp_path / "log.jsonl")
    state = {}
    _, s1 = _run(dig_with(NXDOMAIN), state, 1000)
    _, s2 = _run(dig_with(NXDOMAIN), state, 1000 + 1800)
    _, s3 = _run(dig_with(REFERRAL), state, 1000 + 3600)
    assert len(s1) == 1 and s2 == [] and len(s3) == 1 and "восстановлены" in s3[0]


def test_напоминание_раз_в_сутки(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "LOG_FILE", tmp_path / "log.jsonl")
    state = {}
    _run(dig_with(NXDOMAIN), state, 0)
    _, s = _run(dig_with(NXDOMAIN), state, dw.REMIND_S + 1)
    assert len(s) == 1 and "всё ещё сломано" in s[0]


def test_недоступность_серверов_не_меняет_состояние(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "LOG_FILE", tmp_path / "log.jsonl")
    state = {}
    _run(dig_with(NXDOMAIN), state, 0)
    _, s = _run(lambda a, b, c: ("UNREACHABLE", ""), state, 100)
    assert s == [] and state["checks"]["unisender-links-ns"]["ok"] is False


def test_боевой_конфиг_разбирается_и_содержит_делегирование():
    import json
    cfg = json.loads((Path(__file__).resolve().parent.parent / "scripts" / "dns_watch.json").read_text(encoding="utf-8"))
    ids = {c["id"] for c in cfg["checks"]}
    assert "unisender-links-ns" in ids and cfg["servers"]
