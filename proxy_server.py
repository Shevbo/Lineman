"""HTTP forward proxy with smart routing and management API.

Listens on localhost:9090, forwards HTTP/HTTPS (CONNECT) requests
to the correct upstream, injects API keys, and logs everything.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
from tg_miniapp import validate_init_data, user_id_allowed
from backlog import BacklogStore, enqueue_builder_ticket

logger = structlog.get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def build_builder_status(tickets: list, audit: list) -> dict:
    """Шейп данных klod-builder для дашборда: сводка по статусам + тикеты (новые сверху)."""
    summary: dict[str, int] = {}
    shaped = []
    for t in tickets:
        st = t.get("status", "?")
        summary[st] = summary.get(st, 0) + 1
        ev = t.get("evidence", {}) or {}
        repo = (t.get("repo_path", "") or "").rstrip("/")
        shaped.append({
            "id": t.get("id", ""),
            "repo": repo.rsplit("/", 1)[-1] if repo else "",
            "task": (t.get("task", "") or "")[:120],
            "kind": t.get("kind", "normal"),
            "status": st,
            "branch": t.get("branch", ""),
            "pr_url": t.get("pr_url", ""),
            "tests": str(ev.get("tests", ""))[:120],
            "claude": str(ev.get("claude", ""))[:200],
            "created_at": t.get("created_at", ""),
        })
    shaped.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {
        "total": len(tickets),
        "summary": summary,
        "tickets": shaped,
        "audit": audit[-40:],
    }

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
    "vibe": { # vibe = boris's Windows PC. WG (10.66.0.6) is chronically down →
              # reach it via the Pi relay (LAN 192.168.1.64, ProxyJump shevbo-pi).
        "user": "boris",
        "host_ip": "192.168.1.64",          # vibe LAN IP, reached through the Pi jump host
        "proxy_jump": "shevbo-pi",          # WG to 10.66.0.6 is dead; Pi relay is the reliable path
        "key_path": "~/.ssh/id_ed25519", # Assumes this key is authorized on vibe
        # Windows: prevent Node.js from routing localhost requests through iProyal system proxy
        "cmd_prefix": 'set "NO_PROXY=localhost,127.0.0.1,::1" && ',
        "agent_map": {
            "virtual-boris-vibe": "vboris2", # VBoris2 on vibe is agent 'vboris2'
        },
        # Reserve path — re-enable as failover once WireGuard on vibe is restored:
        "fallback_host_ip": "10.66.0.6",
    },
    # cloud (shevbo@10.66.0.3) СПИСАН 2026-06-04 — узла нет, агента tank-3 нет.
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

        # Shectory Portal — единый каталог пользователей.
        # bridge: POST $SHECTORY_PORTAL_URL/api/internal/verify-portal-credentials
        # Bearer $SHECTORY_AUTH_BRIDGE_SECRET, body {email, password}.
        # Кэш положительных проверок: sha256(email:password) → expires_at.
        self._portal_auth_cache: dict[str, float] = {}
        self._portal_auth_ttl: float = 300.0

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

            # klod-access two-way inbox (specialised: file-backed, no openclaw cli)
            elif request_path_only.startswith("/api/agent/klod-access/"):
                await self._raw_api_klod_access(rd, wr, request_path, method)
                return

            # Censor's daily top-offenders report
            elif request_path_only == "/api/censor/top-offenders":
                await self._raw_api_censor_top_offenders(rd, wr)
                return

            # Lazy Queue (отложенные задачи для local LLM)
            elif request_path_only.startswith("/api/queue/lazy"):
                await self._raw_api_lazy_queue(rd, wr, request_path, method)
                return

            elif request_path_only == "/api/budget":
                await self._raw_api_budget(rd, wr)
                return

            # Дневные капы токенов на провайдера (контроль траты)
            elif request_path_only == "/api/token-caps":
                await self._read_headers(rd)
                import reverse_proxy as _rp
                try:
                    day = _rp._today_msk()
                    st = _rp._get_token_cap(self._config).status(day)
                except Exception as e:
                    day, st = "", {"error": str(e)[:120]}
                self._send_simple_and_close(wr, 200, {"day": day, "caps": st})
                await wr.drain()
                wr.close()
                return

            elif request_path_only == "/api/keymaster/leak_alert" and method == "POST":
                await self._raw_api_leak_alert(rd, wr)
                return

            # nginx auth_request на dashboard.shectory.ru — единый каталог Shectory Portal.
            # Возвращает 200 если Basic-кред валиден через portal bridge, 401 иначе.
            elif request_path_only == "/api/portal-auth-check":
                headers = await self._read_headers(rd)
                creds = self._parse_basic_auth(headers)
                if creds and await self._verify_portal_credentials(*creds):
                    body = b'{"ok":true}'
                    wr.write(
                        f"HTTP/1.1 200 OK\r\n"
                        f"X-Portal-User: {creds[0]}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n".encode()
                    )
                    wr.write(body)
                    await wr.drain()
                    wr.close()
                else:
                    await self._send_401_basic(wr)
                return

            # Брендированный логин Shectory (cookie-сессия вместо Basic popup).
            elif request_path_only in ("/login", "/login/") and method == "GET":
                await self._raw_dashboard(rd, wr, "login.html")
                return
            elif request_path_only == "/api/login" and method == "POST":
                await self._raw_api_login(rd, wr)
                return
            # Telegram Mini App Клода: страница (без auth — бутстрап) + initData-вход.
            elif request_path_only in ("/miniapp", "/miniapp/"):
                await self._raw_dashboard(rd, wr, "miniapp.html")
                return
            elif request_path_only == "/api/tg/miniapp-auth" and method == "POST":
                await self._raw_api_miniapp_auth(rd, wr)
                return
            # nginx auth_request: 200 если валидна cookie-сессия, 401 иначе (без popup).
            elif request_path_only == "/api/session-check":
                headers = await self._read_headers(rd)
                email = self._session_email_from_cookie(headers)
                if email:
                    body = b'{"ok":true}'
                    wr.write(
                        f"HTTP/1.1 200 OK\r\nX-Portal-User: {email}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    )
                    wr.write(body)
                else:
                    body = b'{"error":"no session"}'
                    wr.write(
                        f"HTTP/1.1 401 Unauthorized\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    )
                    wr.write(body)
                await wr.drain()
                wr.close()
                return
            elif request_path_only in ("/api/logout", "/logout") and method == "POST":
                await self._read_headers(rd)
                body = b'{"ok":true}'
                wr.write(
                    f"HTTP/1.1 200 OK\r\n"
                    f"Set-Cookie: shectory_session=; Path=/; HttpOnly; SameSite=Lax; "
                    f"Max-Age=0\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                )
                wr.write(body)
                await wr.drain()
                wr.close()
                return

            # Agent-to-agent messaging API
            # /api/agent/{target_agent_id}/message?from=<from_agent_id>&message=<msg>
            elif request_path_only.startswith("/api/agent/"):
                await self._raw_api_agent_message(rd, wr, request_path, method)
                return

            # Dashboard static serve
            elif request_path_only in ("/dashboard", "/dashboard/"):
                await self._raw_dashboard(rd, wr)
            elif request_path_only in ("/klod-chat", "/klod-chat/",
                                       "/api/klod-chat", "/api/klod-chat/"):
                # Единая учётка Shectory Portal. Основной путь — cookie-сессия (через
                # nginx /login); Basic оставлен как fallback для прямого доступа к :9090.
                headers = await self._read_headers(rd)
                if not self._session_email_from_cookie(headers):
                    creds = self._parse_basic_auth(headers)
                    if not creds or not await self._verify_portal_credentials(*creds):
                        await self._send_401_basic(wr)
                        return
                await self._raw_dashboard(
                    rd, wr, "klod-chat.html", drain_headers=False
                )
            # Та же миниаппа (чаты+тикеты+бэклог) через дашборд по cookie-сессии (вне Telegram).
            elif request_path_only in ("/klod", "/klod/"):
                headers = await self._read_headers(rd)
                if not self._session_email_from_cookie(headers):
                    creds = self._parse_basic_auth(headers)
                    if not creds or not await self._verify_portal_credentials(*creds):
                        await self._send_401_basic(wr)
                        return
                await self._raw_dashboard(rd, wr, "miniapp.html", drain_headers=False)
            # Мониторинг тикетов Билдера (klod-builder) на дашборде.
            elif request_path_only == "/api/builder/tickets":
                await self._raw_api_builder_tickets(rd, wr)
                return
            elif request_path_only in ("/builder", "/builder/",
                                       "/api/builder", "/api/builder/"):
                headers = await self._read_headers(rd)
                if not self._session_email_from_cookie(headers):
                    creds = self._parse_basic_auth(headers)
                    if not creds or not await self._verify_portal_credentials(*creds):
                        await self._send_401_basic(wr)
                        return
                await self._raw_dashboard(
                    rd, wr, "builder.html", drain_headers=False
                )
            elif request_path_only in ("/api/search", "/api/youtube") and method == "GET":
                # Федеративный web_search / youtube-поиск (keyless, egress Lineman).
                await self._raw_api_search(rd, wr, request_path)
            elif request_path_only == "/api/build" and method == "POST":
                # Постановка тикета Билдеру (klod-builder очередь).
                await self._raw_api_build(rd, wr, request_path)
                return
            elif request_path_only.startswith("/api/backlog"):
                # Трекер бэклога Клода (#7): список/добавить/промоут в Билдер/статус.
                await self._raw_api_backlog(rd, wr, request_path, method)
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
                from secret_mask import mask_row
                mask_row(row)
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
                ssh_cmd = ["ssh", "-o", "ConnectTimeout=15"]
                if ssh_cfg.get("proxy_jump"):
                    ssh_cmd += ["-J", ssh_cfg["proxy_jump"]]
                ssh_cmd += [
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

    async def _raw_api_klod_access(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
        method: str,
    ) -> None:
        """File-backed inbox+outbox for the klod-access agent.

        Routes:
          POST /api/agent/klod-access/message?from=&node= body=text  → inbox
          GET  /api/agent/klod-access/inbox?since=&limit=            → JSONL
          POST /api/agent/klod-access/reply?to=&in_reply_to=  body=text → outbox+forward
          GET  /api/agent/klod-access/outbox?since=&limit=           → JSONL
        """
        from urllib.parse import urlparse, parse_qs
        import klod_inbox

        # Read headers + optional body (content-length only)
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

        url = urlparse(request_path)
        suffix = url.path[len("/api/agent/klod-access/"):].rstrip("/")
        qs = parse_qs(url.query)

        def q(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return vals[0] if vals else default

        try:
            if method == "POST" and suffix == "message":
                from_agent = q("from")
                if not from_agent:
                    return self._send_simple_and_close(wr, 400, {"error": "missing 'from'"})
                # Body may be text/plain or JSON {"message": "..."}
                msg = body_bytes.decode("utf-8", errors="replace").strip()
                if msg.startswith("{"):
                    try:
                        msg = json.loads(body_bytes).get("message", msg)
                    except Exception:
                        pass
                if not msg:
                    msg = q("message", "")
                if not msg:
                    return self._send_simple_and_close(wr, 400, {"error": "empty message"})
                rec = klod_inbox.write_inbox(from_agent, msg, node=q("node"))
                logger.info("klod_inbox_msg", id=rec["id"], from_=from_agent)
                return self._send_simple_and_close(wr, 200, {"status": "ok", "id": rec["id"]})

            if method == "GET" and suffix == "inbox":
                since = int(q("since", "0") or "0")
                limit = min(int(q("limit", "50") or "50"), 500)
                msgs = klod_inbox.read_inbox(since, limit)
                return self._send_simple_and_close(wr, 200, {"messages": msgs})

            if method == "POST" and suffix == "reply":
                to_agent = q("to")
                if not to_agent:
                    return self._send_simple_and_close(wr, 400, {"error": "missing 'to'"})
                in_reply_to = int(q("in_reply_to", "0") or "0") or None
                msg = body_bytes.decode("utf-8", errors="replace").strip()
                if msg.startswith("{"):
                    try:
                        msg = json.loads(body_bytes).get("message", msg)
                    except Exception:
                        pass
                if not msg:
                    return self._send_simple_and_close(wr, 400, {"error": "empty message"})
                # Записываем в outbox СРАЗУ (всегда доступно для pull), даже если push
                # не доходит (агент не openclaw-dispatchable: 404/timeout). Push —
                # best-effort в фоне, не блокирует ответ.
                rec = klod_inbox.write_outbox(to_agent, msg, in_reply_to, delivered=None)
                try:
                    asyncio.create_task(klod_inbox.deliver_reply(to_agent, msg))
                except Exception:
                    pass
                return self._send_simple_and_close(wr, 200, {
                    "status": "ok", "id": rec["id"],
                    "pull": f"/api/agent/klod-access/outbox?to={to_agent}&since=<cursor>",
                })

            if method == "GET" and suffix == "outbox":
                since = int(q("since", "0") or "0")
                limit = min(int(q("limit", "50") or "50"), 500)
                to = q("to") or None
                msgs = klod_inbox.read_outbox(since, limit, to=to)
                return self._send_simple_and_close(wr, 200, {"messages": msgs})

            self._send_simple_and_close(wr, 404, {"error": "unknown klod-access route"})
        except Exception as e:
            logger.exception("klod_access_handler_error")
            self._send_simple_and_close(wr, 500, {"error": str(e)})

    async def _raw_api_censor_top_offenders(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """GET /api/censor/top-offenders — latest Censor top-offenders report."""
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
        try:
            import glob
            import os as _os
            pattern = _os.path.expanduser("~/workspaces/infra/censor/reports/top-offenders-*.json")
            files = sorted(glob.glob(pattern))
            if not files:
                self._send_simple_and_close(wr, 404, {"error": "no top-offenders report yet"})
                return
            payload = json.loads(open(files[-1]).read())
            self._send_simple_and_close(wr, 200, payload)
        except Exception as e:
            self._send_simple_and_close(wr, 500, {"error": str(e)})

    async def _raw_api_lazy_queue(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
        method: str,
    ) -> None:
        """Lazy Queue API:
        POST /api/queue/lazy            body=JSON         → {job_id}
        GET  /api/queue/lazy/<id>                         → {status, output, ...}
        GET  /api/queue/lazy?from_agent=X&status=Y        → [{...}, ...]
        DELETE /api/queue/lazy/<id>                       → {deleted: bool}
        """
        from urllib.parse import urlparse, parse_qs
        import lazy_queue as lq

        headers: dict[str, str] = {}
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
            decoded = hdr.decode("utf-8", errors="replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                headers[k.lower()] = v

        body = b""
        content_length = int(headers.get("content-length", "0") or "0")
        if content_length > 0:
            body = await asyncio.wait_for(
                rd.read(min(content_length, 256 * 1024)), timeout=10
            )

        parsed = urlparse(request_path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        suffix = path[len("/api/queue/lazy"):]  # "", "/<id>", или ""

        try:
            if method == "POST" and suffix in ("", "/"):
                data = json.loads(body) if body else {}
                job_id = lq.submit_job(
                    from_agent=str(data.get("from_agent") or qs.get("from_agent", [""])[0] or "unknown"),
                    from_node=str(data.get("from_node") or qs.get("from_node", [""])[0] or "smain"),
                    kind=str(data.get("kind") or "tune"),
                    user_prompt=str(data.get("prompt") or data.get("user_prompt") or ""),
                    system_prompt=str(data.get("system") or data.get("system_prompt") or ""),
                    max_tokens=int(data.get("max_tokens") or 600),
                    temperature=float(data.get("temperature") or 0.3),
                    priority=int(data.get("priority") or 3),
                    deadline_hint_minutes=int(data.get("deadline_hint_minutes") or 60),
                )
                self._send_simple_and_close(wr, 200, {"job_id": job_id, "status": "queued"})
                return

            if method == "GET" and suffix.startswith("/"):
                try:
                    job_id = int(suffix.lstrip("/"))
                except ValueError:
                    self._send_simple_and_close(wr, 400, {"error": "bad job_id"})
                    return
                job = lq.get_job(job_id)
                if not job:
                    self._send_simple_and_close(wr, 404, {"error": "not found"})
                    return
                self._send_simple_and_close(wr, 200, job)
                return

            if method == "GET" and suffix in ("", "/"):
                jobs = lq.list_jobs(
                    from_agent=qs.get("from_agent", [None])[0],
                    status=qs.get("status", [None])[0],
                    limit=int(qs.get("limit", ["50"])[0]),
                )
                self._send_simple_and_close(wr, 200, {"jobs": jobs, "stats_24h": lq.stats_24h()})
                return

            if method == "DELETE" and suffix.startswith("/"):
                try:
                    job_id = int(suffix.lstrip("/"))
                except ValueError:
                    self._send_simple_and_close(wr, 400, {"error": "bad job_id"})
                    return
                ok = lq.delete_job(job_id)
                self._send_simple_and_close(wr, 200, {"deleted": ok})
                return

            self._send_simple_and_close(wr, 400, {"error": f"unsupported {method} {request_path}"})
        except Exception as e:
            self._send_simple_and_close(wr, 500, {"error": str(e)})

    async def _raw_api_leak_alert(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """POST /api/keymaster/leak_alert — агент сообщает что нашёл утечку.

        Body: {"secret_name": "X|null", "where": "...", "snippet": "...",
               "source_agent": "...", "severity": "high|medium|low"}
        Действие: secret_leak_alert.report_leak → klod-inbox + TG + auto-rotate.
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
        cl = int(headers.get("content-length", "0") or "0")
        body = await asyncio.wait_for(rd.read(min(cl, 16384)), timeout=10) if cl > 0 else b""
        try:
            d = json.loads(body) if body else {}
            from secret_leak_alert import report_leak
            rec = report_leak(
                secret_name=d.get("secret_name"),
                where=str(d.get("where") or "?"),
                snippet=str(d.get("snippet") or ""),
                source_agent=str(d.get("source_agent") or "?"),
                severity=str(d.get("severity") or "high"),
            )
            self._send_simple_and_close(wr, 200, rec)
        except Exception as e:
            self._send_simple_and_close(wr, 500, {"error": str(e)})

    async def _raw_api_budget(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """GET /api/budget — текущий расход за месяц по провайдерам vs лимиты.

        Возвращает: {provider: {used_usd, limit_usd, pct, status, top_models[]}}
        Используется dashboard widget + daily_audit алёрты.
        """
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
        try:
            budget_cfg = (self._config.get("budget") or {})
            pricing = (self._config.get("pricing") or {}).get("models") or {}
            limits = {
                "anthropic":  float(budget_cfg.get("anthropic_monthly_usd") or 0),
                "deepseek":   float(budget_cfg.get("deepseek_monthly_usd") or 0),
                "google":     float(budget_cfg.get("google_monthly_usd") or 0),
                "openai":     float(budget_cfg.get("openai_monthly_usd") or 0),
                "openrouter": float(budget_cfg.get("openrouter_monthly_usd") or 0),
            }
            alert_pct = float(budget_cfg.get("alert_threshold_pct") or 90)

            import sqlite3 as _sql
            con = _sql.connect(str(self._db._path))
            cur = con.cursor()
            # Расход с начала текущего месяца
            rows = cur.execute("""
                SELECT llm_provider, llm_model,
                       SUM(COALESCE(tokens_in,0)) as tin,
                       SUM(COALESCE(tokens_out,0)) as tout,
                       COUNT(*) as calls
                FROM request_log
                WHERE timestamp >= strftime('%Y-%m-01T00:00:00', 'now')
                  AND llm_provider IS NOT NULL AND llm_provider != ''
                GROUP BY llm_provider, llm_model
            """).fetchall()
            con.close()

            by_provider: dict[str, dict] = {}
            for provider, model, tin, tout, calls in rows:
                price = pricing.get(model) or pricing.get((model or "").split("/")[-1]) or {}
                in_usd  = (tin or 0) * float(price.get("in") or 0) / 1_000_000
                out_usd = (tout or 0) * float(price.get("out") or 0) / 1_000_000
                cost = in_usd + out_usd
                p = by_provider.setdefault(provider, {"used_usd": 0.0, "calls": 0, "tokens": 0, "top_models": []})
                p["used_usd"] = round(p["used_usd"] + cost, 4)
                p["calls"] += int(calls or 0)
                p["tokens"] += int((tin or 0) + (tout or 0))
                p["top_models"].append({"model": model or "?", "calls": calls, "cost_usd": round(cost, 4)})
            for prov in list(by_provider.keys()):
                limit = limits.get(prov, 0)
                used = by_provider[prov]["used_usd"]
                pct = round(100.0 * used / limit, 1) if limit > 0 else 0.0
                by_provider[prov].update({
                    "limit_usd": limit,
                    "pct": pct,
                    "status": "red" if pct >= alert_pct else "yellow" if pct >= 70 else "green",
                })
                by_provider[prov]["top_models"] = sorted(
                    by_provider[prov]["top_models"], key=lambda x: -x["cost_usd"]
                )[:3]
            # Также Lazy Queue saved
            lazy_saved = 0.0
            try:
                con = _sql.connect(str(self._db._path))
                saved = con.execute(
                    "SELECT SUM(COALESCE(saved_usd,0)) FROM lazy_jobs "
                    "WHERE ts_done >= strftime('%Y-%m-01T00:00:00', 'now')"
                ).fetchone()[0]
                lazy_saved = round(float(saved or 0), 4)
                con.close()
            except Exception:
                pass
            import datetime as _dt
            self._send_simple_and_close(wr, 200, {
                "month": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m"),
                "providers": by_provider,
                "alert_threshold_pct": alert_pct,
                "lazy_saved_month_usd": lazy_saved,
            })
        except Exception as e:
            self._send_simple_and_close(wr, 500, {"error": str(e)})

    def _send_simple_and_close(self, wr: asyncio.StreamWriter, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                  404: "Not Found", 500: "Internal Server Error",
                  503: "Service Unavailable"}.get(status, "OK")
        wr.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        wr.write(body)

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
        WG_ORDER = ["pi2", "pi", "vibe", "smain", "sdev", "hoster"]
        known = {name for name in WG_HOST_MAP.values()}
        decommissioned = set(fed_cfg.get("decommissioned", []))

        # Derive per-node status from recent heartbeat signals (5-minute window).
        node_last_seen: dict[str, float] = {}
        for s in signals_list:
            node_id = s.get("from_node")
            if node_id and s.get("type") == "heartbeat":
                ts = float(s.get("ts") or 0)
                if ts > node_last_seen.get(node_id, 0):
                    node_last_seen[node_id] = ts
        now = time.time()
        HEARTBEAT_OK_WINDOW = 300.0

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
            if name in decommissioned:
                status = "decommissioned"
            else:
                last = node_last_seen.get(name, 0)
                status = "ok" if (now - last) < HEARTBEAT_OK_WINDOW and last > 0 else "unknown"
            other_nodes.append({
                "id":     name,
                "label":  name,
                "status": status,
                "agents": nd_agents,
                "last_heartbeat_age_s": round(now - node_last_seen.get(name, 0)) if node_last_seen.get(name) else None,
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

    async def _raw_api_build(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
    ) -> None:
        """POST /api/build?repo=<path>&from=<agent>  body=<задача> — тикет Билдеру.
        Аппендит тикет в очередь klod-builder (~/.builder/queue.json). Билдер сам
        переклассифицирует kind (critical для lineman/keymaster/censor) при claim."""
        from urllib.parse import urlparse, parse_qs
        import time as _time

        headers: dict[str, str] = {}
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break
            d = hdr.decode("utf-8", "replace").strip()
            if ": " in d:
                k, v = d.split(": ", 1)
                headers[k.lower()] = v
        clen = int(headers.get("content-length", "0") or "0")
        body = await asyncio.wait_for(rd.read(min(clen, 65536)), timeout=10) if clen > 0 else b""

        url = urlparse(request_path)
        qs = parse_qs(url.query)
        task = body.decode("utf-8", "replace").strip()
        if task.startswith("{"):
            try:
                task = json.loads(body).get("task", task)
            except Exception:
                pass
        repo = (qs.get("repo") or [""])[0]
        frm = (qs.get("from") or ["?"])[0]
        if not task or not repo:
            return self._send_simple_and_close(
                wr, 400, {"error": "need ?repo=<path> and body=<task>"})

        qpath = os.path.expanduser(os.environ.get("BUILDER_QUEUE", "~/.builder/queue.json"))
        os.makedirs(os.path.dirname(qpath), exist_ok=True)
        try:
            items = json.loads(open(qpath).read()) if os.path.exists(qpath) else []
        except Exception:
            items = []
        tid = f"t{int(_time.time())}"
        items.append({"id": tid, "repo_path": repo, "task": task, "kind": "normal",
                      "status": "queued", "branch": "", "pr_url": "",
                      "created_at": "", "evidence": {"from": frm}})
        try:
            with open(qpath, "w") as f:
                f.write(json.dumps(items, ensure_ascii=False, indent=1))
        except Exception as e:
            return self._send_simple_and_close(wr, 500, {"error": str(e)[:200]})
        logger.info("builder_ticket", id=tid, repo=repo, from_=frm)
        self._send_simple_and_close(wr, 200, {"status": "ok", "id": tid, "queued": len(items)})

    async def _raw_api_backlog(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
        method: str,
    ) -> None:
        """Трекер бэклога Клода (#7). За cookie-сессией (nginx /api/).
          GET  /api/backlog                  → {items, summary, total}
          POST /api/backlog        {title,note,repo,priority}  → добавить
          POST /api/backlog/promote {id,repo?}                 → тикет Билдеру + статус sent
          POST /api/backlog/status  {id,status}                → сменить статус
          POST /api/backlog/remove  {id}                       → удалить
        """
        from urllib.parse import urlparse
        headers = await self._read_headers(rd)
        clen = int(headers.get("content-length", "0") or "0")
        raw = await asyncio.wait_for(rd.read(min(clen, 65536)), timeout=10) if clen > 0 else b""
        path = urlparse(request_path).path.rstrip("/") or "/api/backlog"
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        store = BacklogStore(os.environ.get("KLOD_BACKLOG", "~/.klod/backlog.json"))

        if method == "GET" and path == "/api/backlog":
            items = store.list()
            return self._send_simple_and_close(
                wr, 200, {"items": items, "summary": store.summary(), "total": len(items)})

        if method == "POST" and path == "/api/backlog":
            try:
                it = store.add(str(body.get("title", "")), note=str(body.get("note", "")),
                               repo=str(body.get("repo", "")),
                               priority=str(body.get("priority", "normal")))
            except ValueError as e:
                return self._send_simple_and_close(wr, 400, {"error": str(e)})
            return self._send_simple_and_close(wr, 200, {"ok": True, "item": it})

        if method == "POST" and path == "/api/backlog/promote":
            it = store.get(str(body.get("id", "")))
            if not it:
                return self._send_simple_and_close(wr, 404, {"error": "not found"})
            repo = str(body.get("repo", "") or it.get("repo", "")).strip()
            if not repo:
                return self._send_simple_and_close(wr, 400, {"error": "нужен repo"})
            task = it["title"] + (("\n\n" + it["note"]) if it.get("note") else "")
            try:
                tid = enqueue_builder_ticket(
                    os.environ.get("BUILDER_QUEUE", "~/.builder/queue.json"),
                    repo, task, frm="klod-backlog")
            except Exception as e:
                return self._send_simple_and_close(wr, 500, {"error": str(e)[:200]})
            store.set_status(it["id"], "sent", ticket_id=tid)
            logger.info("backlog_promote", id=it["id"], ticket=tid, repo=repo)
            return self._send_simple_and_close(wr, 200, {"ok": True, "ticket_id": tid})

        if method == "POST" and path == "/api/backlog/status":
            try:
                upd = store.set_status(str(body.get("id", "")), str(body.get("status", "")))
            except ValueError as e:
                return self._send_simple_and_close(wr, 400, {"error": str(e)})
            if not upd:
                return self._send_simple_and_close(wr, 404, {"error": "not found"})
            return self._send_simple_and_close(wr, 200, {"ok": True, "item": upd})

        if method == "POST" and path == "/api/backlog/remove":
            ok = store.remove(str(body.get("id", "")))
            return self._send_simple_and_close(wr, 200 if ok else 404, {"ok": ok})

        return self._send_simple_and_close(wr, 400, {"error": "bad backlog request"})

    async def _raw_api_search(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        request_path: str,
    ) -> None:
        """GET /api/search?q=&limit= — федеративный web_search (keyless DuckDuckGo
        через egress Lineman). App-friendly JSON {query,count,results:[{title,url,snippet}]}."""
        from urllib.parse import urlparse, parse_qs
        import lineman_search

        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        url = urlparse(request_path)
        qs = parse_qs(url.query)
        query = (qs.get("q") or qs.get("query") or [""])[0].strip()
        try:
            limit = min(int((qs.get("limit") or ["6"])[0]), 15)
        except Exception:
            limit = 6
        if not query:
            return self._send_simple_and_close(wr, 400, {"error": "missing q"})
        is_youtube = "/api/youtube" in (url.path or "")
        try:
            if is_youtube:
                results = await lineman_search.youtube_search(
                    query, proxy="http://127.0.0.1:9090", limit=limit)
            else:
                results = await lineman_search.web_search(
                    query, proxy="http://127.0.0.1:9090", limit=limit)
            self._send_simple_and_close(
                wr, 200, {"query": query, "kind": "youtube" if is_youtube else "web",
                          "count": len(results), "results": results})
        except Exception as e:
            logger.exception("web_search_failed")
            self._send_simple_and_close(wr, 502, {"error": str(e)[:200]})

    async def _read_headers(
        self,
        rd: asyncio.StreamReader,
        timeout: float = 5.0,
    ) -> dict[str, str]:
        """Read HTTP headers until blank line; return lowercase-keyed dict."""
        headers: dict[str, str] = {}
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=timeout)
            if hdr in (b"\r\n", b"\n", b""):
                break
            try:
                name, sep, value = hdr.decode("latin-1", errors="replace").partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            except Exception:
                pass
        return headers

    def _parse_basic_auth(self, headers: dict[str, str]) -> tuple[str, str] | None:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
        except Exception:
            return None
        email, sep, password = decoded.partition(":")
        if not sep or not email or not password:
            return None
        return email.strip(), password

    # --- Сессия по cookie (брендированный логин Shectory вместо Basic popup) ---
    # Токен подписывается тем же SHECTORY_AUTH_BRIDGE_SECRET (стандарт федерации):
    # формат email:expires:HMAC_SHA256("email:expires", secret). HttpOnly cookie.

    def _session_secret(self) -> str:
        return (os.environ.get("SHECTORY_AUTH_BRIDGE_SECRET") or "").strip()

    def _make_session_token(self, email: str, ttl: int = 7 * 86400,
                            now: float | None = None) -> str:
        secret = self._session_secret()
        exp = int((now if now is not None else time.time()) + ttl)
        msg = f"{email.strip().lower()}:{exp}"
        sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return f"{msg}:{sig}"

    def _verify_session_token(self, token: str, now: float | None = None) -> str | None:
        secret = self._session_secret()
        if not secret or not token:
            return None
        try:
            email, exp_s, sig = token.rsplit(":", 2)
        except ValueError:
            return None
        expected = hmac.new(secret.encode(), f"{email}:{exp_s}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            exp = int(exp_s)
        except ValueError:
            return None
        if exp < (now if now is not None else time.time()):
            return None
        return email

    def _session_email_from_cookie(self, headers: dict[str, str],
                                   now: float | None = None) -> str | None:
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "shectory_session" and v:
                return self._verify_session_token(v, now=now)
        return None

    async def _verify_portal_credentials(self, email: str, password: str) -> bool:
        """Verify credentials against Shectory Portal bridge with positive-cache TTL."""
        key = hashlib.sha256(f"{email.lower()}:{password}".encode("utf-8")).hexdigest()
        now = time.time()
        exp = self._portal_auth_cache.get(key)
        if exp and exp > now:
            return True

        secret = (os.environ.get("SHECTORY_AUTH_BRIDGE_SECRET") or "").strip()
        if not secret:
            logger.warning("portal_auth_no_secret")
            return False
        base = (os.environ.get("SHECTORY_PORTAL_URL")
                or "http://127.0.0.1:3000").rstrip("/")
        url = f"{base}/api/internal/verify-portal-credentials"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Content-Type": "application/json",
                    },
                    json={"email": email, "password": password},
                ) as r:
                    if r.status != 200:
                        return False
                    data = await r.json(content_type=None)
                    if not data or not data.get("ok"):
                        return False
        except Exception as e:
            logger.warning("portal_auth_check_failed", error=str(e)[:160])
            return False

        self._portal_auth_cache[key] = now + self._portal_auth_ttl
        if len(self._portal_auth_cache) > 256:
            self._portal_auth_cache = {
                k: v for k, v in self._portal_auth_cache.items() if v > now
            }
        return True

    async def _send_401_basic(
        self,
        wr: asyncio.StreamWriter,
        realm: str = "Shectory Portal",
    ) -> None:
        body = b'{"error":"auth required"}'
        wr.write(
            f"HTTP/1.1 401 Unauthorized\r\n"
            f"WWW-Authenticate: Basic realm=\"{realm}\", charset=\"UTF-8\"\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_login(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """POST /api/login {email,password} — проверка через portal bridge,
        на успехе ставит HttpOnly cookie shectory_session (HMAC). Без Basic popup."""
        headers = await self._read_headers(rd)
        clen = int(headers.get("content-length", "0") or "0")
        raw = await asyncio.wait_for(rd.read(min(clen, 8192)), timeout=10) if clen > 0 else b""
        try:
            data = json.loads(raw or b"{}")
            email = str(data.get("email", "")).strip()
            password = str(data.get("password", ""))
        except Exception:
            self._send_simple_and_close(wr, 400, {"error": "bad request"})
            await wr.drain(); wr.close(); return

        if not email or not password:
            self._send_simple_and_close(wr, 400, {"error": "Введите e-mail и пароль"})
            await wr.drain(); wr.close(); return
        if not self._session_secret():
            self._send_simple_and_close(wr, 503, {"error": "Сервис авторизации не настроен"})
            await wr.drain(); wr.close(); return

        ok = await self._verify_portal_credentials(email, password)
        if not ok:
            self._send_simple_and_close(wr, 401, {"error": "Неверный e-mail или пароль"})
            await wr.drain(); wr.close(); return

        token = self._make_session_token(email)
        body = b'{"ok":true}'
        wr.write(
            f"HTTP/1.1 200 OK\r\n"
            f"Set-Cookie: shectory_session={token}; Path=/; HttpOnly; Secure; "
            f"SameSite=Lax; Max-Age={7 * 86400}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_miniapp_auth(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """POST /api/tg/miniapp-auth {initData} — валидирует Telegram initData ключом
        KLOD_BOT_TOKEN, сверяет user.id с allowlist, ставит session cookie (как /api/login).
        SameSite=None — миниаппа живёт во webview Telegram (third-party контекст)."""
        headers = await self._read_headers(rd)
        clen = int(headers.get("content-length", "0") or "0")
        raw = await asyncio.wait_for(rd.read(min(clen, 8192)), timeout=10) if clen > 0 else b""
        try:
            data = json.loads(raw or b"{}")
            init_data = str(data.get("initData", ""))
        except Exception:
            self._send_simple_and_close(wr, 400, {"error": "bad request"})
            await wr.drain(); wr.close(); return

        bot_token = (os.environ.get("KLOD_BOT_TOKEN") or "").strip()
        if not bot_token or not self._session_secret():
            self._send_simple_and_close(wr, 503, {"error": "miniapp не настроен"})
            await wr.drain(); wr.close(); return

        allowed = {x for x in os.environ.get("KLOD_MINIAPP_ALLOW", "36910539").split(",") if x}
        parsed = validate_init_data(init_data, bot_token)
        if not user_id_allowed(parsed, allowed):
            self._send_simple_and_close(wr, 403, {"error": "forbidden"})
            await wr.drain(); wr.close(); return

        uid = str(parsed["user"]["id"])
        token = self._make_session_token(f"telegram:{uid}")
        body = b'{"ok":true}'
        wr.write(
            f"HTTP/1.1 200 OK\r\n"
            f"Set-Cookie: shectory_session={token}; Path=/; HttpOnly; Secure; "
            f"SameSite=None; Max-Age={7 * 86400}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        )
        wr.write(body)
        await wr.drain()
        wr.close()

    async def _raw_api_builder_tickets(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """GET /api/builder/tickets — очередь + аудит klod-builder для дашборда."""
        await self._read_headers(rd)
        qpath = os.path.expanduser(
            os.environ.get("BUILDER_QUEUE", "~/.builder/queue.json"))
        apath = os.path.join(os.path.dirname(qpath), "audit.jsonl")
        try:
            tickets = json.loads(open(qpath, encoding="utf-8").read()) \
                if os.path.exists(qpath) else []
        except Exception:
            tickets = []
        audit: list = []
        if os.path.exists(apath):
            try:
                for line in open(apath, encoding="utf-8", errors="replace") \
                        .read().splitlines()[-60:]:
                    line = line.strip()
                    if line:
                        try:
                            audit.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                pass
        self._send_simple_and_close(wr, 200, build_builder_status(tickets, audit))
        await wr.drain()
        wr.close()

    async def _raw_dashboard(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
        filename: str = "index.html",
        drain_headers: bool = True,
    ) -> None:
        """GET /dashboard|/klod-chat — serve dashboard/<filename>."""
        if drain_headers:
            while True:
                hdr = await asyncio.wait_for(rd.readline(), timeout=5)
                if hdr in (b"\r\n", b"\n", b""):
                    break

        html_path = Path(__file__).resolve().parent / "dashboard" / filename
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
