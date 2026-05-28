"""HTTP forward proxy with smart routing and management API.

Listens on localhost:9090, forwards HTTP/HTTPS (CONNECT) requests
to the correct upstream, injects API keys, and logs everything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

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
from circuit_breaker import CircuitBreaker
from dedup_cache import DedupCache

logger = structlog.get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# SSH config for remote agents (Federation Agent ID to remote host/agent mapping)
# IMPORTANT: These SSH keys must be pre-authorized on respective hosts from Lineman's host.
REMOTE_SSH_CONFIG = {
    "sdev": { # sdev is shevbo@10.66.0.4
        "user": "shevbo",
        "host_ip": "10.66.0.4",
        "key_path": "~/.ssh/id_ed25519", # Assumes this key is authorized on sdev
        "agent_map": { # Federation ID -> Remote OpenClaw agent ID
            "tank-dev": "main", # TankDev on sdev is agent 'main'
            "selfcoder-sdev": "selfcoder",
            "qaper-sdev": "qaper",
        }
    },
    "hoster": { # hoster is ubuntu@10.66.0.7
        "user": "ubuntu",
        "host_ip": "10.66.0.7",
        "key_path": "~/.ssh/id_ed25519", # Assumes this key is authorized on hoster
        "agent_map": {
            "hoster": "main", # Hoster on hoster is agent 'main'
            "shopin": "shopin",
            "resumewriter": "resumewriter",
            "inbox": "inbox",
        }
    },
    "vibe": { # vibe is boris@10.66.0.6 (Windows)
        "user": "boris",
        "host_ip": "10.66.0.6",
        "key_path": "~/.ssh/id_ed25519", # Assumes this key is authorized on vibe
        # Windows: prevent Node.js from routing localhost requests through iProyal system proxy
        "cmd_prefix": 'set "NO_PROXY=localhost,127.0.0.1,::1" && ',
        "agent_map": {
            "virtual-boris-vibe": "vboris2", # VBoris2 on vibe is agent 'vboris2'
        }
    },
    "cloud": { # cloud is shevbo@10.66.0.3 (shevbo-cloud) — stable VPS, usually online
        "user": "shevbo",
        "host_ip": "10.66.0.3",
        "key_path": "~/.ssh/id_ed25519",
        "agent_map": {
            "tank-3": "main", # Tank 3 on cloud is agent 'main'
        }
    },
    "keymaster": { # Keymaster API service is local on smain
        "user": "shectory", # dummy
        "host_ip": "127.0.0.1",
        "key_path": "", # dummy
        "agent_map": {
            "keymaster": "keymaster", # Keymaster on smain is agent 'keymaster'
        }
    },
}


def _resolve_api_key(env_var: str, config_path: str) -> str:
    """Resolve API key: env first, then openclaw config."""
    val = os.environ.get(env_var, "")
    if val:
        return val
    if not config_path:
        return ""
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


def _expand_env(obj: Any) -> Any:
    """Recursively expand ${VAR} references in config values using os.environ."""
    if isinstance(obj, str):
        return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(i) for i in obj]
    return obj


def _load_config() -> dict[str, Any]:
    path = BASE_DIR / "config.json"
    with open(path) as f:
        return _expand_env(json.load(f))


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

        # Signal queue (populated by main.py)
        self._signals: Any = None

        # Agent metadata dict (populated by main.py)
        self._agents_meta: dict[str, Any] = {}

        # Proxy pool
        self._pool = ProxyPool(self._config.get("proxy_pool", {}))

        # Telegram rate limiter: account → last send timestamp
        self._tg_rate: dict[str, float] = {}
        # Telegram message dedup: (account:chat_id:text_hash) → timestamp
        # Prevents duplicate sends when multiple nodes respond to the same message.
        self._tg_msg_dedup: dict[str, float] = {}
        self._tg_oc_path = Path.home() / ".openclaw" / "openclaw.json"

        # Circuit breaker (per-source_ip sliding window)
        self._breaker = CircuitBreaker(self._config)

        # Request dedup cache + retry analyzer
        self._dedup = DedupCache(self._config)

        # LLM concurrency queue — prevents cascade overloads under heavy load
        llm_q = proxy_cfg.get("llm_queue", {})
        self._llm_sem = asyncio.Semaphore(llm_q.get("max_concurrent", 5))
        self._llm_queue_timeout: float = llm_q.get("queue_timeout_s", 30.0)
        self._llm_hosts: frozenset[str] = frozenset(llm_q.get("hosts", [
            "api.deepseek.com", "generativelanguage.googleapis.com",
        ]))
        self._llm_providers: frozenset[str] = frozenset(llm_q.get("providers", [
            "deepseek", "google",
        ]))

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
        self._breaker.set_session(self._upstream_session)
        self._dedup.set_session(self._upstream_session)
        self._pool.set_session(self._upstream_session)

        # aiohttp app for HTTP-only (management API + HTTP forward proxy)
        app = web.Application()
        app.router.add_route("*", "/health", self._handle_health)
        app.router.add_route("*", "/metrics", self._handle_metrics)
        app.router.add_route("*", "/state", self._handle_state)
        app.router.add_route("GET", "/analytics", self._handle_analytics)
        app.router.add_route("POST", "/rtk", self._handle_rtk)

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

            # Signal API
            elif request_path_only == "/api/signal" and method == "POST":
                await self._raw_api_signal_post(rd, wr, source_ip)
                return
            elif request_path_only.startswith("/api/signals"):
                await self._raw_api_signals(rd, wr, request_path)
                return
            elif request_path_only == "/api/nodes":
                await self._raw_api_nodes(rd, wr, request_path)
                return
            elif request_path_only == "/api/report":
                await self._raw_api_report(rd, wr, request_path)
                return

            elif request_path_only == "/api/routing":
                await self._raw_api_routing(wr)
                return

            elif request_path_only == "/api/routing/decisions":
                await self._raw_api_routing_decisions(rd, wr)
                return

            elif request_path_only == "/api/tg/send" and method == "POST":
                await self._raw_api_tg_send(rd, wr)
                return

            # Agent-to-agent messaging API
            # /api/agent/{target_agent_id}/message?from=<from_agent_id>&message=<msg>
            elif request_path_only.startswith("/api/agent/"):
                await self._raw_api_agent_message(rd, wr, request_path, method)
                return

            # Dashboard static serve
            elif request_path_only in ("/dashboard", "/dashboard/"):
                await self._raw_dashboard(rd, wr)
                return

            # Reverse proxy: /proxy/{provider}/... — plaintext body inspection
            elif request_path_only.startswith("/proxy/"):
                self._request_count += 1
                parts = request_path_only.split("/")
                provider = parts[2] if len(parts) > 2 else ""
                if provider in self._llm_providers:
                    try:
                        await asyncio.wait_for(
                            self._llm_sem.acquire(), timeout=self._llm_queue_timeout
                        )
                    except asyncio.TimeoutError:
                        wr.write(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 15\r\nContent-Length: 0\r\n\r\n")
                        await wr.drain()
                        wr.close()
                        return
                    try:
                        await handle_reverse_proxy(
                            method, request_path, rd, wr, self._upstream_session,
                            db=self._db, source_ip=source_ip, pool=self._pool,
                            config=self._config, signals=self._signals,
                            breaker=self._breaker, dedup=self._dedup,
                            router=self._router,
                        )
                    finally:
                        self._llm_sem.release()
                else:
                    await handle_reverse_proxy(
                        method, request_path, rd, wr, self._upstream_session,
                        db=self._db, source_ip=source_ip, pool=self._pool,
                        config=self._config, signals=self._signals,
                        breaker=self._breaker, dedup=self._dedup,
                        router=self._router,
                    )
                return

            # CONNECT tunnel — drain remaining headers first
            if method == "CONNECT":
                while True:
                    hdr = await asyncio.wait_for(rd.readline(), timeout=5)
                    if hdr in (b"\r\n", b"\n", b""):
                        break
                self._request_count += 1
                host = request_path.split(":")[0]
                if host in self._llm_hosts:
                    try:
                        await asyncio.wait_for(
                            self._llm_sem.acquire(), timeout=self._llm_queue_timeout
                        )
                    except asyncio.TimeoutError:
                        wr.write(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 15\r\nContent-Length: 0\r\n\r\n")
                        await wr.drain()
                        wr.close()
                        return
                    try:
                        await handle_tunnel(
                            request_path, rd, wr, self._config,
                            db=self._db, source_ip=source_ip, pool=self._pool,
                        )
                    finally:
                        self._llm_sem.release()
                else:
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

        content_length = int(headers.get("content-length", "0") or "0")
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

    async def _raw_api_agent_message(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
        method: str,
    ) -> None:
        """GET /api/agent/{id}/message?from=<id>&message=<msg> -- Send message to agent."""
        # Drain headers first
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        parts = request_path.split("/")
        if len(parts) < 4: # Expected: /api/agent/{id}/message
            self._send_json_error(wr, 400, "Invalid agent message path. Usage: /api/agent/{id}/message")
            await wr.drain()
            wr.close()
            return

        target_agent_id = parts[3]

        # Parse query params
        qs = parse_qs(urlparse(request_path).query)

        def _qs(key: str) -> str | None:
            vals = qs.get(key)
            return vals[0] if vals else None

        from_agent_id = _qs("from")
        message_text = _qs("message")

        if not from_agent_id or not message_text:
            self._send_json_error(wr, 400, "Missing 'from' or 'message' query parameter.")
            await wr.drain()
            wr.close()
            return

        logger.info(
            "agent_message_received",
            target=target_agent_id,
            source=from_agent_id,
            message=message_text[:100], # Log first 100 chars
        )

        agent_meta = self._agents_meta.get(target_agent_id)

        # _agents_meta only knows local openclaw.json agents.
        # Remote federation agents (e.g. "virtual-boris-vibe") are only in REMOTE_SSH_CONFIG.
        # Fall back: synthesize metadata from REMOTE_SSH_CONFIG if not found locally.
        if not agent_meta:
            for _node_key, _ssh_cfg in REMOTE_SSH_CONFIG.items():
                if target_agent_id in _ssh_cfg["agent_map"]:
                    agent_meta = {"id": target_agent_id, "node": _node_key}
                    break

        if not agent_meta:
            self._send_json_error(wr, 404, f"Agent '{target_agent_id}' not found in federation.")
            await wr.drain()
            wr.close()
            return

        node = agent_meta.get("node", "smain")
        response_data: dict[str, Any] = {"status": "error", "message": "Failed to communicate with agent."}
        status_code = 500

        if node == "smain": # Local agent on smain
            try:
                cmd = ["openclaw", "agent", "--agent", target_agent_id, "--message", message_text, "--json"]
                logger.debug("local_agent_call", cmd=" ".join(cmd))
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout.total)
                
                if proc.returncode == 0:
                    try:
                        response_data = json.loads(stdout.decode("utf-8", errors="ignore"))
                        status_code = 200
                    except json.JSONDecodeError:
                        response_data = {"status": "error", "message": "Invalid JSON response from agent.", "stdout": stdout.decode(), "stderr": stderr.decode()}
                        status_code = 500
                else:
                    response_data = {"status": "error", "message": f"Agent command failed (code {proc.returncode}).", "stdout": stdout.decode(), "stderr": stderr.decode()}
                    status_code = 500
            except asyncio.TimeoutError:
                response_data = {"status": "timeout", "message": f"Agent '{target_agent_id}' did not respond in time."}
                status_code = 504
            except Exception as e:
                response_data = {"status": "error", "message": f"Local agent communication error: {e}"}
                status_code = 500
        else: # Remote agent
            # Use SSH for remote agents, if configured
            ssh_cfg = REMOTE_SSH_CONFIG.get(node)
            if not ssh_cfg:
                self._send_json_error(wr, 501, f"Remote agent on node '{node}' not configured for SSH communication.")
                await wr.drain()
                wr.close()
                return

            remote_agent_id = ssh_cfg["agent_map"].get(target_agent_id)
            if not remote_agent_id:
                self._send_json_error(wr, 404, f"Federation agent '{target_agent_id}' not mapped to remote agent on node '{node}'.")
                await wr.drain()
                wr.close()
                return
            
            try:
                escaped_msg = message_text.replace('"', '\\"')
                cmd_prefix = ssh_cfg.get("cmd_prefix", "")
                ssh_cmd = [
                    "ssh",
                    "-o", "ConnectTimeout=15",
                    "-i", os.path.expanduser(ssh_cfg["key_path"]),
                    f"{ssh_cfg['user']}@{ssh_cfg['host_ip']}",
                    f'{cmd_prefix}openclaw agent --agent {remote_agent_id} --message "{escaped_msg}" --json'
                ]
                logger.debug("remote_agent_call", cmd=" ".join(ssh_cmd))
                
                proc = await asyncio.create_subprocess_exec(
                    *ssh_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout.total)

                if proc.returncode == 0:
                    try:
                        response_data = json.loads(stdout.decode("utf-8", errors="ignore"))
                        status_code = 200
                    except json.JSONDecodeError:
                        response_data = {"status": "error", "message": "Invalid JSON response from remote agent.", "stdout": stdout.decode(), "stderr": stderr.decode()}
                        status_code = 500
                else:
                    response_data = {"status": "error", "message": f"Remote agent command failed (code {proc.returncode}).", "stdout": stdout.decode(), "stderr": stderr.decode()}
                    status_code = 500
            except asyncio.TimeoutError:
                response_data = {"status": "timeout", "message": f"Remote agent '{target_agent_id}' on node '{node}' did not respond in time."}
                status_code = 504
            except FileNotFoundError: # SSH command not found
                response_data = {"status": "error", "message": "SSH command not found. Is SSH client installed and in PATH?"}
                status_code = 500
            except Exception as e:
                response_data = {"status": "error", "message": f"Remote agent communication error: {e}"}
                status_code = 500

        self._send_json_response(wr, status_code, response_data)
        await wr.drain()
        wr.close()

    async def _raw_api_tg_send(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """POST /api/tg/send — rate-limited Telegram sendMessage.

        Body: {"account": "default", "chat_id": "...", "text": "...", "parse_mode": "Markdown"}
        Rate limit: 1 message per 15 seconds per account.
        """
        headers: dict[str, str] = {}
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
            decoded = hdr.decode("utf-8", errors="replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                headers[k.lower()] = v

        try:
            content_length = int(headers.get("content-length", "0") or "0")
        except ValueError:
            content_length = 0
        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                rd.read(min(content_length, 65536)), timeout=10
            )

        try:
            req = json.loads(body_bytes)
        except json.JSONDecodeError:
            self._send_json_response(wr, 400, {"ok": False, "error": "invalid JSON"})
            await wr.drain()
            wr.close()
            return

        account = req.get("account", "default")
        chat_id = str(req.get("chat_id", ""))
        text = req.get("text", "")
        parse_mode = req.get("parse_mode", "")

        if not chat_id or not text:
            self._send_json_response(wr, 400, {"ok": False, "error": "chat_id and text required"})
            await wr.drain()
            wr.close()
            return

        # Rate limit check
        RATE_LIMIT_S = 15.0
        now = time.time()
        last = self._tg_rate.get(account, 0.0)
        since = now - last
        if since < RATE_LIMIT_S:
            retry_after = round(RATE_LIMIT_S - since, 1)
            self._send_json_response(wr, 429, {"ok": False, "retry_after": retry_after})
            await wr.drain()
            wr.close()
            logger.warning("tg_rate_limited", account=account, retry_after=retry_after)
            return

        # Message dedup: drop duplicate sends from multiple nodes within 60s.
        MSG_DEDUP_TTL = 60.0
        _text_hash = hashlib.sha256(f"{chat_id}:{text}".encode()).hexdigest()[:16]
        _dedup_key = f"{account}:{_text_hash}"
        _last_sent = self._tg_msg_dedup.get(_dedup_key, 0.0)
        # Evict stale entries periodically to prevent unbounded growth
        if len(self._tg_msg_dedup) > 500:
            cutoff = now - MSG_DEDUP_TTL
            self._tg_msg_dedup = {k: v for k, v in self._tg_msg_dedup.items() if v > cutoff}
        if now - _last_sent < MSG_DEDUP_TTL:
            logger.info("tg_msg_dedup_dropped", account=account, chat_id=chat_id)
            self._send_json_response(wr, 200, {"ok": True, "message_id": -1, "dedup": True})
            await wr.drain()
            wr.close()
            return
        self._tg_msg_dedup[_dedup_key] = now

        # Load token from openclaw.json
        try:
            with open(self._tg_oc_path) as f:
                oc = json.load(f)
            token = (
                oc.get("channels", {})
                .get("telegram", {})
                .get("accounts", {})
                .get(account, {})
                .get("botToken", "")
            )
        except Exception:
            self._send_json_response(wr, 503, {"ok": False, "error": "config unavailable"})
            await wr.drain()
            wr.close()
            return

        if not token:
            self._send_json_response(wr, 400, {"ok": False, "error": f"unknown account: {account}"})
            await wr.drain()
            wr.close()
            return

        # Send to Telegram
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        # Set optimistically before await to prevent concurrent bypass
        self._tg_rate[account] = time.time()

        tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with self._upstream_session.post(
                tg_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                try:
                    tg_body = await resp.json(content_type=None)
                except Exception:
                    self._tg_rate[account] = 0.0
                    self._tg_msg_dedup.pop(_dedup_key, None)  # allow retry
                    self._send_json_response(wr, 503, {"ok": False, "error": "telegram returned non-JSON"})
                    await wr.drain()
                    wr.close()
                    return
                if tg_body.get("ok"):
                    self._send_json_response(wr, 200, {
                        "ok": True,
                        "message_id": tg_body.get("result", {}).get("message_id"),
                    })
                else:
                    # Reset rate and dedup on failure so caller can retry
                    self._tg_rate[account] = 0.0
                    self._tg_msg_dedup.pop(_dedup_key, None)
                    self._send_json_response(wr, 503, {
                        "ok": False,
                        "error": f"telegram error: {tg_body.get('description', 'unknown')}",
                    })
        except asyncio.TimeoutError:
            self._tg_rate[account] = 0.0
            self._tg_msg_dedup.pop(_dedup_key, None)
            self._send_json_response(wr, 504, {"ok": False, "error": "telegram timeout"})
        except Exception:
            self._tg_rate[account] = 0.0
            self._tg_msg_dedup.pop(_dedup_key, None)
            self._send_json_response(wr, 503, {"ok": False, "error": "upstream error"})

        await wr.drain()
        wr.close()

    def _send_json_response(self, wr: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        wr.write(
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        
    def _send_json_error(self, wr: asyncio.StreamWriter, status: int, message: str) -> None:
        self._send_json_response(wr, status, {"status": "error", "message": message})


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

    # --- Signal & Dashboard API (raw TCP helpers) ---

    async def _raw_api_signal_post(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        source_ip: str,
    ) -> None:
        """POST /api/signal — accept a manual signal from an agent SDK."""
        headers: dict[str, str] = {}
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
            decoded = hdr.decode("utf-8", errors="replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                headers[k.lower()] = v

        content_length = int(headers.get("content-length", "0") or "0")
        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                rd.read(min(content_length, 65536)), timeout=10
            )

        status = 200
        resp_body = b'{"ok":true}'
        if self._signals is None:
            status = 503
            resp_body = b'{"error":"signal queue not available"}'
        else:
            try:
                sig = json.loads(body_bytes)
                if not sig.get("from_node"):
                    from db import source_host_from_ip
                    sig["from_node"] = source_host_from_ip(source_ip)
                asyncio.create_task(self._signals.async_enqueue(sig))
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

    async def _raw_api_signals(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
    ) -> None:
        """GET /api/signals?since=&limit=&agent=&node= — query signals."""
        # Drain remaining headers
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        qs = parse_qs(urlparse(request_path).query)

        def _qs(key: str) -> str | None:
            vals = qs.get(key)
            return vals[0] if vals else None

        since_ts = float(_qs("since") or "0")
        limit = int(_qs("limit") or "100")
        from_node = _qs("node")
        from_agent = _qs("agent")

        if self._signals is None:
            body = b'{"error":"signal queue not available"}'
            status = 503
        else:
            signals = await self._signals.recent(
                since_ts=since_ts,
                limit=limit,
                from_node=from_node,
                from_agent=from_agent,
            )
            body = json.dumps({"signals": signals, "count": len(signals)}).encode()
            status = 200

        wr.write(
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_nodes(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
    ) -> None:
        """GET /api/nodes — full federation topology for dashboard."""
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        fed_cfg    = self._config.get("federation", {})
        local_node = fed_cfg.get("local_node", "smain")
        node_agents_cfg = fed_cfg.get("node_agents", {})

        # Local node — full data
        agents       = list(self._agents_meta.values())
        signals_list: list[Any] = []
        stats: dict[str, Any]   = {}
        if self._signals is not None:
            signals_list = await self._signals.recent(limit=50)
            stats        = await self._signals.today_stats()
        health = json.loads(self._build_health_json())

        local_nd: dict[str, Any] = {
            "id":      local_node,
            "label":   local_node,
            "status":  "ok",
            "health":  health,
            "agents":  agents,
            "signals": signals_list,
            "stats":   stats,
        }

        # All other WG nodes in canonical order
        from db import WG_HOST_MAP
        WG_ORDER = ["pi2", "pi", "vibe", "smain", "cloud", "sdev", "hoster"]
        known = {name for name in WG_HOST_MAP.values()}

        other_nodes: list[dict[str, Any]] = []
        for name in WG_ORDER:
            if name == local_node or name not in known:
                continue
            nd_agents = [
                {
                    "id":          ag["id"],
                    "name":        ag.get("name", ag["id"]),
                    "emoji":       ag.get("emoji", "🤖"),
                    "model":       ag.get("model", ""),
                    "node":        name,
                    "description": ag.get("description", ag.get("name", ag["id"])),
                }
                for ag in node_agents_cfg.get(name, [])
            ]
            other_nodes.append({
                "id":     name,
                "label":  name,
                "status": "unknown",
                "agents": nd_agents,
            })

        services = self._build_services_health()
        data = {
            "ts":       time.time(),
            "nodes":    [local_nd] + other_nodes,
            "services": services,
        }

        body = json.dumps(data).encode()
        wr.write(
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_report(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
    ) -> None:
        """GET /api/report?since=ISO&until=ISO — token savings vs Claude Sonnet baseline."""
        from db import WG_HOST_MAP # Import moved here
        from urllib.parse import urlparse, parse_qs
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        qs = parse_qs(urlparse(request_path).query)
        since_iso = (qs.get("since") or [None])[0]
        until_iso = (qs.get("until") or [None])[0]

        # Price per 1M tokens (input, output) in USD — updated 2026-05-17
        PRICES: dict[str, tuple[float, float]] = {
            "deepseek-v4-flash":      (0.14,  0.28),
            "deepseek-v4-pro":        (0.435, 0.87),
            "deepseek-chat":          (0.14,  0.28),
            "deepseek-reasoner":      (0.435, 0.87),
            "gemini-2.5-flash":       (0.15,  0.60),
            "gemini-2.5-pro":         (1.25, 10.00),
            "gemini-3.1-pro-preview": (2.00, 12.00),
            "gemini-2.0-flash":       (0.10,  0.40),
            "gpt-4o":                 (2.50, 10.00),
            "gpt-4o-mini":            (0.15,  0.60),
            "claude-haiku-4-5":       (1.00,  5.00),
            "claude-haiku-4-5-20251001": (1.00, 5.00),
            "claude-sonnet-4-6":      (3.00, 15.00),
            "claude-opus-4-7":        (5.00, 25.00),
            "llama3.1:8b":            (0.00,  0.00),
        }
        CLAUDE_IN  = 3.00   # claude-sonnet-4-6 input per 1M
        CLAUDE_OUT = 15.00  # claude-sonnet-4-6 output per 1M

        rows: list[dict[str, Any]] = []
        total_actual   = 0.0
        total_baseline = 0.0

        try:
            where_parts = ["llm_model IS NOT NULL", "llm_model != ''"]
            params: list[Any] = []
            if since_iso:
                where_parts.append("timestamp >= ?")
                params.append(since_iso)
            if until_iso:
                where_parts.append("timestamp <= ?")
                params.append(until_iso)
            where_clause = " AND ".join(where_parts)

            async with self._db._lock:
                db_rows = self._db._conn.execute(
                    f"""SELECT llm_model, COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0)
                       FROM request_log
                       WHERE {where_clause}
                       GROUP BY llm_model
                       ORDER BY SUM(COALESCE(tokens_in,0)) DESC""",
                    params,
                ).fetchall()
        except Exception as exc:
            logger.error("report_db_error", error=str(exc))
            db_rows = []

        for model, calls, tin, tout in db_rows:
            tin  = tin  or 0
            tout = tout or 0
            p = PRICES.get(model, (CLAUDE_IN, CLAUDE_OUT))
            actual   = (tin * p[0] + tout * p[1]) / 1_000_000
            baseline = (tin * CLAUDE_IN + tout * CLAUDE_OUT) / 1_000_000
            saved    = baseline - actual
            pct      = (saved / baseline * 100) if baseline > 0 else 0.0
            rows.append({
                "model":           model,
                "calls":           calls,
                "tokens_in":       tin,
                "tokens_out":      tout,
                "actual_cost_usd": round(actual,   2),
                "claude_cost_usd": round(baseline, 2),
                "saved_usd":       round(saved,    2),
                "saved_pct":       round(pct,      1),
            })
            total_actual   += actual
            total_baseline += baseline

        total_saved = total_baseline - total_actual
        data = {
            "period":            "all-time",
            "rows":              rows,
            "total_actual_usd":  round(total_actual,   2),
            "total_claude_usd":  round(total_baseline, 2),
            "total_saved_usd":   round(total_saved,    2),
            "total_saved_pct":   round((total_saved / total_baseline * 100) if total_baseline > 0 else 0, 1),
        }

        body = json.dumps(data).encode()
        wr.write(
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_routing(self, wr: asyncio.StreamWriter) -> None:
        """GET /api/routing — routing config + algorithm description."""
        routing = self._config.get("routing", {})
        # Check whether agents are going through Lineman or direct
        oc_json_path = os.path.expanduser("~/.openclaw/openclaw.json")
        lineman_mode = True
        try:
            import json as _json
            with open(oc_json_path) as _f:
                _oc = _json.load(_f)
            google_url = _oc.get("models", {}).get("providers", {}).get("google", {}).get("baseUrl", "")
            lineman_mode = "127.0.0.1" in google_url
        except Exception:
            pass
        payload = {
            "routing": routing,
            "lineman_mode": lineman_mode,
            "algorithm": [
                {"rule": "Длинный контекст", "condition": f"> {routing.get('longContextThreshold', 60000)//1000}k токенов", "provider": routing.get("longContext", {}).get("provider", "gemini"), "model": routing.get("longContext", {}).get("model", "—")},
                {"rule": "Размышление (think)", "condition": "тег [think]", "provider": routing.get("think", {}).get("provider", "deepseek"), "model": routing.get("think", {}).get("model", "—")},
                {"rule": "Веб-поиск", "condition": "webSearch запрос", "provider": routing.get("webSearch", {}).get("provider", "gemini"), "model": routing.get("webSearch", {}).get("model", "—")},
                {"rule": "Фоновые задачи", "condition": "background cron", "provider": routing.get("background", {}).get("provider", "deepseek"), "model": routing.get("background", {}).get("model", "—")},
                {"rule": "По умолчанию", "condition": "всё остальное", "provider": routing.get("default", {}).get("provider", "deepseek"), "model": routing.get("default", {}).get("model", "—")},
            ],
        }
        body = _json.dumps(payload).encode()
        resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            + body
        )
        wr.write(resp)
        await wr.drain()

    async def _raw_api_routing_decisions(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """GET /api/routing/decisions — last 50 routing decisions from Router."""
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        decisions: list[Any] = []
        if hasattr(self, "_router") and self._router is not None:
            decisions = self._router.recent_decisions()

        body = json.dumps({"decisions": decisions, "count": len(decisions)}).encode()
        wr.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            + body
        )
        await wr.drain()
        wr.close()

    async def _raw_dashboard(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """GET /dashboard — serve dashboard/index.html."""
        # Drain headers
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        html_path = Path("/home/shectory/workspaces/projects/shectory-dashboard") / "index.html"
        if not html_path.exists():
            body = b"<h1>Dashboard not found. Create dashboard/index.html</h1>"
            ct = "text/html"
        else:
            body = html_path.read_bytes()
            ct = "text/html; charset=utf-8"

        wr.write(
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {ct}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    def _build_services_health(self) -> list[dict[str, Any]]:
        """Map service state to dashboard-friendly list."""
        svc_map = {
            "deepseek": {"label": "DeepSeek", "emoji": "☁️", "ids": ["deepseek-flash", "deepseek-pro"]},
            "gemini":   {"label": "Gemini",   "emoji": "☁️", "ids": ["gemini-flash", "gemini-pro"]},
            "telegram": {"label": "Telegram", "emoji": "📱", "ids": ["telegram"]},
            "google":   {"label": "Google",   "emoji": "📧", "ids": ["google-drive", "google-gmail", "google-calendar"]},
            "openai":   {"label": "OpenAI",   "emoji": "☁️", "ids": []},
        }
        services_state = self.state.get("services", {})
        result = []
        for svc_key, info in svc_map.items():
            status = "unknown"
            for sid in info["ids"]:
                s = services_state.get(sid, {})
                st = s.get("state", "unknown")
                if st == "online":
                    status = "online"
                    break
                elif st in ("down", "degraded"):
                    status = st
            result.append({
                "id": svc_key,
                "label": f"{info['label']} {info['emoji']}",
                "status": status,
            })
        return result
