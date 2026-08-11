"""Исход доставки reply обязан осесть в журнале и всплыть в /outbox.

Регрессия 2026-08-11 (вопрос klod-stl про просрочку): outbox-запись писалась
с delivered=None, deliver_reply отрабатывал в фоне и результат терялся —
17955 записей из 17959 с delivered=null. Отправитель не мог отличить
доставленное от потерянного.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import klod_inbox as ki


@pytest.fixture
def tmp_boxes(tmp_path, monkeypatch):
    d = tmp_path / "klod-access"
    monkeypatch.setattr(ki, "INBOX_DIR", d)
    monkeypatch.setattr(ki, "INBOX_FILE", d / "inbox.jsonl")
    monkeypatch.setattr(ki, "OUTBOX_FILE", d / "outbox.jsonl")
    monkeypatch.setattr(ki, "COUNTER_FILE", d / "counter.txt")
    monkeypatch.setattr(ki, "PUSH_URLS_FILE", d / "push_urls.json")
    monkeypatch.setattr(ki, "DELIVERY_STATUS_FILE", d / "outbox_delivery.jsonl")
    monkeypatch.setattr(ki, "PULL_CURSORS_FILE", d / "pull_cursors.json")
    return d


def test_record_and_load_delivery_status(tmp_boxes):
    ki.record_delivery(7, True, None, "push")
    st = ki.load_delivery_status()
    assert st[7]["delivered"] is True
    assert st[7]["via"] == "push"


def test_last_status_wins(tmp_boxes):
    ki.record_delivery(7, False, "push HTTP 502", "push")
    ki.record_delivery(7, True, None, "forward")
    st = ki.load_delivery_status()
    assert st[7]["delivered"] is True and st[7]["delivery_error"] is None


def test_read_outbox_overlays_status(tmp_boxes):
    rec = ki.write_outbox("klod-stl", "текст", in_reply_to=42, delivered=None)
    ki.record_delivery(rec["id"], False, "push HTTP 404", "push")
    got = ki.read_outbox(to="klod-stl")
    assert len(got) == 1
    assert got[0]["delivered"] is False
    assert got[0]["delivery_error"] == "push HTTP 404"
    assert got[0]["delivery_via"] == "push"
    assert got[0]["delivered_at"]


def test_read_outbox_without_status_stays_null(tmp_boxes):
    ki.write_outbox("klod-stl", "текст")
    got = ki.read_outbox(to="klod-stl")
    assert got[0]["delivered"] is None


def test_deliver_reply_records_push_failure(tmp_boxes, monkeypatch):
    """push_url зарегистрирован, но эндпоинт недостижим → delivered=False в журнале."""
    ki.set_push_url("klod-stl", "http://127.0.0.1:1/push")
    rec = ki.write_outbox("klod-stl", "текст")
    ok, err = asyncio.run(ki.deliver_reply("klod-stl", "текст", record_id=rec["id"]))
    assert ok is False and err
    st = ki.load_delivery_status()[rec["id"]]
    assert st["delivered"] is False and st["via"] == "push"
    assert ki.read_outbox(to="klod-stl")[0]["delivered"] is False


def test_pull_cursor_marks_record_delivered(tmp_boxes):
    """Read-receipt для pull: курсор адресата подтверждает, что запись забрана."""
    rec = ki.write_outbox("klod-stl", "текст")
    assert ki.read_outbox(to="klod-stl")[0]["delivered"] is None
    ki.record_pull("klod-stl", rec["id"])          # агент пришёл с since=<id>
    got = ki.read_outbox(to="klod-stl")[0]
    assert got["delivered"] is True and got["delivery_via"] == "pull"


def test_pull_cursor_only_moves_forward(tmp_boxes):
    ki.record_pull("klod-stl", 100)
    ki.record_pull("klod-stl", 50)
    assert ki.load_pull_cursors()["klod-stl"] == 100


def test_pull_cursor_does_not_touch_other_agents(tmp_boxes):
    rec = ki.write_outbox("klod-stl", "текст")
    ki.record_pull("eshkola", rec["id"] + 10)
    assert ki.read_outbox(to="klod-stl")[0]["delivered"] is None


def test_push_status_wins_over_cursor(tmp_boxes):
    """Явный исход push важнее курсора: провал доставки не должен затираться."""
    rec = ki.write_outbox("klod-stl", "текст")
    ki.record_delivery(rec["id"], False, "push HTTP 502", "push")
    ki.record_pull("klod-stl", rec["id"])
    got = ki.read_outbox(to="klod-stl")[0]
    assert got["delivered"] is False and got["delivery_via"] == "push"


def test_delivery_journal_is_append_only_jsonl(tmp_boxes):
    ki.record_delivery(1, True, None, "push")
    ki.record_delivery(2, False, "fwd HTTP 404", "forward")
    lines = [json.loads(x) for x in
             (tmp_boxes / "outbox_delivery.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert [d["ref"] for d in lines] == [1, 2]
