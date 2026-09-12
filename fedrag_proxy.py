"""Прокси к индексу fedrag на sdev.

Демон ragkit слушает только loopback на sdev — так задумано его автором, и ломать это
незачем. Агентам федерации на hoster, vibe и pi нужен доступ, поэтому единственным
входом становится Lineman: он и так единственный шлюз для LLM и секретов.

Что здесь есть сверх простого проксирования:

* **Кэш.** Канон меняется раз в сутки, а вопросы агентов повторяются. Демон обслуживает
  запросы по одному, поэтому каждый ответ из кэша — это ещё и снятая очередь.
* **Лимит на агента.** Демон один на всю федерацию. Зациклившийся агент без лимита
  занимает его целиком и ослепляет остальных.
* **Внятная деградация.** Если sdev недоступен, агент должен получить короткий ответ
  «RAG недоступен, смотри карточку», а не висеть до таймаута.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from typing import Any

import aiohttp
import structlog

log = structlog.get_logger(__name__)

# Локальный конец ssh-туннеля на smain (PM2-процесс fedrag-tunnel, scripts/fedrag_tunnel.sh).
# Прямой адрес sdev по WG тут не годится: демон слушает только loopback на своём узле.
DEFAULT_URL = "http://127.0.0.1:8791/search"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_CACHE_TTL_S = 3600.0
DEFAULT_CACHE_MAX = 512
DEFAULT_RATE_PER_MIN = 20
MAX_QUERY_LEN = 2000

# Профиль поиска на стороне ragkit. Демон умеет искать по нескольким индексам сразу,
# но Searcher штрафует всё, кроме первого индекса (secondary_penalty), а первым у него
# стоит STL. Для вопросов про федерацию это давало чанки кода STL вместо канона:
# замер 2026-09-12 — recall@1 0.20 и медиана 8.4с против 0.73 и 0.32с на профиле fed.
# Старый демон лишние поля тела игнорирует, поэтому параметр безопасен и до обновления.
DEFAULT_PROFILE = "fed"

# Что агент видит, когда индекс недоступен: коротко и по делу, дальше он идёт в карточку.
FALLBACK_TEXT = (
    "RAG федерации недоступен. Действуй по своей карточке агента, "
    "а если нужного там нет — спроси Клода через /api/agent/klod-access/message."
)


def _stale_body(payload: dict[str, Any], age_s: int) -> dict[str, Any]:
    """Ответ из кэша с честной пометкой о возрасте.

    Агент обязан видеть, что ответ не свежий: канон меняется редко, но решение
    «удалить», «переключить», «выдать доступ» по устаревшему ответу может стоить дорого.
    """
    return {
        "text": payload["text"],
        "source": "stale",
        "age_s": age_s,
        "warning": ("Ответ из кэша, возраст %d мин: свежий индекс сейчас недоступен. "
                    "Для необратимых действий сверься с первоисточником." % (age_s // 60)),
    }


class FedRagProxy:
    """Кэширующий прокси с лимитом на агента к демону ragkit."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = (config or {}).get("fedrag", {}) or {}
        self.url: str = cfg.get("url", DEFAULT_URL)
        self.timeout_s: float = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
        self.cache_ttl_s: float = float(cfg.get("cache_ttl_s", DEFAULT_CACHE_TTL_S))
        self.cache_max: int = int(cfg.get("cache_max", DEFAULT_CACHE_MAX))
        self.rate_per_min: int = int(cfg.get("rate_per_min", DEFAULT_RATE_PER_MIN))
        self.profile: str = cfg.get("profile", DEFAULT_PROFILE)
        self.enabled: bool = bool(cfg.get("enabled", True))

        # ключ -> (истекает, ответ)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # агент -> отметки времени запросов за последнюю минуту
        self._calls: dict[str, deque[float]] = {}
        self._stats = {"hits": 0, "misses": 0, "errors": 0, "throttled": 0, "stale": 0}

    # ------------------------------------------------------------------ вспомогательное
    @staticmethod
    def _key(query: str, depth: str) -> str:
        raw = "%s\x00%s" % (depth, query.strip().lower())
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        """Свежая запись или None. Протухшую НЕ удаляем — она нужна как аварийный запас."""
        hit = self._cache.get(key)
        if not hit:
            return None
        expires, payload = hit
        if expires < time.time():
            return None
        return payload

    def _cache_get_stale(self, key: str) -> tuple[dict[str, Any], int] | None:
        """Протухшая запись и её возраст в секундах.

        Во время аварии ответ по канону трёхчасовой давности несравнимо полезнее,
        чем «индекс недоступен»: канон меняется раз в сутки, а не ежеминутно.
        """
        hit = self._cache.get(key)
        if not hit:
            return None
        expires, payload = hit
        age = int(time.time() - (expires - self.cache_ttl_s))
        return payload, age

    def _cache_put(self, key: str, payload: dict[str, Any]) -> None:
        if len(self._cache) >= self.cache_max:
            # Выбрасываем самую близкую к истечению запись: дешевле полноценного LRU,
            # а кэш здесь небольшой и однородный.
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (time.time() + self.cache_ttl_s, payload)

    def _throttled(self, agent: str) -> bool:
        """Скользящее окно в минуту на агента."""
        now = time.time()
        marks = self._calls.setdefault(agent, deque())
        while marks and now - marks[0] > 60.0:
            marks.popleft()
        if len(marks) >= self.rate_per_min:
            return True
        marks.append(now)
        return False

    def stats(self) -> dict[str, Any]:
        return dict(self._stats, cached=len(self._cache), agents=len(self._calls))

    # ------------------------------------------------------------------------ поиск
    async def search(
        self,
        query: str,
        depth: str = "snippet",
        agent: str = "unknown",
    ) -> tuple[int, dict[str, Any]]:
        """Вернуть (http-статус, тело ответа).

        Ошибка индекса никогда не становится ошибкой агента: он получает 200 и текст
        деградации, чтобы продолжить работу по карточке, а не встать.
        """
        query = (query or "").strip()
        if not query:
            return 400, {"error": "query обязателен"}
        if len(query) > MAX_QUERY_LEN:
            return 400, {"error": "query длиннее %d символов" % MAX_QUERY_LEN}
        if not self.enabled:
            return 200, {"text": FALLBACK_TEXT, "source": "disabled"}

        key = self._key(query, depth)
        cached = self._cache_get(key)
        if cached is not None:
            self._stats["hits"] += 1
            return 200, dict(cached, source="cache")

        if self._throttled(agent):
            self._stats["throttled"] += 1
            log.warning("fedrag_throttled", agent=agent, limit=self.rate_per_min)
            # Упёрлись в лимит — но если ответ на этот вопрос уже лежит, отдать его
            # дешевле и полезнее отказа: он не ходит в сеть и не занимает демон.
            stale = self._cache_get_stale(key)
            if stale is not None:
                payload, age = stale
                self._stats["stale"] += 1
                return 200, dict(_stale_body(payload, age), throttled=True)
            return 429, {
                "error": "слишком часто, лимит %d запросов в минуту" % self.rate_per_min,
                "retry_after": 60,
            }

        self._stats["misses"] += 1
        started = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.url,
                    json={"query": query, "depth": depth, "profile": self.profile},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError("ragkit ответил %d" % resp.status)
                    payload = await resp.json()
        # OSError здесь обязателен: сетевые сбои (обрыв, отказ в соединении, DNS)
        # всплывают именно им, и без него недоступный sdev валил бы запрос агента
        # вместо деградации. Поймано тестом test_недоступный_индекс_не_роняет_агента.
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError,
                RuntimeError, ValueError) as exc:
            self._stats["errors"] += 1
            log.warning(
                "fedrag_unavailable",
                agent=agent,
                error=str(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            stale = self._cache_get_stale(key)
            if stale is not None:
                payload, age = stale
                self._stats["stale"] += 1
                log.info("fedrag_stale_served", agent=agent, age_s=age)
                return 200, _stale_body(payload, age)
            return 200, {"text": FALLBACK_TEXT, "source": "fallback", "error": str(exc)}

        latency_ms = int((time.monotonic() - started) * 1000)
        result = {"text": payload.get("text", ""), "source": "ragkit",
                  "latency_ms": latency_ms}
        self._cache_put(key, {"text": result["text"], "latency_ms": latency_ms})
        log.info("fedrag_search", agent=agent, latency_ms=latency_ms,
                 chars=len(result["text"]))
        return 200, result
