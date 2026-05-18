"""HTTP raw handler for Lineman proxy (CONNECT tunnel + HTTP forwarding).

v4: proxy pool support, token-aware route_applied label.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from yarl import URL
import structlog

logger = structlog.get_logger(__name__)

_RELAY_TIMEOUT = 120       # max idle seconds before closing a tunnel direction
_CONNECT_TIMEOUT = 15      # upstream CONNECT handshake timeout


async def _relay_websocket(
    ws: aiohttp.ClientWebSocketResponse,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> tuple[int, int]:
    """Bidirectional relay between a client TCP stream and a WebSocket upstream."""
    bytes_out = 0
    bytes_in = 0

    async def client_to_ws() -> None:
        nonlocal bytes_out
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65536), timeout=_RELAY_TIMEOUT)
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                bytes_out += len(data)
                await ws.send_bytes(data)
        except Exception:
            pass
        finally:
            await ws.close()

    async def ws_to_client() -> None:
        nonlocal bytes_in
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    bytes_in += len(msg.data)
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    await asyncio.gather(client_to_ws(), ws_to_client(), return_exceptions=True)
    return bytes_out, bytes_in


async def handle_tunnel(
    target: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: dict[str, Any],
    *,
    db: Any = None,
    source_ip: str = "",
    pool: Any = None,
) -> None:
    """Handle CONNECT tunnel: relay between client and upstream."""
    from db import source_host_from_ip, llm_provider_from_host

    host = target
    port = 443
    if ":" in target:
        host, port_str = target.rsplit(":", 1)
        port = int(port_str)

    # Safety net: detect LLM calls arriving via CONNECT instead of /proxy/
    _llm_provider = llm_provider_from_host(host)
    if _llm_provider is not None:
        logger.warning(
            "llm_via_connect_tunnel",
            host=host,
            source_ip=source_ip,
            provider=_llm_provider,
        )
        if db is not None:
            import asyncio as _asyncio
            _asyncio.ensure_future(db.log_request({
                "source_host": source_host_from_ip(source_ip),
                "llm_provider": _llm_provider,
                "route_applied": "connect_tunnel_llm_flagged",
                "status_code": 0,
                "error": f"LLM via CONNECT tunnel: {host}",
                "request_size": 0,
            }))

    # Proxy selection: pool first, then legacy global config fallback
    use_proxy: str | None = None
    proxy_id: str = "direct"

    if pool is not None:
        use_proxy, proxy_id = pool.select(host)
    else:
        global_cfg = config.get("global", {})
        proxy_url = global_cfg.get("proxy_url", "")
        proxy6_url = global_cfg.get("proxy6_url", "")
        if any(g in host for g in (
            "generativelanguage.googleapis.com",
            "www.googleapis.com",
            "gmail.googleapis.com",
            "docs.googleapis.com",
        )):
            use_proxy = proxy_url or None
            proxy_id = "legacy_proxy" if use_proxy else "direct"
        elif "api.telegram.org" in host:
            use_proxy = proxy6_url or None
            proxy_id = "legacy_proxy6" if use_proxy else "direct"

    route_applied = f"proxy:{proxy_id}" if use_proxy else "direct"
    t_start = time.monotonic()
    error_str: str | None = None
    status_code = 200

    # --- 1a. WebSocket tunnel (CF Worker) ---
    if use_proxy and (use_proxy.startswith("wss://") or use_proxy.startswith("ws://")):
        ws_url = f"{use_proxy}?target={target}"
        try:
            _ws_session = aiohttp.ClientSession()
            _ws = await asyncio.wait_for(
                _ws_session.ws_connect(
                    ws_url,
                    timeout=aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, total=None),
                ),
                timeout=_CONNECT_TIMEOUT,
            )
        except Exception as exc:
            writer.write(
                f"HTTP/1.1 502 Bad Gateway\r\n\r\nWS tunnel connect failed: {exc}\r\n".encode()
            )
            await writer.drain()
            writer.close()
            await _ws_session.close()
            status_code = 502
            error_str = str(exc)
            _maybe_log(db, target, host, source_ip, route_applied,
                       status_code, error_str, 0, 0,
                       int((time.monotonic() - t_start) * 1000))
            if pool:
                pool.record(proxy_id, False, (time.monotonic() - t_start) * 1000)
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        logger.info("connect_tunnel_open", target=target, source_ip=source_ip, route=route_applied + "(ws)")

        bytes_c2u, bytes_u2c = await _relay_websocket(_ws, reader, writer)
        await _ws.close()
        await _ws_session.close()

        latency_ms = int((time.monotonic() - t_start) * 1000)
        logger.info("connect_tunnel_closed", target=target, source_ip=source_ip,
                    bytes_out=bytes_c2u, bytes_in=bytes_u2c, latency_ms=latency_ms, route=route_applied)
        if pool:
            pool.record(proxy_id, True, latency_ms, bytes_in=bytes_u2c, bytes_out=bytes_c2u)
        _maybe_log(db, target, host, source_ip, route_applied, 200, None,
                   bytes_out=bytes_c2u, bytes_in=bytes_u2c, latency_ms=latency_ms)
        return

    # --- 1b. HTTP CONNECT upstream ---
    try:
        if use_proxy:
            pu = urlparse(use_proxy)
            up_reader, up_writer = await asyncio.open_connection(
                pu.hostname, pu.port or 80,
            )
            creds = ""
            if pu.username and pu.password:
                creds = base64.b64encode(
                    f"{pu.username}:{pu.password}".encode()
                ).decode()
            conn_req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
            if creds:
                conn_req += f"Proxy-Authorization: Basic {creds}\r\n"
            conn_req += "\r\n"
            up_writer.write(conn_req.encode())
            await up_writer.drain()

            resp_line = await asyncio.wait_for(
                up_reader.readline(), timeout=_CONNECT_TIMEOUT
            )
            if not resp_line or b"200" not in resp_line:
                err = (resp_line.decode(errors="replace").strip()
                       if resp_line else "no response")
                writer.write(
                    f"HTTP/1.1 502 Bad Gateway\r\n\r\nProxy rejected: {err}\r\n".encode()
                )
                await writer.drain()
                writer.close()
                up_writer.close()
                status_code = 502
                error_str = f"proxy rejected: {err}"
                _maybe_log(db, target, host, source_ip, route_applied,
                           status_code, error_str, 0, 0,
                           int((time.monotonic() - t_start) * 1000))
                if pool:
                    pool.record(proxy_id, False, (time.monotonic() - t_start) * 1000)
                return
            # drain remaining CONNECT response headers
            while True:
                hdr = await asyncio.wait_for(up_reader.readline(), timeout=10)
                if hdr in (b"\r\n", b"\n", b""):
                    break
        else:
            up_reader, up_writer = await asyncio.open_connection(host, port)
    except (OSError, asyncio.TimeoutError) as exc:
        writer.write(
            f"HTTP/1.1 502 Bad Gateway\r\n\r\nCONNECT failed: {exc}\r\n".encode()
        )
        await writer.drain()
        writer.close()
        status_code = 502
        error_str = str(exc)
        _maybe_log(db, target, host, source_ip, route_applied,
                   status_code, error_str, 0, 0,
                   int((time.monotonic() - t_start) * 1000))
        if pool:
            pool.record(proxy_id, False, (time.monotonic() - t_start) * 1000)
        return

    # --- 2. Signal client that tunnel is open ---
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()

    logger.info(
        "connect_tunnel_open",
        target=target,
        source_ip=source_ip,
        route=route_applied,
    )

    # --- 3. Bidirectional relay with byte counting ---
    bytes_client_to_upstream = 0
    bytes_upstream_to_client = 0

    async def relay(
        name: str,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
    ) -> int:
        total = 0
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        src.read(65536), timeout=_RELAY_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.debug("tunnel_idle_timeout", direction=name)
                    break
                if not data:
                    break
                total += len(data)
                dst.write(data)
                try:
                    await dst.drain()
                except (ConnectionError, OSError):
                    break
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass
        return total

    results = await asyncio.gather(
        relay("client->upstream", reader, up_writer),
        relay("upstream->client", up_reader, writer),
        return_exceptions=True,
    )

    if isinstance(results[0], int):
        bytes_client_to_upstream = results[0]
    if isinstance(results[1], int):
        bytes_upstream_to_client = results[1]

    latency_ms = int((time.monotonic() - t_start) * 1000)

    logger.info(
        "connect_tunnel_closed",
        target=target,
        source_ip=source_ip,
        bytes_out=bytes_client_to_upstream,
        bytes_in=bytes_upstream_to_client,
        latency_ms=latency_ms,
        route=route_applied,
    )

    # --- 4. Cleanup ---
    for w in (up_writer, writer):
        try:
            w.close()
        except Exception:
            pass

    # --- 5. Update pool stats ---
    if pool:
        pool.record(
            proxy_id,
            success=(status_code == 200),
            latency_ms=latency_ms,
            bytes_in=bytes_upstream_to_client,
            bytes_out=bytes_client_to_upstream,
        )

    # --- 6. Log to DB ---
    _maybe_log(
        db, target, host, source_ip, route_applied, status_code, None,
        bytes_out=bytes_client_to_upstream,
        bytes_in=bytes_upstream_to_client,
        latency_ms=latency_ms,
    )


def _maybe_log(
    db: Any,
    target: str,
    host: str,
    source_ip: str,
    route_applied: str,
    status_code: int,
    error: str | None,
    bytes_out: int,
    bytes_in: int,
    latency_ms: int,
) -> None:
    """Fire-and-forget DB log if db is available."""
    if db is None:
        return
    from db import source_host_from_ip, llm_provider_from_host
    row = {
        "source_host":  source_host_from_ip(source_ip),
        "target_url":   f"https://{target}",
        "target_host":  host,
        "llm_provider": llm_provider_from_host(host),
        "route_applied": route_applied,
        "status_code":  status_code,
        "error":        error,
        "bytes_out":    bytes_out,
        "bytes_in":     bytes_in,
        "latency_ms":   latency_ms,
    }
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(db.log_request(row))
    except RuntimeError:
        pass


async def handle_http(
    url: str,
    method: str,
    data: bytes,
    writer: asyncio.StreamWriter,
    session: aiohttp.ClientSession,
    config: dict[str, Any],
    *,
    db: Any = None,
    source_ip: str = "",
    pool: Any = None,
) -> None:
    """Forward raw HTTP request through internal session."""
    from db import source_host_from_ip, llm_provider_from_host
    from yarl import URL as YarlURL

    headers_end = data.find(b"\r\n\r\n")
    header_block = (
        data[:headers_end].decode("utf-8", errors="replace")
        if headers_end >= 0
        else ""
    )
    body = data[headers_end + 4:] if headers_end >= 0 else b""

    raw_headers = {}
    for line in header_block.split("\r\n")[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            raw_headers[k.lower()] = v

    raw_headers.pop("proxy-connection", None)

    try:
        parsed = YarlURL(url)
        host = parsed.host or url
    except Exception:
        host = url

    # Proxy selection
    use_proxy: str | None = None
    proxy_id: str = "direct"
    if pool is not None:
        use_proxy, proxy_id = pool.select(host)

    route_applied = f"proxy:{proxy_id}" if use_proxy else "direct"
    t_start = time.monotonic()
    status_code = 502
    error_str: str | None = None
    resp_size = 0

    try:
        kwargs: dict[str, Any] = dict(
            method=method,
            url=url,
            headers=raw_headers,
            data=body or None,
            timeout=aiohttp.ClientTimeout(total=120),
        )
        if use_proxy:
            kwargs["proxy"] = use_proxy

        async with session.request(**kwargs) as resp:
            resp_body = await resp.read()
            status_code = resp.status
            reason = resp.reason or ""
            resp_size = len(resp_body)

        resp_line = f"HTTP/1.1 {status_code} {reason}\r\n"
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl not in ("transfer-encoding", "content-encoding"):
                resp_line += f"{k}: {v}\r\n"
        resp_line += f"Content-Length: {len(resp_body)}\r\n\r\n"
        writer.write(resp_line.encode())
        writer.write(resp_body)
        await writer.drain()
    except Exception as exc:
        error_str = str(exc)
        logger.warning("http_forward_error", url=url, error=error_str)
        err = f"Proxy error: {exc}".encode()
        hdr = f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(err)}\r\n\r\n"
        writer.write(hdr.encode())
        writer.write(err)
        await writer.drain()

    latency_ms = int((time.monotonic() - t_start) * 1000)

    try:
        writer.close()
    except Exception:
        pass

    if pool:
        pool.record(
            proxy_id,
            success=(status_code < 500),
            latency_ms=latency_ms,
            bytes_in=resp_size,
            bytes_out=len(data),
        )

    _maybe_log(
        db, url, host, source_ip, route_applied, status_code, error_str,
        bytes_out=len(data),
        bytes_in=resp_size,
        latency_ms=latency_ms,
    )
