"""Reverse proxy handler — transparent HTTP forward with body/token inspection.

URL format: /proxy/{provider}/{rest...}
Strips /proxy/{provider}, forwards to real upstream HTTPS,
extracts token counts from request/response bodies, logs to request_log.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog
from router import RouteContext

logger = structlog.get_logger(__name__)

# Max estimated tokens for unnamed agents before blocking
_UNNAMED_CTX_LIMIT = 80_000
# Потолок контекста на ОДИН запрос для ИМЕНОВАННЫХ агентов (дефолт; перебивается
# config.reverse_proxy.ctx_hard_limit). Главный жор федерации — claude-cli:lineman ~500k/запрос
# (max 999k). Абсурдные одиночные запросы режем 429, чтобы заставить суммаризировать.
_NAMED_CTX_HARD_LIMIT = 700_000
# huge_context алерты: не чаще раза в 30 мин на агента (иначе застрявший на 300k агент
# плодит десятки одинаковых сигналов в инбокс klod-access → флуд + трата токенов).
_HUGE_CTX_LAST: dict[str, float] = {}
_HUGE_CTX_COOLDOWN_S = 1800.0

_UNNAMED_CTX_BLOCKED_HOSTS = frozenset({"smain", "hoster", "cloud"})


def _extract_agent_name(headers: dict[str, str]) -> str | None:
    """Return X-Agent-Name header value, or None."""
    return headers.get("x-agent-name") or headers.get("x-lineman-agent") or None


def _extract_prompt_snippet(body: bytes, max_len: int = 2000) -> str | None:
    """Extract last user message content from request body, truncated."""
    if not body:
        return None
    try:
        data = json.loads(body)
        messages = data.get("messages", [])
        if not messages:
            return None
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content[:max_len]
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            return text[:max_len]
        return None
    except Exception:
        return None


def _count_message_tokens(content: Any) -> int:
    """Estimate token count from a message content field (4 chars ≈ 1 token)."""
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        return sum(
            len(block.get("text", "")) // 4
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return 0


from secret_mask import mask_secrets as _mask_sensitive_impl, mask_row

def _mask_sensitive(text: str) -> str:
    return _mask_sensitive_impl(text) or ""


def _load_klod_anthropic_oauth() -> str:
    """Bearer-токен Klod-Access для Anthropic — та же кредуха, что использует
    /api/klod/ask (_load_klod_oauth_token в proxy_server). Отдельная копия здесь,
    чтобы reverse_proxy мог инжектить OAuth для агентов из
    reverse_proxy.anthropic_agent_allowlist, у которых своего ключа нет
    (Claude Code CLI у ltx/подобных). Возвращает '' при отсутствии/ошибке."""
    try:
        import pathlib
        p = pathlib.Path.home() / ".claude/.credentials.json"
        d = json.loads(p.read_text())
        return (d.get("claudeAiOauth") or {}).get("accessToken", "") or ""
    except Exception:
        return ""


def _anthropic_allowlist(config: dict | None) -> list:
    return list(((config or {}).get("reverse_proxy", {})
                 .get("anthropic_agent_allowlist") or []))


def maybe_inject_anthropic_oauth(
    provider: str,
    agent_name: str | None,
    fwd_headers: dict,
    config: dict | None,
    token_loader=_load_klod_anthropic_oauth,
) -> str:
    """Инжект Klod OAuth для anthropic-запросов от разрешённых агентов.

    Мутирует fwd_headers на месте: подставляет Authorization/anthropic-beta,
    вырезает клиентский x-api-key. Возвращает статус:
      "not_applicable" — не anthropic / нет agent_name
      "not_in_allowlist" — агент известен, но не в списке
      "no_token" — allowlist ок, но OAuth недоступен (для 503)
      "injected" — инжект сделан
    """
    if provider != "anthropic" or not agent_name:
        return "not_applicable"
    if agent_name not in _anthropic_allowlist(config):
        return "not_in_allowlist"
    tok = token_loader() or ""
    if not tok:
        return "no_token"
    fwd_headers["authorization"] = f"Bearer {tok}"
    fwd_headers["anthropic-beta"] = "oauth-2025-04-20"
    fwd_headers.pop("x-api-key", None)
    return "injected"


_SUMMARIZE_PROMPT = (
    "Summarize this conversation in 5 bullet points. "
    "Preserve: decisions made, tool results, unresolved questions, key facts. Be terse."
)


async def _call_summarizer(
    tail_msgs: list[dict],
    config: dict[str, Any] | None = None,
) -> str | None:
    """POST tail messages to DeepSeek directly (bypassing Lineman) for summarization.

    Uses DEEPSEEK_API_KEY and LINEMAN_IPROYAL_URL env vars. Returns None on any error.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None

    proxy_url = os.environ.get("LINEMAN_IPROYAL_URL", "") or None

    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": _SUMMARIZE_PROMPT}] + tail_msgs,
        "max_tokens": 300,
        "stream": False,
    }
    try:
        kwargs: dict[str, Any] = dict(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8.0),
        )
        if proxy_url:
            kwargs["proxy"] = proxy_url
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                "https://api.deepseek.com/v1/chat/completions", **kwargs
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content") or None
                return None
    except Exception:
        return None


