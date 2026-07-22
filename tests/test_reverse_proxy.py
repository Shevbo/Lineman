"""Tests for agent header, prompt snippet extraction, and router batch rewrite."""
from __future__ import annotations
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_extract_agent_name_from_header():
    from reverse_proxy import _extract_agent_name
    headers = {"content-type": "application/json", "x-agent-name": "selfcoder"}
    assert _extract_agent_name(headers) == "selfcoder"


def test_extract_agent_name_lineman_fallback():
    from reverse_proxy import _extract_agent_name
    headers = {"x-lineman-agent": "titan"}
    assert _extract_agent_name(headers) == "titan"


def test_extract_agent_name_missing():
    from reverse_proxy import _extract_agent_name
    assert _extract_agent_name({"content-type": "application/json"}) is None


def test_extract_prompt_snippet_from_messages():
    from reverse_proxy import _extract_prompt_snippet
    body = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is the capital of France?"},
        ]
    }).encode()
    assert _extract_prompt_snippet(body) == "What is the capital of France?"


def test_extract_prompt_snippet_truncation():
    from reverse_proxy import _extract_prompt_snippet
    body = json.dumps({"messages": [{"role": "user", "content": "A" * 3000}]}).encode()
    result = _extract_prompt_snippet(body)
    assert result is not None and len(result) == 2000


def test_extract_prompt_snippet_invalid_json():
    from reverse_proxy import _extract_prompt_snippet
    assert _extract_prompt_snippet(b"not json") is None


def test_extract_prompt_snippet_no_messages():
    from reverse_proxy import _extract_prompt_snippet
    body = json.dumps({"model": "deepseek-v4-flash"}).encode()
    assert _extract_prompt_snippet(body) is None


def test_extract_prompt_snippet_content_blocks():
    from reverse_proxy import _extract_prompt_snippet
    body = json.dumps({"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "Explain this code"},
            {"type": "image", "source": {}},
        ]}
    ]}).encode()
    assert _extract_prompt_snippet(body) == "Explain this code"


# ---------------------------------------------------------------------------
# Router batch rewrite integration
# ---------------------------------------------------------------------------

BATCH_CONFIG = {
    "reverse_proxy": {
        "upstreams": {
            "ollama-hoster": "http://10.66.0.7:11434",
            "deepseek": "https://api.deepseek.com",
        }
    },
    "routing": {},
}


def test_router_batch_route_resolves_to_ollama():
    """Router.resolve(BATCH) must return ollama-hoster/llama3.2:3b from fallback chain."""
    from router import Router, RouteContext
    router = Router({})
    route = router.resolve(RouteContext.BATCH)
    assert route.provider == "ollama-hoster"
    assert route.model == "llama3.2:3b"


def test_router_batch_detect_from_cron_keyword():
    """detect_context flags BATCH when body contains 'cron'."""
    from router import Router, RouteContext
    router = Router({})
    body = json.dumps({"messages": [{"role": "user", "content": "run cron job now"}]}).encode()
    ctx = router.detect_context(body, {})
    assert ctx == RouteContext.BATCH


def test_resolve_upstream_ollama_from_config():
    """_resolve_upstream reads ollama-hoster URL from config correctly."""
    from reverse_proxy import _resolve_upstream
    url = _resolve_upstream("ollama-hoster", BATCH_CONFIG)
    assert url == "http://10.66.0.7:11434"


# ---------------------------------------------------------------------------
# Anthropic OAuth injection for allowlisted external agents (ltx и др.).
# Клод-Access владеет OAuth, Claude Code CLI у ltx ключа не имеет — Lineman
# подставляет свой Bearer только для agents, явно указанных в
# reverse_proxy.anthropic_agent_allowlist.
# ---------------------------------------------------------------------------

ALLOWLIST_CFG = {
    "reverse_proxy": {
        "anthropic_agent_allowlist": ["ltx", "vboris2"],
    }
}


def test_oauth_injected_for_allowlisted_agent():
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {"content-type": "application/json", "x-api-key": "client-key-should-drop"}
    status = maybe_inject_anthropic_oauth(
        "anthropic", "ltx", headers, ALLOWLIST_CFG,
        token_loader=lambda: "klod-oauth-token")
    assert status == "injected"
    assert headers["authorization"] == "Bearer klod-oauth-token"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in headers


