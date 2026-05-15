"""Smart routing — maps request context to provider/model with fallback.

Inspired by claude-code-router.
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class RouteContext(Enum):
    """Request context categories."""
    DEFAULT = "default"
    THINK = "think"
    BACKGROUND = "background"
    LONG_CONTEXT = "longContext"
    WEB_SEARCH = "webSearch"


@dataclass(frozen=True)
class Route:
    """Resolved route target."""
    provider: str
    model: str


FALLBACK_CHAINS: dict[RouteContext, list[Route]] = {
    RouteContext.DEFAULT: [
        Route("deepseek", "deepseek-v4-flash"),
        Route("deepseek", "deepseek-v4-pro"),
        Route("gemini", "gemini-2.5-flash"),
    ],
    RouteContext.BACKGROUND: [
        Route("deepseek", "deepseek-v4-flash"),
        Route("deepseek", "deepseek-v4-pro"),
        Route("gemini", "gemini-2.5-flash"),
    ],
    RouteContext.THINK: [
        Route("deepseek", "deepseek-v4-pro"),
        Route("gemini", "gemini-3.1-pro-preview"),
        Route("gemini", "gemini-2.5-flash"),
    ],
    RouteContext.LONG_CONTEXT: [
        Route("gemini", "gemini-3.1-pro-preview"),
        Route("deepseek", "deepseek-v4-pro"),
    ],
    RouteContext.WEB_SEARCH: [
        Route("gemini", "gemini-2.5-flash"),
        Route("deepseek", "deepseek-v4-flash"),
    ],
}


class Router:
    """Resolve request context to provider/model with fallback."""

    def __init__(self, routing_config: dict[str, Any]) -> None:
        self._config = routing_config
        self._long_context_threshold = routing_config.get(
            "longContextThreshold", 60000
        )
        self._decisions: collections.deque = collections.deque(maxlen=50)
        self._build_map()

    def recent_decisions(self) -> list[dict]:
        """Return recent routing decisions, newest first."""
        return list(reversed(self._decisions))

    def _build_map(self) -> None:
        """Build primary route map from config overrides."""
        self._primary: dict[RouteContext, Route] = {}
        for ctx_name in RouteContext:
            entry = self._config.get(ctx_name.value)
            if entry:
                self._primary[ctx_name] = Route(
                    provider=entry.get("provider", "deepseek"),
                    model=entry.get("model", "deepseek-v4-flash"),
                )

    def resolve(self, context: RouteContext) -> Route:
        """Resolve a context to primary route."""
        route = self._primary.get(context)
        if route is not None:
            return route
        fallback = FALLBACK_CHAINS.get(context)
        if fallback:
            return fallback[0]
        return Route("deepseek", "deepseek-v4-flash")

    def fallback_chain(self, context: RouteContext) -> list[Route]:
        """Return fallback chain for a context, starting from the given context."""
        config_route = self._primary.get(context)
        chain: list[Route] = []
        if config_route:
            chain.append(config_route)

        fallback = FALLBACK_CHAINS.get(context, [])
        for route in fallback:
            if route not in chain:
                chain.append(route)
        return chain

    def detect_context(
        self,
        request_body: bytes | None,
        headers: dict[str, str],
        estimated_tokens: int = 0,
    ) -> RouteContext:
        """Auto-detect route context from request body and headers."""
        triggered_by = "body"
        snippet: str | None = None

        route_header = headers.get("X-Lineman-Route", "")
        if route_header:
            try:
                ctx = RouteContext(route_header)
                route = self.resolve(ctx)
                self._decisions.append({
                    "ts": time.time(),
                    "context": ctx.value,
                    "provider": route.provider,
                    "model": route.model,
                    "triggered_by": "header",
                    "snippet": None,
                })
                return ctx
            except ValueError:
                pass

        body_text = ""
        if request_body:
            try:
                body_text = request_body.decode("utf-8", errors="replace")
                snippet = body_text[:120].replace("\n", " ")
            except (UnicodeDecodeError, AttributeError):
                body_text = ""

        body_lower = body_text.lower()
        if any(kw in body_lower for kw in ("background", "фонов", "in background", "async task")):
            ctx = RouteContext.BACKGROUND
        elif ("thinking" in body_lower or "reasoning" in body_lower
                or '"thinking"' in body_lower or "'thinking'" in body_lower):
            ctx = RouteContext.THINK
        elif ("web_search" in body_text or "webSearch" in body_text
                or '"search"' in body_text):
            ctx = RouteContext.WEB_SEARCH
        elif estimated_tokens > self._long_context_threshold:
            ctx = RouteContext.LONG_CONTEXT
        else:
            ctx = RouteContext.DEFAULT

        route = self.resolve(ctx)
        self._decisions.append({
            "ts": time.time(),
            "context": ctx.value,
            "provider": route.provider,
            "model": route.model,
            "triggered_by": triggered_by,
            "snippet": snippet,
        })
        return ctx

    @property
    def long_context_threshold(self) -> int:
        return self._long_context_threshold

    @long_context_threshold.setter
    def long_context_threshold(self, value: int) -> None:
        self._long_context_threshold = value
