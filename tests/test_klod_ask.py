"""Klod-Access LLM gateway: чистая логика (без HTTP)."""
import json
import pathlib

import klod_ask


# ---------------- resolve_model ----------------

def test_resolve_model_default_when_no_hint():
    p, m = klod_ask.resolve_model(None)
    assert p == "anthropic" and m.startswith("claude-sonnet")


def test_resolve_model_unknown_hint_falls_to_normal():
    assert klod_ask.resolve_model("xyz") == klod_ask.MODEL_PRESETS["normal"]


def test_resolve_model_all_presets():
    for k in ("fast", "normal", "deep", "gemini-flash", "gemini-flash-lite",
              "gemini-pro", "deepseek-fast", "deepseek-reason"):
        assert klod_ask.resolve_model(k) == klod_ask.MODEL_PRESETS[k]


def test_resolve_model_flash_lite():
    assert klod_ask.resolve_model("gemini-flash-lite") == ("google", "gemini-2.5-flash-lite")


def test_resolve_model_deepseek_mappings():
    assert klod_ask.resolve_model("deepseek-fast") == ("deepseek", "deepseek-chat")
    assert klod_ask.resolve_model("deepseek-reason") == ("deepseek", "deepseek-reasoner")


def test_resolve_model_case_insensitive():
    assert klod_ask.resolve_model("FAST") == klod_ask.MODEL_PRESETS["fast"]
    assert klod_ask.resolve_model("  Deep  ") == klod_ask.MODEL_PRESETS["deep"]


# ---------------- is_agent_allowed ----------------

def test_is_agent_allowed_empty_agent_rejected():
    assert klod_ask.is_agent_allowed("", None) is False
    assert klod_ask.is_agent_allowed("  ", None) is False


def test_is_agent_allowed_wildcard_lets_named():
    assert klod_ask.is_agent_allowed("career-bot", ["*"]) is True
    assert klod_ask.is_agent_allowed("nurse", None) is True  # пустой allow = открыто


def test_is_agent_allowed_explicit_list():
    al = ["career-bot", "titan"]
    assert klod_ask.is_agent_allowed("career-bot", al) is True
    assert klod_ask.is_agent_allowed("nurse", al) is False


# ---------------- check_budget ----------------

def test_check_budget_under_default_passes():
    ok, reason = klod_ask.check_budget("career-bot", [], {}, now=100.0)
    assert ok is True and reason == ""


def test_check_budget_per_hour_exhausted():
    recent = [(50.0, "career-bot")] * 30
    cfg = {"default": {"per_hour": 30, "per_day": 200}}
    ok, reason = klod_ask.check_budget("career-bot", recent, cfg, now=100.0)
    assert ok is False
    assert "per-hour" in reason and "30/30" in reason


def test_check_budget_per_day_exhausted():
    recent = [(time, "career-bot") for time in range(0, 200)]  # 200 за окно дня
    cfg = {"default": {"per_hour": 1000, "per_day": 200}}
    ok, reason = klod_ask.check_budget("career-bot", recent, cfg,
                                       now=86000.0)
    assert ok is False and "per-day" in reason


def test_check_budget_per_agent_override():
    cfg = {"default": {"per_hour": 30}, "agents": {"career-bot": {"per_hour": 5}}}
    recent = [(50.0, "career-bot")] * 5
    ok, _ = klod_ask.check_budget("career-bot", recent, cfg, now=100.0)
    assert ok is False  # career-bot specific limit hit
    # Другой агент свой бюджет не съел
    ok2, _ = klod_ask.check_budget("nurse", recent, cfg, now=100.0)
    assert ok2 is True


def test_check_budget_zero_disables_check():
    cfg = {"default": {"per_hour": 0, "per_day": 0}}
    recent = [(50.0, "career-bot")] * 9999
    ok, _ = klod_ask.check_budget("career-bot", recent, cfg, now=100.0)
    assert ok is True


def test_check_budget_outside_window_doesnt_count():
    """Запись старше часа не считается в per-hour счётчике."""
    recent = [(0.0, "career-bot")] * 100  # все вне часового окна
    cfg = {"default": {"per_hour": 5}}
    ok, _ = klod_ask.check_budget("career-bot", recent, cfg, now=10_000.0)
    assert ok is True


# ---------------- clamp_max_tokens ----------------

def test_clamp_max_tokens_defaults_when_none():
    assert klod_ask.clamp_max_tokens(None) == klod_ask.DEFAULT_MAX_TOKENS


def test_clamp_max_tokens_caps_too_large():
    assert klod_ask.clamp_max_tokens(99999) == klod_ask.HARD_MAX_TOKENS