async def summarise_addendums(
    req_body: bytes,
    config: dict[str, Any] | None = None,
) -> tuple[bytes, int, int]:
    """Compress conversation tail when it dominates the useful prompt.

    Returns (new_body, tail_tokens_before, tail_tokens_after).
    tail_after == tail_before means no change applied.
    """
    try:
        data = json.loads(req_body)
    except Exception:
        return req_body, 0, 0

    messages = data.get("messages")
    if not isinstance(messages, list) or len(messages) < 4:
        return req_body, 0, 0

    system_tokens = sum(
        _count_message_tokens(m.get("content", ""))
        for m in messages if m.get("role") == "system"
    )
    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"), None
    )
    if last_user is None:
        return req_body, 0, 0

    user_tokens = _count_message_tokens(last_user.get("content", ""))
    useful = system_tokens + user_tokens
    if useful == 0:
        return req_body, 0, 0

    tail_msgs = [
        m for m in messages
        if m.get("role") != "system" and m is not last_user
    ]
    tail_tokens = sum(
        _count_message_tokens(m.get("content", "")) for m in tail_msgs
    )

    # Compress when tail is large enough to warrant it.
    # Three-part guard (skip if ALL are true would compress too early on small agents):
    #   1. Minimum absolute size 1500 tokens — don't compress tiny histories.
    #   2. Proportional threshold 30% of useful — prevents aggressive compression
    #      on agents with tiny system prompts (e.g. 200-token useful → skip < 60 was
    #      triggering on every turn; now requires tail > 30% of useful).
    #   3. Absolute cap 6K — always compress before the 80K block fires.
    if tail_tokens < 1_500:
        return req_body, tail_tokens, tail_tokens
    if tail_tokens <= 0.3 * useful and tail_tokens < 6_000:
        return req_body, tail_tokens, tail_tokens

    summary = await _call_summarizer(tail_msgs, config)
    if not summary:
        return req_body, tail_tokens, tail_tokens

    new_messages = [m for m in messages if m.get("role") == "system"]
    new_messages.append({"role": "user", "content": f"[Context summary]:\n{summary}"})
    new_messages.append(last_user)
    data["messages"] = new_messages

    new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    summary_tokens = len(summary) // 4
    return new_body, tail_tokens, summary_tokens


UPSTREAM_MAP: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "google": "https://generativelanguage.googleapis.com",
}

# Минимальное число токенов system-prompt, после которого Lineman сам
# включает Anthropic ephemeral prompt-caching. Кэшированные input-токены
# в Anthropic стоят 0.1× от обычных — экономия до 90% на повторяющемся
# system prompt в длинных сессиях.
_CACHE_MIN_SYSTEM_TOKENS = 1024


def _inject_anthropic_prompt_cache(req_body: bytes) -> bytes:
    """Анализирует Anthropic-style payload и помечает большой system-prompt
    как `cache_control: {"type": "ephemeral"}`. Идемпотентно — если cache_control
    уже есть, ничего не меняет. Тихо возвращает оригинал при любой ошибке.
    """
    if not req_body or len(req_body) < 2000:
        return req_body
    try:
        data = json.loads(req_body)
    except Exception:
        return req_body
    if not isinstance(data, dict):
        return req_body
    system = data.get("system")
    changed = False
    # Anthropic поддерживает `system` либо строкой, либо списком блоков
    if isinstance(system, str) and len(system) // 4 >= _CACHE_MIN_SYSTEM_TOKENS:
        data["system"] = [{"type": "text", "text": system,
                           "cache_control": {"type": "ephemeral"}}]
        changed = True
    elif isinstance(system, list):
        for block in system:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            if (isinstance(text, str)
                    and len(text) // 4 >= _CACHE_MIN_SYSTEM_TOKENS
                    and "cache_control" not in block):
                block["cache_control"] = {"type": "ephemeral"}
                changed = True
    if not changed:
        return req_body
    return json.dumps(data, ensure_ascii=False).encode("utf-8")

_READ_TIMEOUT = 30.0
_STREAM_TIMEOUT = 180.0
_BODY_MAX = 4 * 1024 * 1024
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503})

# --- Жёсткий дневной кап токенов на провайдера (deepseek жёг миллионы/день) ---
from datetime import datetime as _dt, timezone as _tz, timedelta as _td  # noqa: E402
from token_cap import DailyTokenCap  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
BASE_DIR = _Path(__file__).resolve().parent  # lineman module dir; fallback для db_path (fix NameError 2026-07-06)

_MSK = _tz(_td(hours=3))
_TOKEN_CAP: DailyTokenCap | None = None


def _today_msk() -> str:
    return _dt.now(_MSK).strftime("%Y-%m-%d")


