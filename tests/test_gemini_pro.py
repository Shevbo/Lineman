"""Тесты пер-агентного грант-стора Gemini Pro (gemini_pro.ProGrants)."""
from __future__ import annotations

import json

import pytest

from gemini_pro import ProGrants, apply_pro_gate


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(tmp_path):
    clk = FakeClock()
    g = ProGrants(str(tmp_path / "grants.json"), default_hours=3, clock=clk)
    return g, clk


def test_no_grant_is_not_pro(store):
    g, _ = store
    assert g.is_pro("titan") is False


def test_grant_makes_pro(store):
    g, _ = store
    g.grant("titan")
    assert g.is_pro("titan") is True
    assert g.is_pro("nurse") is False  # грант только конкретному агенту


def test_grant_expires(store):
    g, clk = store
    g.grant("titan", hours=2)
    assert g.is_pro("titan") is True
    clk.t += 2 * 3600 + 1  # окно прошло
    assert g.is_pro("titan") is False


def test_revoke(store):
    g, _ = store
    g.grant("titan")
    g.revoke("titan")
    assert g.is_pro("titan") is False


def test_default_hours_used(store):
    g, clk = store
    g.grant("titan")  # без hours → default 3
    clk.t += 3 * 3600 - 10
    assert g.is_pro("titan") is True
    clk.t += 20
    assert g.is_pro("titan") is False


def test_status_only_active(store):
    g, clk = store
    g.grant("titan", hours=1)
    g.grant("nurse", hours=1)
    clk.t += 3600 + 1  # titan и nurse истекли? оба по 1ч
    g.grant("career", hours=2)
    st = g.status()
    assert "career" in st
    assert "titan" not in st and "nurse" not in st
    assert 0 < st["career"] <= 2 * 3600


def test_persistence_across_instances(store, tmp_path):
    g, clk = store
    g.grant("titan", hours=5)
    g2 = ProGrants(str(tmp_path / "grants.json"), clock=clk)
    assert g2.is_pro("titan") is True


def test_corrupted_file_is_safe(tmp_path):
    p = tmp_path / "grants.json"
    p.write_text("{ this is not json", encoding="utf-8")
    g = ProGrants(str(p), clock=FakeClock())
    assert g.is_pro("titan") is False  # битый файл → не Pro (безопасно)


# --- apply_pro_gate: enforcement над rest_path ---

class _Cfg:
    BASE = "gemini-3.5-flash"
    PRO = "gemini-2.5-pro"


def _cfg():
    return {"gemini_pro": {"base_model": _Cfg.BASE, "pro_model": _Cfg.PRO}}


def test_gate_no_grant_downgrades_pro(store):
    g, _ = store
    path = "/v1beta/models/gemini-2.5-pro:generateContent"
    out = apply_pro_gate(path, "titan", g, _cfg())
    assert "gemini-3.5-flash" in out
    assert "2.5-pro" not in out


def test_gate_grant_keeps_pro(store):
    g, _ = store
    g.grant("titan")
    path = "/v1beta/models/gemini-2.5-pro:generateContent"
    out = apply_pro_gate(path, "titan", g, _cfg())
    assert "gemini-2.5-pro" in out


def test_gate_non_pro_untouched(store):
    g, _ = store
    path = "/v1beta/models/gemini-2.5-flash:generateContent"
    out = apply_pro_gate(path, "titan", g, _cfg())
    assert out == path


def test_gate_unknown_agent_downgrades(store):
    g, _ = store
    path = "/v1beta/models/gemini-2.5-pro:generateContent"
    out = apply_pro_gate(path, None, g, _cfg())
    assert "gemini-3.5-flash" in out
