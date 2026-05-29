"""Shared retry helper with exponential backoff."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def retry_with_backoff(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    **kwargs: Any,
) -> Any:
    """Call fn(args, kwargs) with exponential backoff on failure.

    Retries on exceptions only. Returns on first success.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                jitter = delay * 0.1 * random.random()
                total_delay = delay + jitter
                logger.debug(
                    "retry_wait",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay=round(total_delay, 2),
                    error=str(exc),
                )
                await asyncio.sleep(total_delay)

    raise last_exc  # type: ignore[misc]