def _get_token_cap(config: dict) -> DailyTokenCap:
    """Singleton кап из config.reverse_proxy.daily_token_caps, сид из request_log (today)."""
    global _TOKEN_CAP
    if _TOKEN_CAP is not None:
        return _TOKEN_CAP
    rp_cfg = (config or {}).get("reverse_proxy", {}) or {}
    caps = rp_cfg.get("daily_token_caps", {}) or {}
    agent_caps = rp_cfg.get("daily_agent_token_caps", {}) or {}
    cap = DailyTokenCap(caps, agent_caps)
    day = _today_msk()
    db = (config or {}).get("db_path") or str(BASE_DIR / "lineman.db")

    def _seed_q(where_sql: str, params: tuple):
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) "
                "FROM request_log WHERE " + where_sql, params).fetchone()
            return int(row[0]) if row and row[0] else 0
        finally:
            con.close()

    # Сид: рестарт Lineman не должен обнулять дневной расход (иначе кап обходится рестартом).
    for prov in cap.caps:
        try:
            cap.seed(prov, _seed_q("llm_provider=? AND timestamp >= ?",
                                   (prov, day + "T00:00:00")), day)
        except Exception:
            pass
    # Per-agent сид: request_log хранит source_agent как "career-bot" ИЛИ "claude-cli:career-bot".
    for ag, provmap in cap.agent_caps.items():
        for prov in provmap:
            try:
                cap.seed(prov, _seed_q(
                    "llm_provider=? AND timestamp >= ? AND "
                    "(source_agent=? OR source_agent=?)",
                    (prov, day + "T00:00:00", ag, f"claude-cli:{ag}")), day, agent=ag)
            except Exception:
                pass
    _TOKEN_CAP = cap
    return _TOKEN_CAP

# Paths that bypass body buffering — streamed directly to upstream.
# Used for large uploads (Gemini File API, etc.) where body can exceed _BODY_MAX.
_PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "/upload/",  # Google File API: /upload/v1beta/files
)
_MAX_UPSTREAM_RETRIES = 2
_HOP_BY_HOP = frozenset({
    "host", "connection", "proxy-connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade",
})


async def _stream_body(reader: asyncio.StreamReader, content_length: int):
    """Async generator: yield body chunks from TCP stream without buffering all."""
    remaining = content_length
    while remaining > 0:
        chunk = await asyncio.wait_for(
            reader.read(min(65536, remaining)), timeout=300
        )
        if not chunk:
            break
        yield chunk
        remaining -= len(chunk)


def _google_api_key() -> str:
    """Ключ Gemini ТОЛЬКО у Lineman: GEMINI_LINEMAN_API_TOKEN (новый эксклюзивный ключ).
    Агенты ходят через /proxy/google БЕЗ ключа — Lineman его инжектит. Транзишн-фолбэк на
    старый GEMINI_API_KEY / openclaw.json, пока новый не прилетел от Ключника."""
    k = os.environ.get("GEMINI_LINEMAN_API_TOKEN") or os.environ.get("GEMINI_API_KEY")
    if k and k.strip():
        return k.strip()
    try:
        with open(os.path.expanduser("~/.openclaw/openclaw.json")) as _f:
            _oc = json.load(_f)
        return (_oc.get("models", {}).get("providers", {}).get("google", {}).get("apiKey", "") or "").strip()
    except Exception:
        return ""


def _strip_query_key(url: str) -> str:
    """Срезать любой клиентский key=... из query — Lineman поставит свой эксклюзивный."""
    if "key=" not in url:
        return url
    base, _, query = url.partition("?")
    if not query:
        return url
    kept = [p for p in query.split("&") if not p.startswith("key=")]
    return base + ("?" + "&".join(kept) if kept else "")


def _drop_client_google_creds(headers: dict[str, str]) -> None:
    """Убрать клиентский google-ключ из заголовков (любой регистр) — ключ только у Lineman."""
    for hk in [k for k in headers if k.lower() == "x-goog-api-key"]:
        headers.pop(hk, None)


async def _handle_passthrough(
    method: str,
    provider: str,
    rest_path: str,
    upstream_base: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: aiohttp.ClientSession,
    *,
    pool: Any = None,
    config: dict[str, Any] | None = None,
    source_ip: str = "",
) -> None:
    """Streaming passthrough for large uploads — pipes body without buffering.

    Skips body inspection, circuit breaker, dedup, and token counting.
    Logs only: provider, path, content_length, status, latency.
    """
    # Read headers
    req_headers: dict[str, str] = {}
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                req_headers[k.lower()] = v
    except asyncio.TimeoutError:
        await _send_json_error(writer, 408, "Timeout reading headers")
        return

    content_length = int(req_headers.get("content-length", "0") or "0")

    # Build upstream URL
    upstream_url = upstream_base.rstrip("/") + rest_path

    # Google: Lineman — ЕДИНСТВЕННЫЙ держатель ключа. Срезаем любой клиентский ключ
    # (URL key= и заголовок x-goog-api-key) и инжектим эксклюзивный ключ Lineman.
    if provider == "google":
        _drop_client_google_creds(req_headers)
        upstream_url = _strip_query_key(upstream_url)
        gkey = _google_api_key()
        if gkey:
            sep = "&" if "?" in upstream_url else "?"
            upstream_url += sep + "key=" + gkey

    fwd_headers = {k: v for k, v in req_headers.items() if k not in _HOP_BY_HOP}
    fwd_headers["accept-encoding"] = "gzip, deflate, identity"
    if content_length:
        fwd_headers["content-length"] = str(content_length)

    # Proxy pool
    use_proxy: str | None = None
    if pool is not None:
        try:
            from urllib.parse import urlparse
            up_host = urlparse(upstream_base).hostname or ""
            use_proxy, _ = pool.select(up_host)
        except Exception:
            pass

    t_start = time.monotonic()
    status_code = 502

    body_stream = _stream_body(reader, content_length) if content_length > 0 else None
    req_kwargs: dict[str, Any] = dict(
        method=method,
        url=upstream_url,
        headers=fwd_headers,
        data=body_stream,
        timeout=aiohttp.ClientTimeout(total=600),
    )
    if use_proxy:
        req_kwargs["proxy"] = use_proxy

    try:
        async with session.request(**req_kwargs) as resp:
            status_code = resp.status
            resp_body = await resp.read()
            resp_hdr_lines = "".join(
                f"{k}: {v}\r\n" for k, v in resp.headers.items()
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
            )
            header_block = (
                f"HTTP/1.1 {status_code} {resp.reason or ''}\r\n"
                + resp_hdr_lines
                + f"Content-Length: {len(resp_body)}\r\n\r\n"
            )
            writer.write(header_block.encode())
            writer.write(resp_body)
            await writer.drain()
    except Exception as exc:
        await _send_json_error(writer, 502, f"Passthrough upstream error: {exc}")
        return

    latency_ms = round((time.monotonic() - t_start) * 1000)
    logger.info(
        "passthrough",
        provider=provider,
        path=rest_path,
        method=method,
        content_length=content_length,
        status=status_code,
        latency_ms=latency_ms,
        source_ip=source_ip,
    )


