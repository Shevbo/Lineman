"""Reverse proxy handler — transparent HTTP forward with body/token inspection.

URL format: /proxy/{provider}/{rest...}
Strips /proxy/{provider}, forwards to real upstream HTTPS,
extracts token counts from request/response bodies, logs to request_log.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


def _extract_agent_name(headers: dict[str, str]) -> str | None:
    """Return X-Agent-Name header value, or None."""
    return headers.get("x-agent-name") or headers.get("x-lineman-agent") or None


def _extract_prompt_snippet(body: bytes, max_len: int = 300) -> str | None:
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


UPSTREAM_MAP: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "google": "https://gemini-proxy-worker.bshevelev75.workers.dev",
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}

_READ_TIMEOUT = 30.0
_STREAM_TIMEOUT = 180.0
_BODY_MAX = 4 * 1024 * 1024
_HOP_BY_HOP = frozenset({
    "host", "connection", "proxy-connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade",
})


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
) -> None:
    """Handle /proxy/{provider}/... — forward to upstream and log tokens."""

    # Parse /proxy/{provider}/{rest}
    parts = request_path.lstrip("/").split("/", 2)  # ["proxy", "deepseek", "v1/chat/..."]
    if len(parts) < 2:
        await _send_json_error(writer, 400, "Bad proxy path")
        return

    provider = parts[1]
    rest_path = "/" + parts[2] if len(parts) > 2 else "/"

    upstream_base = _resolve_upstream(provider, config)
    if not upstream_base:
        await _send_json_error(writer, 400, f"Unknown provider: {provider!r}")
        return

    # Google CF Worker requires /v1beta/ prefix — rewrite bare /models/... paths
    if provider == "google" and rest_path.startswith("/models/"):
        rest_path = "/v1beta" + rest_path

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
    try:
        req_json = json.loads(req_body)
        req_model = req_json.get("model", "")
        is_streaming = bool(req_json.get("stream", False))
    except Exception:
        pass

    # Extract agent identity and prompt for signal attribution
    agent_name = _extract_agent_name(req_headers)
    prompt_snippet = _extract_prompt_snippet(req_body)

    # Build forwarded headers
    fwd_headers: dict[str, str] = {
        k: v for k, v in req_headers.items() if k not in _HOP_BY_HOP
    }

    upstream_url = upstream_base.rstrip("/") + rest_path

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

    try:
        req_kwargs: dict[str, Any] = dict(
            method=method,
            url=upstream_url,
            headers=fwd_headers,
            data=req_body or None,
            timeout=aiohttp.ClientTimeout(total=_STREAM_TIMEOUT),
        )
        if use_proxy:
            req_kwargs["proxy"] = use_proxy

        async with session.request(**req_kwargs) as resp:
            status_code = resp.status
            ct = resp.headers.get("Content-Type", "")
            uses_sse = "text/event-stream" in ct or is_streaming

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

    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        error_str = str(exc)
        logger.warning("rproxy_upstream_error", provider=provider,
                       url=upstream_url, error=error_str)
        await _send_json_error(writer, 502, f"Upstream error: {exc}")
    except Exception as exc:
        error_str = str(exc)
        logger.exception("rproxy_unexpected_error", provider=provider)
        await _send_json_error(writer, 500, "Internal proxy error")

    latency_ms = int((time.monotonic() - t_start) * 1000)

    try:
        writer.close()
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
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_host": source_host_from_ip(source_ip),
                "source_agent": agent_name,
                "llm_provider": provider,
                "llm_model": req_model,
                "request_body": req_body.decode("utf-8", errors="replace")[:4096] if req_body else None,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_hit": 1 if cache_read else 0,
                "route_applied": f"rproxy:{provider}",
                "status_code": status_code,
                "error": error_str,
                "latency_ms": latency_ms,
                "target_url": upstream_url,
                "target_host": provider,
            }
            asyncio.create_task(db.log_request(row))
        except Exception:
            pass

    # Auto-emit signal for dashboard
    if signals is not None:
        try:
            from db import source_host_from_ip
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
                "prompt_snippet": prompt_snippet,
                "status": "ok" if status_code < 400 else "error",
            }))
        except Exception:
            pass

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
