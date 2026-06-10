"""Тесты вотчдога Клода (#4)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from watchdog import (scan_text_for_secrets, scan_files_for_secrets, check_required_docs,
                      check_paid_route_leak, check_token_caps, build_report,
                      new_violations, Violation)


def test_detects_deepseek_key():
    v = scan_text_for_secrets("key = sk-abcdef0123456789abcdef0123", "auth.py")
    assert len(v) == 1 and v[0].severity == "high" and v[0].check == "secret_leak"


def test_ignores_placeholder():
    assert scan_text_for_secrets("DEEPSEEK_API_KEY=sk-xxx-your-key-here", "doc.md") == []
    assert scan_text_for_secrets("token = 123456:ABC-example", "doc.md") == []


def test_detects_github_and_privkey():
    files = {"a.py": "t=ghp_0123456789012345678901234567890123",
             "b.pem": "-----BEGIN PRIVATE KEY-----"}
    vs = scan_files_for_secrets(files)
    assert len(vs) == 2


def test_required_docs_missing():
    vs = check_required_docs({"docs/A.md"}, ["docs/A.md", "docs/PORTAL_AUTH_STANDARD.md"])
    assert len(vs) == 1 and "PORTAL_AUTH_STANDARD" in vs[0].path


def test_paid_route_leak():
    assert check_paid_route_leak("ROUTE = deepseek-flash") != []          # платный без фолбэка
    assert check_paid_route_leak("ROUTE = lm-studio, ollama") == []        # локальный — ок
    assert check_paid_route_leak("ROUTE = deepseek, lm-studio") == []      # есть фолбэк


def test_token_caps():
    assert check_token_caps({"reverse_proxy": {"daily_token_caps": {"deepseek": 1000000}}}) == []
    assert check_token_caps({"reverse_proxy": {}}) != []


def test_build_report_and_new():
    vs = [Violation("secret_leak", "high", "a.py:1", "похоже на deepseek/openai key")]
    rep = build_report(vs, "2026-06-10T12:00:00")
    assert rep["total"] == 1 and rep["by_severity"]["high"] == 1 and rep["ok"] is False
    # повтор того же → не новое; другое → новое
    assert new_violations(rep["violations"], vs) == []
    v2 = [Violation("token_caps", "medium", "config.json", "нет капа")]
    assert len(new_violations(rep["violations"], v2)) == 1


def test_clean_report_ok():
    rep = build_report([], "t")
    assert rep["ok"] is True and rep["total"] == 0