async def handle_reverse_proxy(
    method: str,
    request_path: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: aiohttp.ClientSession,
    *,
    db: Any = None,
    source_ip: str = "",
    pool: Any = None,
    config: dict[str, Any] | None = None,
    signals: Any = None,
    breaker: Any = None,
    dedup: Any = None,
    router: Any = None,
) -> None:
    """Handle /proxy/{provider}/... — forward to upstream and log tokens."""

    # Parse /proxy/{provider}/{rest}
    parts = request_path.lstrip("/").split("/", 2)  # ["proxy", "deepseek", "v1/chat/..."]
    if len(parts) < 2:
        await _send_json_error(writer, 400, "Bad proxy path")
        return

    provider = parts[1]
    rest_path = "/" + parts[2] if len(parts) > 2 else "/"

    # ГАРД 3.1-pro: у gemini-3.1-pro мусорный лимит 250 RPD даже на Tier1 (выжигается аудитором/
    # сервисами за часы → 429 всей федерации). Переписываем на gemini-2.5-pro (1K RPD, 2M TPM)
    # на ШЛЮЗЕ — никто не может жечь общий 250/день. Модель google в URL-пути.
    if provider == "google" and "gemini-3.1-pro" in rest_path:
        rest_path = re.sub(r"gemini-3\.1-pro(-preview|-latest)?", "gemini-2.5-pro", rest_path)
        logger.info("gemini_31pro_rewritten", path=rest_path[:80])

    upstream_base = _resolve_upstream(provider, config)
    if not upstream_base:
        await _send_json_error(writer, 400, f"Unknown provider: {provider!r}")
        return

    # Streaming passthrough for large uploads (no body buffering or inspection)
    if any(rest_path.startswith(p) for p in _PASSTHROUGH_PREFIXES):
        await _handle_passthrough(
            method, provider, rest_path, upstream_base,
            reader, writer, session,
            pool=pool, config=config, source_ip=source_ip,
        )
        return

    # Read remaining HTTP request headers from the TCP stream
    req_headers: dict[str, str] = {}
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                req_headers[k.lower()] = v
    except asyncio.TimeoutError:
        await _send_json_error(writer, 408, "Timeout reading headers")
        return

    # Read request body
    content_length = int(req_headers.get("content-length", "0") or "0")
    req_body = b""
    if content_length > 0:
        try:
            req_body = await asyncio.wait_for(
                reader.readexactly(min(content_length, _BODY_MAX)),
                timeout=_READ_TIMEOUT,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            await _send_json_error(writer, 408, "Timeout reading body")
            return

    # Extract metadata from request JSON
    req_model = ""
    is_streaming = False
    req_json: dict | None = None
    try:
        req_json = json.loads(req_body)
        req_model = req_json.get("model", "")
        is_streaming = bool(req_json.get("stream", False))
        # Strip "provider/" prefix from model name if present (e.g. "deepseek/deepseek-v4-flash" → "deepseek-v4-flash").
        # OpenClaw sends composite model strings; cloud upstream APIs expect bare model names.
        # ВАЖНО: для lm-studio НЕ резать — там префикс это ИЗДАТЕЛЬ и часть реального id
        # модели (напр. "qwen/qwen3.5-9b"). Срезание → LM Studio видит "qwen3.5-9b" и
        # поднимает ВТОРОЙ дубль-инстанс рядом с загруженным "qwen/qwen3.5-9b".
        if "/" in req_model and req_body and provider != "lm-studio":
            bare_model = req_model.split("/", 1)[1]
            req_json["model"] = bare_model
            req_body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
            req_model = bare_model
    except Exception:
        pass

    # Router: rewrite provider/model for BATCH context → local Ollama on hoster.
    # Only applies to deepseek requests — Google body schema is incompatible with Ollama.
    # Skip if request contains tools — Ollama rejects tool-schema requests.
    _has_tools = bool(req_json.get("tools")) if req_json else False
    if router is not None and req_body and provider == "deepseek" and not _has_tools:
        try:
            _rctx = router.detect_context(req_body, req_headers)
            if _rctx == RouteContext.BATCH:
                _batch_route = router.resolve(_rctx)
                _new_upstream = _resolve_upstream(_batch_route.provider, config)
                if _new_upstream:
                    try:
                        _bd = json.loads(req_body)
                        _bd["model"] = _batch_route.model
                        _new_body = json.dumps(_bd, ensure_ascii=False).encode("utf-8")
                        # Commit rewrite atomically — only after JSON parse succeeds
                        req_body = _new_body
                        req_model = _batch_route.model
                        provider = _batch_route.provider
                        upstream_base = _new_upstream
                        # Strip caller's auth header — Ollama does not need it
                        req_headers.pop("authorization", None)
                        logger.info(
                            "router_batch_rewrite",
                            new_provider=provider,
                            new_model=req_model,
                            source_ip=source_ip,
                        )
                    except Exception:
                        pass
                else:
                    logger.warning(
                        "router_batch_no_upstream",
                        provider=_batch_route.provider,
                        source_ip=source_ip,
                    )
        except Exception:
            pass

    # Extract agent identity and prompt for signal attribution
    agent_name = _extract_agent_name(req_headers)
    prompt_snippet = _extract_prompt_snippet(req_body)

    # Пер-агентный гейт Gemini Pro: без активного гранта pro-модель → база (gemini-3.5-flash).
    # Глобальный guard 3.1-pro→2.5-pro уже применён к rest_path выше (до диспетча).
    if provider == "google":
        from gemini_pro import apply_pro_gate, get_grants
        _new_path = apply_pro_gate(rest_path, agent_name, get_grants(), config)
        if _new_path != rest_path:
            logger.info("gemini_pro_downgrade", agent=agent_name or "(unknown)",
                        from_path=rest_path[:60], to_path=_new_path[:60])
            rest_path = _new_path
        if not req_model:
            _gm = re.search(r"models/([^:/?]+)", rest_path)
            if _gm:
                req_model = _gm.group(1)

    # Context limit — block unnamed agents with runaway context
    if not agent_name and req_body:
        from db import source_host_from_ip as _shost_fn
        _src = _shost_fn(source_ip)
        if _src in _UNNAMED_CTX_BLOCKED_HOSTS:
            try:
                _msgs = json.loads(req_body).get("messages") or []
                _est = sum(_count_message_tokens(m.get("content", "")) for m in _msgs)
            except Exception:
                _est = len(req_body) // 4
            if _est > _UNNAMED_CTX_LIMIT:
                logger.warning(
                    "unnamed_ctx_limit_blocked",
                    source_ip=source_ip,
                    source_host=_src,
                    estimated_tokens=_est,
                    provider=provider,
                )
                _detail = (
                    f"[LINEMAN GUARD] Контекст ~{_est // 1000}K токенов "
                    f"(лимит {_UNNAMED_CTX_LIMIT // 1000}K). "
                    "Запрос заблокирован. Для анализа нужны детали: "
                    "какую задачу ты сейчас решаешь, какой последний результат, "
                    "на чём застрял? Сохрани ключевые факты и начни новую сессию."
                )
                await _send_json_error(writer, 429, _detail)
                return

    # Hard-ceiling контекста для ИМЕНОВАННЫХ агентов (anthropic — главный жор). Режем абсурдные
    # одиночные запросы (500k-999k токенов) ДО форварда — экономия + сигнал «суммаризируй».
    if agent_name and req_body and provider == "anthropic":
        _hard = int(((config or {}).get("reverse_proxy", {}) or {}).get(
            "ctx_hard_limit", _NAMED_CTX_HARD_LIMIT) or 0)
        if _hard > 0:
            try:
                _j = json.loads(req_body)
                _est = sum(_count_message_tokens(m.get("content", "")) for m in (_j.get("messages") or []))
                _est += _count_message_tokens(_j.get("system", ""))
            except Exception:
                _est = len(req_body) // 4
            if _est > _hard:
                logger.warning("named_ctx_hard_blocked", agent=agent_name,
                               estimated_tokens=_est, limit=_hard, provider=provider)
                await _send_json_error(writer, 429,
                    f"[LINEMAN GUARD] Контекст ~{_est // 1000}K токенов (потолок {_hard // 1000}K). "
                    "Запрос заблокирован: слишком большой контекст на один вызов. "
                    "Суммаризируй/очисти историю и повтори меньшим контекстом.")
                return

    # Circuit breaker — check before forwarding
    if breaker is not None:
        blocked, cb_reason = breaker.check(
            source_ip, len(req_body), provider, req_model, agent_name
        )
        if blocked:
            logger.warning(
                "rproxy_cb_blocked",
                source_ip=source_ip,
                provider=provider,
                model=req_model,
                reason=cb_reason,
            )
            asyncio.create_task(
                breaker.alert(source_ip, cb_reason, provider, req_model, agent_name)
            )
            if signals is not None:
                try:
                    from db import source_host_from_ip
                    asyncio.create_task(signals.async_enqueue({
                        "ts": time.time(),
                        "from_agent": agent_name,
                        "from_node": source_host_from_ip(source_ip),
                        "to_service": provider,
                        "type": "error",
                        "model": req_model or None,
                        "tokens_in": None,
                        "tokens_out": None,
                        "latency_ms": 0,
                        "prompt_snippet": f"[CB BLOCKED] {cb_reason}",
                        "status": "error",
                    }))
                except Exception:
                    pass
            await _send_json_error(writer, 429, f"Circuit breaker: {cb_reason}")
            return

    # Daily token cap per provider — жёсткий потолок (deepseek жёг миллионы/день)
    _cap = _get_token_cap(config)
    _day = _today_msk()
    if not _cap.allow(provider, _day):
        logger.warning("rproxy_token_cap_blocked", provider=provider, day=_day,
                       used=_cap.status(_day).get(provider, {}).get("used"))
        await _send_json_error(
            writer, 429,
            f"Дневной лимит токенов провайдера '{provider}' исчерпан на {_day} "
            f"(контроль траты). Сброс в полночь MSK. См. /api/token-caps.")
        return
    # Per-agent дневной кап — один жадный потребитель не роняет общий аккаунт
    # (career-bot жёг 71M anthropic/сутки, rate-limit прилетал ВСЕМ). 2026-07-05.
    if agent_name and not _cap.allow_agent(agent_name, provider, _day):
        logger.warning("rproxy_agent_cap_blocked", agent=agent_name,
                       provider=provider, day=_day,
                       cap=_cap.agent_cap(agent_name, provider))
        await _send_json_error(
            writer, 429,
            f"[LINEMAN GUARD] Агент '{agent_name}': дневной лимит токенов "
            f"'{provider}' исчерпан на {_day}. Суммаризируй/уменьши контекст или "
            f"перейди на deepseek/local. Сброс в полночь MSK.")
        return

    # Dedup cache — check for identical request, record call for retry analysis
    _dedup_key: str | None = None
    if dedup is not None and req_body:
        _dedup_key = dedup.req_hash(req_body)
        if _dedup_key:
            cached = dedup.get_cached(_dedup_key)
            if cached is not None:
                cached_status, cached_body = cached
                logger.info(
                    "dedup_cache_hit",
                    key=_dedup_key, provider=provider, model=req_model,
                    source_ip=source_ip,
                )
                # Return cached response immediately — no upstream call
                try:
                    writer.write(
                        f"HTTP/1.1 {cached_status} OK\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {len(cached_body)}\r\n"
                        f"X-Dedup-Cache: hit\r\n\r\n".encode()
                    )
                    writer.write(cached_body)
                    await writer.drain()
                    writer.close()
                except Exception:
                    pass
                # Emit dedup signal
                if signals is not None:
                    try:
                        from db import source_host_from_ip
                        asyncio.create_task(signals.async_enqueue({
                            "ts": time.time(),
                            "from_agent": agent_name,
                            "from_node": source_host_from_ip(source_ip),
                            "to_service": provider,
                            "type": "error",
                            "model": req_model or None,
                            "tokens_in": None, "tokens_out": None, "latency_ms": 0,
                            "prompt_snippet": f"[DEDUP HIT] {_dedup_key} — cached response returned",
                            "status": "ok",
                        }))
                    except Exception:
                        pass
                return

            # Record call + check if this is a retry loop
            is_retry, retry_count = dedup.record_and_check(
                _dedup_key, source_ip, provider, req_model, agent_name
            )
            if is_retry:
                logger.warning(
                    "dedup_retry_loop",
                    key=_dedup_key, count=retry_count,
                    source_ip=source_ip, provider=provider,
                )

    # Tail compression: summarise history if it dominates the prompt
    compression_applied = 0
    tail_tokens_before = 0
    tail_tokens_after = 0
    if req_body and req_model:
        try:
            req_body, tail_tokens_before, tail_tokens_after = await summarise_addendums(
                req_body
            )
            if tail_tokens_before != tail_tokens_after:
                compression_applied = 1
                logger.info(
                    "tail_compressed",
                    agent=agent_name,
                    provider=provider,
                    tail_before=tail_tokens_before,
                    tail_after=tail_tokens_after,
                )
        except Exception:
            pass

    # Anthropic ephemeral prompt cache — для повторяющегося system-prompt.
    # Anthropic API даёт ~90% скидку на cached input. Lineman сам добавляет
    # cache_control блоку, агент про это знать не обязан.
    if req_body and provider == "anthropic":
        try:
            req_body = _inject_anthropic_prompt_cache(req_body)
        except Exception:
            pass

    # Build forwarded headers
    fwd_headers: dict[str, str] = {
        k: v for k, v in req_headers.items() if k not in _HOP_BY_HOP
    }
    # Restrict to encodings aiohttp can decompress (no brotli support)
    fwd_headers["accept-encoding"] = "gzip, deflate, identity"
    if req_body:
        fwd_headers["content-length"] = str(len(req_body))

    upstream_url = upstream_base.rstrip("/") + rest_path

    # Anthropic: инжект Klod OAuth Bearer для агентов из allowlist. Claude Code CLI
    # у внешних агентов (ltx и т.п.) шлёт свой ключ через x-api-key/Authorization,
    # но реальным Anthropic-доступом владеет только Klod. Разрешённым по имени
    # агентам Lineman срезает клиентские креды и подставляет свой OAuth
    # (тот же ~/.claude/.credentials.json, что использует /api/klod/ask).
    # Гейт задаётся в config.reverse_proxy.anthropic_agent_allowlist (список agent_name).
    _oauth_status = maybe_inject_anthropic_oauth(
        provider, agent_name, fwd_headers, config)
    if _oauth_status == "injected":
        logger.info("anthropic_oauth_injected", agent=agent_name)
    elif _oauth_status == "no_token":
        logger.warning("anthropic_oauth_missing", agent=agent_name)
        await _send_json_error(
            writer, 503,
            "Klod anthropic OAuth недоступен — обратись к klod-access.")
        return

    # Google: Lineman — ЕДИНСТВЕННЫЙ держатель ключа. Срезаем любой клиентский ключ
    # (URL key= и заголовок x-goog-api-key) и инжектим эксклюзивный ключ Lineman.
    if provider == "google":
        _drop_client_google_creds(fwd_headers)
        upstream_url = _strip_query_key(upstream_url)
        gkey = _google_api_key()
        if gkey:
            sep = "&" if "?" in upstream_url else "?"
            upstream_url = upstream_url + sep + "key=" + gkey

    # Inject DeepSeek API key if not already in Authorization header
    if provider == "deepseek" and "authorization" not in {k.lower() for k in fwd_headers}:
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if ds_key:
            fwd_headers["authorization"] = f"Bearer {ds_key}"

    # Proxy pool selection
    use_proxy: str | None = None
    proxy_id = "direct"
    if pool is not None:
        try:
            from urllib.parse import urlparse
            up_host = urlparse(upstream_base).hostname or ""
            use_proxy, proxy_id = pool.select(up_host)
        except Exception:
            pass

    t_start = time.monotonic()
    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read = 0
    status_code = 502
    error_str: str | None = None

    req_kwargs: dict[str, Any] = dict(
        method=method,
        url=upstream_url,
        headers=fwd_headers,
        data=req_body or None,
        timeout=aiohttp.ClientTimeout(total=_STREAM_TIMEOUT),
    )
    if use_proxy:
        req_kwargs["proxy"] = use_proxy

    for _attempt in range(_MAX_UPSTREAM_RETRIES + 1):
        _final = (_attempt == _MAX_UPSTREAM_RETRIES)
        try:
            async with session.request(**req_kwargs) as resp:
                status_code = resp.status
                ct = resp.headers.get("Content-Type", "")
                uses_sse = "text/event-stream" in ct or is_streaming

                # Transparent retry on 5xx: drain body, back off, retry
                if status_code in _RETRYABLE_STATUSES and not _final:
                    await resp.read()
                    _delay = min(1.5 ** _attempt, 8.0)
                    logger.warning(
                        "rproxy_5xx_retry",
                        status=status_code,
                        attempt=_attempt + 1,
                        provider=provider,
                        delay=round(_delay, 1),
                    )
                    await asyncio.sleep(_delay)
                    continue

                # Build response header block (strip hop-by-hop + encoding headers)
                resp_hdr_lines = ""
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "content-encoding", "content-length"):
                        continue
                    resp_hdr_lines += f"{k}: {v}\r\n"

                if uses_sse:
                    # Streaming: re-encode as chunked, capture tail for usage extraction
                    header_block = (
                        f"HTTP/1.1 {status_code} {resp.reason or ''}\r\n"
                        + resp_hdr_lines
                        + "Transfer-Encoding: chunked\r\n\r\n"
                    )
                    writer.write(header_block.encode())
                    await writer.drain()

                    tail_buf = bytearray()
                    async for chunk in resp.content.iter_chunked(8192):
                        if chunk:
                            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            await writer.drain()
                            tail_buf.extend(chunk)
                            if len(tail_buf) > 8192:
                                tail_buf = tail_buf[-8192:]

                    writer.write(b"0\r\n\r\n")
                    await writer.drain()

                    tokens_in, tokens_out, cache_read = _extract_usage_sse(bytes(tail_buf))
                else:
                    # Non-streaming: buffer full response, extract usage
                    resp_body = await resp.read()

                    header_block = (
                        f"HTTP/1.1 {status_code} {resp.reason or ''}\r\n"
                        + resp_hdr_lines
                        + f"Content-Length: {len(resp_body)}\r\n\r\n"
                    )
                    writer.write(header_block.encode())
                    writer.write(resp_body)
                    await writer.drain()

                    tokens_in, tokens_out, cache_read = _extract_usage_json(resp_body)

                    # Store in dedup cache (non-streaming only)
                    if dedup is not None and _dedup_key and status_code == 200:
                        dedup.store(_dedup_key, status_code, resp_body)

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            error_str = str(exc)
            if not _final:
                _delay = min(1.5 ** _attempt, 8.0)
                logger.warning(
                    "rproxy_exc_retry",
                    attempt=_attempt + 1,
                    provider=provider,
                    error=str(exc),
                    delay=round(_delay, 1),
                )
                await asyncio.sleep(_delay)
                continue
            logger.warning("rproxy_upstream_error", provider=provider,
                           url=upstream_url, error=error_str)
            await _send_json_error(writer, 502, f"Upstream error: {exc}")
        except Exception as exc:
            error_str = str(exc)
            logger.exception("rproxy_unexpected_error", provider=provider)
            await _send_json_error(writer, 500, "Internal proxy error")
        break

    latency_ms = int((time.monotonic() - t_start) * 1000)

    try:
        writer.close()
    except Exception:
        pass

    # Record call in circuit breaker window (after response, uses actual body size)
    if breaker is not None:
        try:
            breaker.record(source_ip, len(req_body))
        except Exception:
            pass

    if pool is not None:
        try:
            pool.record(proxy_id, success=(status_code < 500), latency_ms=latency_ms)
        except Exception:
            pass

    # Log to DB
    if db is not None:
        try:
            from db import source_host_from_ip
            # Mask individual request header values before JSON-dumping for logging
            masked_req_headers = {k: _mask_sensitive(v) for k, v in req_headers.items()} if req_headers else None
            # Учёт потраченных токенов в дневной кап провайдера (жёсткий потолок траты)
            try:
                _get_token_cap(config).record(
                    provider, (tokens_in or 0) + (tokens_out or 0), _today_msk(),
                    agent=agent_name or None)
            except Exception:
                pass
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_host": source_host_from_ip(source_ip),
                "source_agent": agent_name,
                "llm_provider": provider,
                "llm_model": req_model,
                "request_headers_masked": _mask_sensitive(json.dumps(masked_req_headers)) if masked_req_headers else None, # Apply mask twice just to be absolutely sure
                "request_body": _mask_sensitive(req_body.decode("utf-8", errors="replace"))[:4096] if req_body else None,
                "request_size": len(req_body) if req_body else 0,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_hit": 1 if cache_read else 0,
                "compression_applied": compression_applied,
                "tail_tokens_before": tail_tokens_before,
                "tail_tokens_after": tail_tokens_after,
                # #5: измеримая экономия токенов от компрессии хвоста (% урезанных токенов).
                "token_economy_pct": round(
                    (tail_tokens_before - tail_tokens_after) / tail_tokens_before * 100, 1)
                    if tail_tokens_before > 0 and tail_tokens_after < tail_tokens_before else 0.0,
                "route_applied": f"rproxy:{provider}",
                "status_code": status_code,
                "error": error_str,
                "latency_ms": latency_ms,
                "target_url": _mask_sensitive(upstream_url) if upstream_url else None,
                "target_host": provider,
            }
            masked_row = mask_row(row)
            asyncio.create_task(db.log_request(masked_row))
        except Exception:
            pass

    # Auto-emit signal for dashboard
    if signals is not None:
        try:
            from db import source_host_from_ip
            from secret_mask import mask_secrets
            asyncio.create_task(signals.async_enqueue({
                "ts": time.time(),
                "from_agent": agent_name,
                "from_node": source_host_from_ip(source_ip),
                "to_service": provider,
                "type": "prompt" if status_code < 400 else "error",
                "model": req_model or None,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
                "prompt_snippet": mask_secrets(prompt_snippet) if prompt_snippet else None,
                "status": "ok" if status_code < 400 else "error",
            }))
        except Exception:
            pass

    # Active Censor: huge context alert to klod-access inbox.
    # Rate-limited per agent (1/30мин): застрявший на 300k агент иначе плодит десятки
    # одинаковых сигналов в инбокс → флуд + трата токенов.
    if (tokens_in or 0) > 200_000:
        import time as _t
        _ctx_key = agent_name or "(unknown)"
        _now = _t.time()
        if _now - _HUGE_CTX_LAST.get(_ctx_key, 0.0) >= _HUGE_CTX_COOLDOWN_S:
            _HUGE_CTX_LAST[_ctx_key] = _now
            try:
                from klod_inbox import write_inbox
                from db import source_host_from_ip
                write_inbox(
                    from_agent=agent_name or "(unknown)",
                    node=source_host_from_ip(source_ip) or "smain",
                    message=(
                        f"⚠ huge context: tokens_in={tokens_in} model={req_model} "
                        f"provider={provider} status={status_code}. "
                        f"Compression was {'applied' if compression_applied else 'NOT applied'}. "
                        f"Consider pre-summarisation in this agent's pipeline."
                    ),
                    meta={
                        "kind": "huge_context",
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "model": req_model,
                        "provider": provider,
                        "compression_applied": bool(compression_applied),
                    },
                )
            except Exception:
                logger.exception("censor_huge_context_alert_failed")

    logger.info(
        "rproxy_done",
        provider=provider,
        model=req_model,
        path=rest_path,
        status=status_code,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        streaming=is_streaming,
        latency_ms=latency_ms,
    )