def test_clamp_max_tokens_invalid_falls_to_default():
    assert klod_ask.clamp_max_tokens("not-a-number") == klod_ask.DEFAULT_MAX_TOKENS
    assert klod_ask.clamp_max_tokens(-5) == klod_ask.DEFAULT_MAX_TOKENS


def test_clamp_max_tokens_passthrough_in_range():
    assert klod_ask.clamp_max_tokens(500) == 500


# ---------------- trim_recent ----------------

def test_trim_recent_drops_old_entries():
    now = 100_000.0
    recent = [(now - 90_000, "x"), (now - 1000, "y"), (now - 100, "z")]
    out = klod_ask.trim_recent(recent, now=now)
    ids = [a for _, a in out]
    assert "x" not in ids and "y" in ids and "z" in ids


# ---------------- build_request_payload ----------------

def test_build_request_payload_anthropic():
    path, body, headers = klod_ask.build_request_payload(
        "anthropic", "claude-sonnet-4-6", "привет", 500)
    assert path == "/proxy/anthropic/v1/messages"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["max_tokens"] == 500
    assert body["messages"][0]["content"] == "привет"
    assert headers["anthropic-version"] == "2023-06-01"


def test_build_request_payload_google():
    path, body, headers = klod_ask.build_request_payload(
        "google", "gemini-2.5-flash", "привет", 500)
    assert "gemini-2.5-flash" in path and ":generateContent" in path
    assert body["contents"][0]["parts"][0]["text"] == "привет"
    assert body["generationConfig"]["maxOutputTokens"] == 500
    assert headers["X-Agent-Name"] == "klod-access"


def test_build_request_payload_unknown_provider_raises():
    import pytest
    with pytest.raises(ValueError):
        klod_ask.build_request_payload("ollama", "x", "y", 1)


def test_build_request_payload_deepseek():
    path, body, headers = klod_ask.build_request_payload(
        "deepseek", "deepseek-chat", "привет", 500)
    assert path == "/proxy/deepseek/v1/chat/completions"
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 500
    assert body["messages"][0]["content"] == "привет"
    assert headers["X-Agent-Name"] == "klod-access"


# ---------------- extract_text ----------------

def test_extract_text_anthropic():
    resp = {"content": [{"type": "text", "text": "ответ"},
                        {"type": "tool_use", "input": {}}]}
    assert klod_ask.extract_text("anthropic", resp) == "ответ"


def test_extract_text_google():
    resp = {"candidates": [
        {"content": {"parts": [{"text": "ответ"}, {"text": " дальше"}]}}]}
    assert klod_ask.extract_text("google", resp) == "ответ дальше"


def test_extract_text_empty_response():
    assert klod_ask.extract_text("anthropic", {}) == ""
    assert klod_ask.extract_text("google", {"candidates": []}) == ""
    assert klod_ask.extract_text("deepseek", {}) == ""


def test_extract_text_deepseek():
    resp = {"choices": [
        {"message": {"role": "assistant", "content": "ответ DeepSeek"}}]}
    assert klod_ask.extract_text("deepseek", resp) == "ответ DeepSeek"


# ---------------- resolve_explicit ----------------

def test_resolve_explicit_provider_model_overrides_hint():
    prov, model = klod_ask.resolve_explicit("deepseek", "deepseek-reasoner")
    assert prov == "deepseek"
    assert model == "deepseek-reasoner"


def test_resolve_explicit_rejects_unknown_provider():
    import pytest
    with pytest.raises(ValueError):
        klod_ask.resolve_explicit("madeup", "x")


def test_build_payload_lmstudio_openai_compat():
    path, body, headers = klod_ask.build_request_payload("lm-studio", "qwen3.5-9b", "hi", 100)
    assert path == "/proxy/lm-studio/v1/chat/completions"
    assert body["model"] == "qwen3.5-9b"
    assert body["messages"][0]["content"] == "hi"


def test_extract_deepseek_reasoning_content_fallback():
    resp = {"choices": [{"message": {"content": "", "reasoning_content": "думал тут"}}]}
    assert klod_ask.extract_text("deepseek", resp) == "думал тут"


# ---------------- audit_log ----------------

def test_audit_log_writes_jsonl(tmp_path):
    p = tmp_path / "audit.jsonl"
    klod_ask.audit_log({"agent": "career-bot", "model": "haiku", "ok": True}, path=p)
    klod_ask.audit_log({"agent": "nurse", "model": "sonnet", "ok": False}, path=p)
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    assert r0["agent"] == "career-bot"
