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