def _resolve_upstream(provider: str, config: dict[str, Any] | None) -> str:
    if config:
        upstreams = config.get("reverse_proxy", {}).get("upstreams", {})
        if provider in upstreams:
            return upstreams[provider]
    return UPSTREAM_MAP.get(provider, "")


def _extract_usage_json(body: bytes) -> tuple[int | None, int | None, int]:
    """Extract token counts from OpenAI-format or Gemini-format JSON."""
    try:
        data = json.loads(body)
        usage = data.get("usage", {})
        tin = usage.get("prompt_tokens") or usage.get("input_tokens")
        tout = usage.get("completion_tokens") or usage.get("output_tokens")
        details = usage.get("prompt_tokens_details") or {}
        cache = int(
            details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
        )
        # Gemini usageMetadata fallback
        if tin is None and tout is None:
            meta = data.get("usageMetadata", {})
            tin = meta.get("promptTokenCount")
            tout = meta.get("candidatesTokenCount")
        return tin, tout, cache
    except Exception:
        return None, None, 0


def _extract_usage_sse(tail: bytes) -> tuple[int | None, int | None, int]:
    """Scan last SSE bytes for a data: line containing usage fields."""
    try:
        text = tail.decode("utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("data:") and "[DONE]" not in line:
                json_part = line[5:].strip()
                if json_part:
                    tin, tout, cache = _extract_usage_json(json_part.encode())
                    if tin is not None or tout is not None:
                        return tin, tout, cache
    except Exception:
        pass
    return None, None, 0


async def _send_json_error(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
    body = json.dumps({"error": msg}).encode()
    try:
        writer.write(
            f"HTTP/1.1 {code} Error\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        writer.write(body)
        await writer.drain()
        writer.close()
    except Exception:
        pass
