"""HTTP forward proxy with smart routing and management API.

Listens on localhost:9090, forwards HTTP/HTTPS (CONNECT) requests
to the correct upstream, injects API keys, and logs everything.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
import structlog
from yarl import URL

from analytics import AnalyticsStore
from pool import ProxyPool
from router import Router, RouteContext
from rtk import RTK
from _http_raw import handle_tunnel, handle_http
from reverse_proxy import handle_reverse_proxy

logger = structlog.get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _resolve_api_key(env_var: str, config_path: str) -> str:
    """Resolve API key: env first, then openclaw config."""
    val = os.environ.get(env_var, "")
    if val:
        return val
    try:
        result = subprocess.run(
            ["openclaw", "config", "get", config_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def _load_config() -> dict[str, Any]:
    path = BASE_DIR / "config.json"
    with open(path) as f:
        return json.load(f)


class ProxyServer:
    """Async HTTP forward proxy with routing and API."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or _load_config()
        proxy_cfg = self._config.get("proxy_server", {})
        self._host = proxy_cfg.get("host", "127.0.0.1")
        self._port = proxy_cfg.get("port", 9090)
        self._timeout = aiohttp.ClientTimeout(
            total=proxy_cfg.get("request_timeout", 120)
        )
        self._max_connections = proxy_cfg.get("max_connections", 100)

        self._router = Router(self._config.get("routing", {}))
        self._runner: web.AppRunner | None = None
        self._upstream_session: aiohttp.ClientSession | None = None
        self._tcp_server: asyncio.Server | None = None

        # Analytics & RTK
        analytics_cfg = self._config.get("analytics", {})
        if analytics_cfg.get("enabled", True):
            self._analytics = AnalyticsStore(
                analytics_cfg.get("claude_logs_path", "~/.claude/projects/-shectory-work-/")
            )
        else:
            self._analytics = None

        rtk_cfg = self._config.get("rtk", {})
        if rtk_cfg.get("enabled", True):
            self._rtk = RTK(
                rtk_cfg.get("binary_path", "~/.local/bin/rtk")
            )
        else:
            self._rtk = None

        # Metrics
        self._request_count: int = 0
        self._error_count: int = 0
        self._start_time: float = time.time()

        # State/Health refs (populated by main.py)
        self.state: dict[str, Any] = {}
        self.metrics_store: Any = None

        # DB ref (populated by main.py after RequestLogDB.init())
        self._db: Any = None

        # Proxy pool
        self._pool = ProxyPool(self._config.get("proxy_pool", {}))

    async def start(self) -> None:
        """Start the proxy server.

        Uses a raw TCP listener so CONNECT tunneling works (aiohttp doesn't).
        """
        connector = aiohttp.TCPConnector(
            limit=self._max_connections,
            ttl_dns_cache=300,
        )
        self._upstream_session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._timeout,
        )

        # aiohttp app for HTTP-only (management API + HTTP forward proxy)
        app = web.Application()
        app.router.add_route("*", "/health", self._handle_health)
        app.router.add_route("*", "/metrics", self._handle_metrics)
        app.router.add_route("*", "/state", self._handle_state)
        app.router.add_route("GET", "/analytics", self._handle_analytics)
        app.router.add_route("POST", "/rtk", self._handle_rtk)
        app.router.add_route("*", "/{path:.*}", self._handle_proxy)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        # Raw TCP server handles everything on port 9090
        async def _raw_handler(rd: asyncio.StreamReader, wr: asyncio.StreamWriter):
            source_ip = (wr.get_extra_info("peername") or ("",))[0]
            try:
                line = await asyncio.wait_for(rd.readline(), timeout=10)
            except asyncio.TimeoutError:
                wr.close()
                return
            if not line:
                wr.close()
                return
            parts = line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) < 2:
                wr.close()
                return
            method = parts[0].upper()
            request_path = parts[1]
            request_path_only = request_path.split("?")[0]

            # Management API paths — handle locally
            if request_path_only == "/health":
                json_str = self._build_health_json()
                body = json_str.encode("utf-8")
                wr.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode())
                wr.write(body)
                await wr.drain()
                wr.close()
                return
            elif request_path_only == "/metrics":
                json_str = self._build_metrics_json()
                body = json_str.encode("utf-8")
                wr.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode())
                wr.write(body)
                await wr.drain()
                wr.close()
                return
            elif request_path_only == "/state":
                json_str = self._build_state_json()
                body = json_str.encode("utf-8")
                wr.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode())
                wr.write(body)
                await wr.drain()
                wr.close()
                return

            # Pool stats
            elif request_path_only == "/api/pool/stats" and method == "GET":
                body = json.dumps(self._pool.get_stats()).encode()
                wr.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                )
                wr.write(body)
                await wr.drain()
                wr.close()
                return
            elif request_path_only == "/api/pool/hitparade" and method == "GET":
                body = json.dumps(self._pool.hitparade()).encode()
                wr.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                )
                wr.write(body)
                await wr.drain()
                wr.close()
                return

            # Side-channel log API
            elif request_path_only == "/api/log" and method == "POST":
                await self._raw_api_log_post(rd, wr, source_ip)
                return
            elif request_path_only == "/api/log/stats" and method == "GET":
                await self._raw_api_log_stats(wr)
                return
            elif request_path_only == "/api/log" and method == "GET":
                # Drain remaining headers
                while True:
                    hdr = await asyncio.wait_for(rd.readline(), timeout=5)
                    if hdr in (b"\r\n", b"\n", b""):
                        break
                await self._raw_api_log_get(rd, wr, line)
                return

            # Reverse proxy: /proxy/{provider}/... — plaintext body inspection
            elif request_path_only.startswith("/proxy/"):
                self._request_count += 1
                await handle_reverse_proxy(
                    method, request_path, rd, wr, self._upstream_session,
                    db=self._db, source_ip=source_ip, pool=self._pool,
                    config=self._config,
                )
                return

            # CONNECT tunnel — drain remaining headers first
            if method == "CONNECT":
                while True:
                    hdr = await asyncio.wait_for(rd.readline(), timeout=5)
                    if hdr in (b"\r\n", b"\n", b""):
                        break
                self._request_count += 1
                await handle_tunnel(
                    request_path, rd, wr, self._config,
                    db=self._db, source_ip=source_ip, pool=self._pool,
                )
                return

            # HTTP proxy
            rest = await rd.read(65536)
            data = line + rest
            self._request_count += 1
            await handle_http(
                request_path, method, data, wr, self._upstream_session, self._config,
                db=self._db, source_ip=source_ip, pool=self._pool,
            )

        self._tcp_server = await asyncio.start_server(
            _raw_handler, self._host, self._port
        )
        logger.info("proxy_started", host=self._host, port=self._port)

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
        if self._upstream_session:
            await self._upstream_session.close()
        if self._runner:
            await self._runner.cleanup()
        logger.info("proxy_stopped")

    @property
    def port(self) -> int:
        return self._port

    # --- Side-channel log API (raw TCP helpers) ---

    async def _raw_api_log_post(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        source_ip: str,
    ) -> None:
        """POST /api/log — accept JSON row from OpenClaw agent, insert to DB."""
        headers: dict[str, str] = {}
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
            decoded = hdr.decode("utf-8", errors="replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                headers[k.lower()] = v

        content_length = int(headers.get("content-length", "0"))
        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                rd.read(min(content_length, 65536)), timeout=10
            )

        status = 200
        resp_body = b'{"ok":true}'
        if self._db is None:
            status = 503
            resp_body = b'{"error":"db not available"}'
        else:
            try:
                row = json.loads(body_bytes)
                if not row.get("source_ip"):
                    row["source_ip"] = source_ip
                    from db import source_host_from_ip
                    row.setdefault("source_host", source_host_from_ip(source_ip))
                loop = asyncio.get_event_loop()
                loop.create_task(self._db.log_request(row))
            except (json.JSONDecodeError, Exception) as exc:
                status = 400
                resp_body = json.dumps({"error": str(exc)}).encode()

        wr.write(
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
        )
        wr.write(resp_body)
        await wr.drain()
        wr.close()

    async def _raw_api_log_get(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        first_line: bytes,
    ) -> None:
        """GET /api/log?limit=N&source_host=X — query recent log rows."""
        from urllib.parse import urlparse, parse_qs
        # Parse query string from the first line
        first_decoded = first_line.decode("utf-8", errors="replace").strip()
        parts = first_decoded.split(" ")
        full_path = parts[1] if len(parts) > 1 else "/api/log"
        qs = parse_qs(urlparse(full_path).query)

        def _qs(key: str) -> str | None:
            vals = qs.get(key)
            return vals[0] if vals else None

        limit = int(_qs("limit") or "100")

        if self._db is None:
            body = b'{"error":"db not available"}'
            status = 503
        else:
            rows = await self._db.query_logs(
                since=_qs("since"),
                until=_qs("until"),
                limit=limit,
                source_host=_qs("source_host"),
                source_agent=_qs("source_agent"),
                llm_provider=_qs("llm_provider"),
                llm_model=_qs("llm_model"),
            )
            body = json.dumps({"rows": rows, "count": len(rows)}).encode()
            status = 200

        wr.write(
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_log_stats(self, wr: asyncio.StreamWriter) -> None:
        """GET /api/log/stats — aggregate stats."""
        if self._db is None:
            body = b'{"error":"db not available"}'
            status = 503
        else:
            stats = await self._db.get_stats()
            body = json.dumps(stats).encode()
            status = 200

        wr.write(
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def handle_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes = b"",
    ) -> dict[str, Any]:
        """Forward a request through the proxy with smart routing."""
        self._request_count += 1
        start = time.monotonic()

        parsed = URL(url)
        host = parsed.host or ""

        global_cfg = self._config.get("global", {})
        proxy_url = global_cfg.get("proxy_url", "")
        proxy6_url = global_cfg.get("proxy6_url", "")

        # Determine route context from header
        route_header = headers.get("X-Lineman-Route", "default")
        try:
            context = RouteContext(route_header)
        except ValueError:
            context = RouteContext.DEFAULT

        # Resolve model
        route = self._router.resolve(context)
        model_id = f"{route.provider}/{route.model}"

        # Determine upstream proxy selection
        use_proxy: str | None = None
        rewrite_url: str | None = None
        if host == "generativelanguage.googleapis.com":
            cf_url = global_cfg.get("gemini_cf_proxy_url", "")
            if cf_url:
                rewrite_url = url.replace("https://generativelanguage.googleapis.com", cf_url)
        elif host in ("www.googleapis.com", "gmail.googleapis.com", "docs.googleapis.com"):
            use_proxy = proxy_url or None
        elif host == "api.telegram.org":
            use_proxy = proxy6_url or None

        # Resolve API key for target host
        api_key, key_type = self._resolve_key_for_host(host)

        # Build forwarded headers
        fwd_headers = dict(headers)
        fwd_headers.pop("Proxy-Connection", None)
        fwd_headers.pop("X-Lineman-Route", None)
        fwd_headers["X-Lineman-Model"] = model_id

        if api_key:
            if key_type == "bearer":
                fwd_headers["Authorization"] = f"Bearer {api_key}"
            elif key_type == "key_param":
                fwd_headers["x-goog-api-key"] = api_key

        # Send request (with optional URL rewrite for Cloudflare reverse proxy)
        final_url = rewrite_url or url
        result = await self._forward(method, final_url, fwd_headers, body, use_proxy)

        latency_ms = (time.monotonic() - start) * 1000
        result["latency_ms"] = round(latency_ms, 2)
        result["model"] = model_id
        result["route"] = context.value

        if not result.get("success"):
            self._error_count += 1

        logger.info(
            "proxy_request",
            method=method,
            host=host,
            status=result.get("status"),
            latency_ms=round(latency_ms, 2),
            model=model_id,
            route=context.value,
            response_size=result.get("response_size", 0),
        )
        return result

    async def _handle_proxy(self, request: web.Request) -> web.Response:
        """Catch-all handler: forward to upstream or serve API."""
        if request.method == "CONNECT":
            return await self._handle_connect(request)

        url = str(request.url)
        body = await request.read()
        result = await self.handle_request(
            request.method, url, dict(request.headers), body
        )

        if not result.get("success"):
            status = result.get("status", 502)
            return web.json_response(
                {"error": result.get("error", "proxy error")},
                status=status,
            )

        # Forward response to client
        resp_headers = result.get("headers", {})
        body_bytes = result.get("body", b"")
        resp = web.Response(
            body=body_bytes,
            status=result.get("status", 200),
        )
        for hdr, val in resp_headers.items():
            if hdr.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                resp.headers[hdr] = val
        return resp

    async def _raw_connect_handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Raw TCP handler: first-line dispatch (unused, kept for reference)."""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError:
            writer.close()
            return

        if not line:
            writer.close()
            return

        parts = line.decode("utf-8", errors="replace").strip().split(" ")
        if len(parts) < 2:
            writer.close()
            return

        method = parts[0].upper()

        if method == "CONNECT":
            await self._handle_tunnel(parts[1], reader, writer)
        else:
            data = line
            rest = await reader.read(65536)
            data += rest
            url = parts[1] if len(parts) > 1 else "/"
            await self._handle_http_raw(url, method, data, writer)

    async def _handle_tunnel(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle CONNECT tunnel (legacy, unused in active path)."""
        host = target
        port = 443
        if ":" in host:
            host, port_str = host.rsplit(":", 1)
            port = int(port_str)

        global_cfg = self._config.get("global", {})
        proxy_url = global_cfg.get("proxy_url", "")
        proxy6_url = global_cfg.get("proxy6_url", "")

        use_proxy: str | None = None
        if any(g in host for g in ("generativelanguage.googleapis.com",
                                     "www.googleapis.com", "gmail.googleapis.com",
                                     "docs.googleapis.com")):
            use_proxy = proxy_url or None
        elif "api.telegram.org" in host:
            use_proxy = proxy6_url or None

        try:
            if use_proxy:
                from urllib.parse import urlparse
                import base64
                pu = urlparse(use_proxy)
                up_reader, up_writer = await asyncio.open_connection(
                    pu.hostname, pu.port or 80,
                )
                connect_req = (
                    f"CONNECT {target} HTTP/1.1\r\n"
                    f"Host: {target}\r\n"
                )
                if pu.username and pu.password:
                    creds = base64.b64encode(
                        f"{pu.username}:{pu.password}".encode()
                    ).decode()
                    connect_req += f"Proxy-Authorization: Basic {creds}\r\n"
                connect_req += "\r\n"
                up_writer.write(connect_req.encode())
                await up_writer.drain()
                resp_line = await asyncio.wait_for(up_reader.readline(), timeout=15)
                if not resp_line or b"200" not in resp_line:
                    writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{resp_line.decode(errors='replace')}".encode())
                    await writer.drain()
                    up_writer.close()
                    writer.close()
                    return
                while True:
                    hdr = await asyncio.wait_for(up_reader.readline(), timeout=10)
                    if hdr in (b"\r\n", b"\n", b""):
                        break
            else:
                up_reader, up_writer = await asyncio.open_connection(host, port)
        except (OSError, asyncio.TimeoutError) as exc:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\nCONNECT failed: {exc}\r\n".encode())
            await writer.drain()
            writer.close()
            return

        self._request_count += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        closed = asyncio.Event()

        async def relay(a: asyncio.StreamReader, b: asyncio.StreamWriter):
            try:
                while not closed.is_set():
                    data = await asyncio.wait_for(a.read(65536), timeout=60)
                    if not data:
                        break
                    b.write(data)
                    await b.drain()
            except (ConnectionError, asyncio.TimeoutError, OSError):
                pass
            finally:
                closed.set()

        await asyncio.gather(
            relay(reader, up_writer),
            relay(up_reader, writer),
        )

        up_writer.close()
        writer.close()

    async def _handle_http_raw(self, url: str, method: str, data: bytes, writer: asyncio.StreamWriter) -> None:
        """Route a raw HTTP request through aiohttp."""
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def _handle_connect(self, request: web.Request):
        """Legacy CONNECT handler for aiohttp (unused with raw TCP server)."""
        return web.Response(status=400, text="Use raw TCP proxy instead")

    async def _forward(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        proxy: str | None,
    ) -> dict[str, Any]:
        """Forward an HTTP request upstream."""
        session = self._upstream_session
        if session is None:
            return {"success": False, "error": "proxy not started"}

        try:
            kwargs: dict[str, Any] = {
                "headers": headers,
                "allow_redirects": True,
            }
            if method in ("POST", "PUT", "PATCH") and body:
                kwargs["data"] = body

            if proxy:
                kwargs["proxy"] = proxy

            req_method = getattr(session, method.lower(), session.get)
            response = await req_method(url, **kwargs)

            resp_body = await response.read()
            return {
                "success": response.status < 500,
                "status": response.status,
                "headers": dict(response.headers),
                "body": resp_body,
                "response_size": len(resp_body),
            }
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {"success": False, "error": str(exc)}

    def _resolve_key_for_host(self, host: str) -> tuple[str, str]:
        """Return (api_key, key_type) for the given host."""
        services = self._config.get("services", [])
        for svc in services:
            base_url = svc.get("base_url", "")
            if host in base_url:
                api_key = _resolve_api_key(
                    svc.get("api_key_env", ""),
                    svc.get("openclaw_config_path", ""),
                )
                svc_type = svc.get("type", "")
                if svc_type in ("gemini",):
                    return (api_key, "key_param")
                return (api_key, "bearer")

        # Google OAuth services
        if host in ("www.googleapis.com", "gmail.googleapis.com", "docs.googleapis.com"):
            token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
            if token:
                return (token, "bearer")

        return ("", "")

    # --- JSON builders for local management API ---

    def _build_health_json(self) -> str:
        """Build /health JSON string without aiohttp."""
        services_state = self.state.get("services", {})
        overall = "ok"
        degraded: list[str] = []
        down: list[str] = []
        for svc_id, svc_state in services_state.items():
            st = svc_state.get("state", "unknown")
            if st == "down":
                down.append(svc_id)
                overall = "degraded"
            elif st == "degraded":
                degraded.append(svc_id)
                if overall == "ok":
                    overall = "degraded"
        return json.dumps({
            "status": overall,
            "proxy": "running",
            "uptime_s": round(time.time() - self._start_time),
            "requests_served": self._request_count,
            "errors": self._error_count,
            "down_services": down,
            "degraded_services": degraded,
            "services": len(services_state),
        })

    def _build_metrics_json(self) -> str:
        """Build /metrics JSON string without aiohttp."""
        snapshot = {}
        if self.metrics_store is not None:
            snapshot = self.metrics_store.get_snapshot()
        return json.dumps({
            "proxy": {
                "requests_served": self._request_count,
                "errors": self._error_count,
                "uptime_s": round(time.time() - self._start_time),
            },
            "services": snapshot,
        })

    def _build_state_json(self) -> str:
        """Build /state JSON string without aiohttp."""
        return json.dumps(self.state)

    # --- HTTP API handlers (via aiohttp) ---

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health - service status summary."""
        services_state = self.state.get("services", {})
        overall = "ok"
        degraded: list[str] = []
        down: list[str] = []
        for svc_id, svc_state in services_state.items():
            st = svc_state.get("state", "unknown")
            if st == "down":
                down.append(svc_id)
                overall = "degraded"
            elif st == "degraded":
                degraded.append(svc_id)
                if overall == "ok":
                    overall = "degraded"

        return web.json_response({
            "status": overall,
            "proxy": "running",
            "uptime_s": round(time.time() - self._start_time),
            "requests_served": self._request_count,
            "errors": self._error_count,
            "down_services": down,
            "degraded_services": degraded,
            "services": len(services_state),
        })

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """GET /metrics - current metrics from MetricsStore."""
        snapshot = {}
        if self.metrics_store is not None:
            snapshot = self.metrics_store.get_snapshot()
        return web.json_response({
            "proxy": {
                "requests_served": self._request_count,
                "errors": self._error_count,
                "uptime_s": round(time.time() - self._start_time),
            },
            "services": snapshot,
        })

    async def _handle_state(self, request: web.Request) -> web.Response:
        """GET /state - full system state."""
        return web.json_response(self.state)

    async def _handle_analytics(self, request: web.Request) -> web.Response:
        """GET /analytics?period=day|week|month - token usage analytics."""
        if self._analytics is None:
            return web.json_response(
                {"error": "analytics disabled"}, status=404
            )
        period = request.query.get("period", "day")
        data = self._analytics.get_analytics(period)
        return web.json_response(data)

    async def _handle_rtk(self, request: web.Request) -> web.Response:
        """POST /rtk - execute a dev command through RTK.

        Body: {"command": "...", "cwd": "/optional/path"}
        """
        if self._rtk is None:
            return web.json_response(
                {"error": "rtk disabled"}, status=404
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response(
                {"error": "invalid JSON"}, status=400
            )

        command = body.get("command", "").strip()
        if not command:
            return web.json_response(
                {"error": "command required"}, status=400
            )

        cwd = body.get("cwd")
        result = self._rtk.exec(command, cwd)
        return web.json_response(result)
