"""LM Studio hints + TTS resolve / build / extract."""
from __future__ import annotations

import base64

import klod_ask


# -------- LM Studio hints --------

def test_lm_studio_hints_route_to_lm_studio():
    for hint in ("local-fast", "local-normal", "local-deep", "local-reason", "local-qwen"):
        p, m = klod_ask.resolve_model(hint)
        assert p == "lm-studio", f"{hint} routed to {p}, expected lm-studio"
        assert m and not m.startswith("local-")


def test_lm_studio_specific_targets():
    assert klod_ask.resolve_model("local-fast")   == ("lm-studio", "google/gemma-4-e4b")
    assert klod_ask.resolve_model("local-normal") == ("lm-studio", "google/gemma-4-12b")
    assert klod_ask.resolve_model("local-deep")   == ("lm-studio", "google/gemma-4-26b-a4b-it-imatrix")
    assert klod_ask.resolve_model("local-reason") == ("lm-studio", "deepseek-r1-distill-qwen-14b")
    assert klod_ask.resolve_model("local-qwen")   == ("lm-studio", "qwen/qwen3.5-9b")


def test_lm_studio_request_uses_openai_compat_path():
    path, body, _ = klod_ask.build_request_payload(
        "lm-studio", "google/gemma-4-12b", "ping", 100)
    assert path == "/proxy/lm-studio/v1/chat/completions"
    assert body["model"] == "google/gemma-4-12b"
    assert body["messages"][0]["content"] == "ping"


# -------- TTS resolve --------

def test_resolve_tts_defaults_to_pro():
    assert klod_ask.resolve_tts(None) == klod_ask.TTS_PRESETS["tts-pro"]
    assert klod_ask.resolve_tts("") == klod_ask.TTS_PRESETS["tts-pro"]


def test_resolve_tts_known_hints():
    assert klod_ask.resolve_tts("tts-fast") == ("google", "gemini-2.5-flash-preview-tts")
    assert klod_ask.resolve_tts("tts-pro")  == ("google", "gemini-2.5-pro-preview-tts")
    assert klod_ask.resolve_tts("tts-3-1")  == ("google", "gemini-3.1-flash-tts-preview")


def test_resolve_tts_unknown_falls_to_pro():
    assert klod_ask.resolve_tts("totally-not-a-hint") == klod_ask.TTS_PRESETS["tts-pro"]


# -------- TTS request builder --------

def test_build_tts_request_shape():
    path, body, headers = klod_ask.build_tts_request(
        "google", "gemini-2.5-pro-preview-tts", "Привет", voice="Aoede")
    assert path == "/proxy/google/v1beta/models/gemini-2.5-pro-preview-tts:generateContent"
    assert body["contents"][0]["parts"][0]["text"] == "Привет"
    gen = body["generationConfig"]
    assert gen["responseModalities"] == ["AUDIO"]
    voice = gen["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert voice == "Aoede"
    assert headers.get("X-Agent-Name") == "klod-access"


def test_build_tts_request_rejects_non_google():
    import pytest as _p
    with _p.raises(ValueError):
        klod_ask.build_tts_request("anthropic", "claude-sonnet-4-6", "ping")


# -------- TTS extract --------

def _wrap_audio(b: bytes, mime: str = "audio/wav") -> dict:
    return {
        "candidates": [{
            "content": {
                "parts": [{
                    "inlineData": {"data": base64.b64encode(b).decode(), "mimeType": mime},
                }],
            },
        }],
    }


def test_extract_audio_decodes_base64():
    raw = b"\x52\x49\x46\x46\x00FAKEWAV"
    out, mime = klod_ask.extract_audio("google", _wrap_audio(raw, "audio/wav"))
    assert out == raw
    assert mime == "audio/wav"


def test_extract_audio_empty_when_no_candidates():
    assert klod_ask.extract_audio("google", {}) == (b"", "")
    assert klod_ask.extract_audio("google", {"candidates": []}) == (b"", "")


def test_extract_audio_handles_snake_case_keys():
    # Some Google libs spit inline_data + mime_type instead of camelCase
    obj = {
        "candidates": [{
            "content": {"parts": [{"inline_data": {
                "data": base64.b64encode(b"hi").decode(), "mime_type": "audio/mp3",
            }}]},
        }],
    }
    out, mime = klod_ask.extract_audio("google", obj)
    assert out == b"hi"
    assert mime == "audio/mp3"


def test_extract_audio_non_google_returns_empty():
    out, mime = klod_ask.extract_audio("anthropic", _wrap_audio(b"x"))
    assert out == b""
    assert mime == ""