def test_oauth_merges_client_anthropic_beta():
    """Claude Code CLI шлёт свои beta-флаги (context-management-2025-06-27 для 1M
    Opus 4.8, prompt-caching и т.п.). Merge — не replace, иначе 400. Регрессия
    2026-07-22 (ltx OPS #b1784669156558)."""
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {"anthropic-beta": "context-management-2025-06-27"}
    status = maybe_inject_anthropic_oauth(
        "anthropic", "ltx", headers, ALLOWLIST_CFG,
        token_loader=lambda: "klod-oauth-token")
    assert status == "injected"
    parts = [p.strip() for p in headers["anthropic-beta"].split(",")]
    assert "context-management-2025-06-27" in parts
    assert "oauth-2025-04-20" in parts


def test_oauth_deduplicates_beta_flag():
    """Клиент уже прислал oauth-2025-04-20 — не должно быть дубля."""
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {"anthropic-beta": "oauth-2025-04-20, prompt-caching-2024-07-31"}
    maybe_inject_anthropic_oauth(
        "anthropic", "ltx", headers, ALLOWLIST_CFG,
        token_loader=lambda: "tok")
    parts = [p.strip() for p in headers["anthropic-beta"].split(",")]
    assert parts.count("oauth-2025-04-20") == 1
    assert "prompt-caching-2024-07-31" in parts


def test_merge_anthropic_beta_empty_input():
    from reverse_proxy import _merge_anthropic_beta
    assert _merge_anthropic_beta("") == "oauth-2025-04-20"
    assert _merge_anthropic_beta(None) == "oauth-2025-04-20"


def test_oauth_not_injected_for_agent_outside_allowlist():
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {"authorization": "Bearer client-token"}
    status = maybe_inject_anthropic_oauth(
        "anthropic", "career-bot", headers, ALLOWLIST_CFG,
        token_loader=lambda: "klod-oauth-token")
    assert status == "not_in_allowlist"
    assert headers["authorization"] == "Bearer client-token"


def test_oauth_not_applicable_for_google():
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {}
    status = maybe_inject_anthropic_oauth(
        "google", "ltx", headers, ALLOWLIST_CFG,
        token_loader=lambda: "klod-oauth-token")
    assert status == "not_applicable"
    assert "authorization" not in headers


def test_oauth_not_applicable_when_no_agent_name():
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {}
    status = maybe_inject_anthropic_oauth(
        "anthropic", None, headers, ALLOWLIST_CFG,
        token_loader=lambda: "klod-oauth-token")
    assert status == "not_applicable"


def test_oauth_returns_no_token_when_loader_empty():
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {}
    status = maybe_inject_anthropic_oauth(
        "anthropic", "ltx", headers, ALLOWLIST_CFG,
        token_loader=lambda: "")
    assert status == "no_token"
    assert "authorization" not in headers


def test_oauth_allowlist_missing_config_treated_as_empty():
    from reverse_proxy import maybe_inject_anthropic_oauth
    headers = {}
    status = maybe_inject_anthropic_oauth(
        "anthropic", "ltx", headers, {},
        token_loader=lambda: "t")
    assert status == "not_in_allowlist"


def test_load_klod_anthropic_oauth_reads_credentials(tmp_path, monkeypatch):
    from reverse_proxy import _load_klod_anthropic_oauth
    creds_dir = tmp_path / ".claude"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "abc123"}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    import pathlib as _pl
    monkeypatch.setattr(_pl.Path, "home", classmethod(lambda cls: _pl.Path(str(tmp_path))))
    assert _load_klod_anthropic_oauth() == "abc123"


def test_load_klod_anthropic_oauth_missing_returns_empty(tmp_path, monkeypatch):
    from reverse_proxy import _load_klod_anthropic_oauth
    import pathlib as _pl
    monkeypatch.setattr(_pl.Path, "home", classmethod(lambda cls: _pl.Path(str(tmp_path))))
    assert _load_klod_anthropic_oauth() == ""


def test_config_json_declares_ltx_allowlist_and_cap():
    """Регрессия конфига: ltx должен быть и в allowlist, и иметь дневной cap."""
    import json as _json
    from pathlib import Path
    cfg = _json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
    rp = cfg["reverse_proxy"]
    assert "ltx" in rp["anthropic_agent_allowlist"]
    assert rp["daily_agent_token_caps"]["ltx"]["anthropic"] > 0
