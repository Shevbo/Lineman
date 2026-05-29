# Lineman Dashboard Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить 7 структурных дефектов дашборда: агентская атрибуция сигналов, промпты в правой панели, алгоритм маршрутизации в левой панели, фильтрация отчёта, агентская модалка, и документация.

**Architecture:** Все проблемы восходят к одному корню — дашборд читает только `signals` (path C), игнорируя `request_log` (path B). Исправление: (1) обогатить auto-signal из reverse proxy агентским именем и сниппетом промпта, (2) добавить in-memory ring buffer решений роутера, (3) добавить фильтрацию по периоду в report API, (4) в UI подключиться к новым данным.

**Tech Stack:** Python 3.12, asyncio, SQLite (WAL), aiohttp, pytest + pytest-asyncio, vanilla JS (no framework).

---

## File Map

| Файл | Действие | Зона ответственности |
|------|----------|----------------------|
| `signals.py` | Modify | Добавить колонку `prompt_snippet`, обновить `_KNOWN_COLS` |
| `reverse_proxy.py` | Modify | Извлечь `X-Agent-Name`, добавить `prompt_snippet` в auto-signal |
| `router.py` | Modify | Добавить `RoutingDecisionLog` ring buffer, логировать каждый `detect_context()` |
| `proxy_server.py` | Modify | Добавить `since`/`until` в `/api/report`; добавить `/api/routing/decisions` |
| `dashboard/index.html` | Modify | Правая панель (промпты), левая панель (алгоритм + решения), агент модалка (fallback), отчёт (date picker) |
| `WIKI.md` | Modify | Data flow diagram, integration checklist, known limitations |
| `tests/conftest.py` | Create | pytest fixtures: in-memory SQLite, SignalQueue, RequestLogDB |
| `tests/test_signals.py` | Create | Тесты SignalQueue с `prompt_snippet` |
| `tests/test_router.py` | Create | Тесты RoutingDecisionLog + detect_context |
| `tests/test_reverse_proxy.py` | Create | Тесты token extraction, agent header parsing |
| `tests/test_report_filter.py` | Create | Тесты since/until фильтрации отчёта |

---

## Task 1: Установить pytest в venv

**Files:**
- No file changes

- [ ] **Step 1: Установить зависимости**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pip install pytest pytest-asyncio
```

Expected output: `Successfully installed pytest-... pytest-asyncio-...`

- [ ] **Step 2: Проверить pytest работает**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest --version
```

Expected: `pytest X.Y.Z`

- [ ] **Step 3: Создать conftest.py**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for Lineman tests."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import AsyncGenerator

import pytest

# Add project root to path so tests can import lineman modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def in_memory_conn() -> sqlite3.Connection:
    """SQLite in-memory connection with WAL-compatible settings."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@pytest.fixture
def signal_queue(in_memory_conn):
    """Initialised SignalQueue backed by in-memory SQLite."""
    from signals import SignalQueue
    lock = asyncio.Lock()
    sq = SignalQueue(in_memory_conn, lock)
    sq.init_table()
    return sq


@pytest.fixture
def request_log_db(tmp_path):
    """RequestLogDB backed by a temp-file SQLite."""
    from db import RequestLogDB
    db = RequestLogDB(path=tmp_path / "test.db")
    db.init()
    return db
```

- [ ] **Step 4: Проверить что conftest импортируется без ошибок**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/conftest.py --collect-only
```

Expected: `no tests ran` (no errors)

---

## Task 2: Добавить `prompt_snippet` в сигналы

**Files:**
- Modify: `signals.py`
- Create: `tests/test_signals.py`

**Контекст:** `signals.py:50` — `_KNOWN_COLS` фильтрует поля при INSERT. Таблица не имеет `prompt_snippet`. Нужно добавить колонку и включить её в белый список.

- [ ] **Step 1: Написать падающий тест**

Create `tests/test_signals.py`:

```python
"""Tests for SignalQueue with prompt_snippet field."""
from __future__ import annotations
import asyncio
import pytest


@pytest.mark.asyncio
async def test_signal_stores_prompt_snippet(signal_queue):
    """prompt_snippet must be persisted and returned by recent()."""
    await signal_queue.async_enqueue({
        "ts": 1_000_000.0,
        "from_node": "smain",
        "to_service": "deepseek",
        "type": "prompt",
        "tokens_in": 512,
        "tokens_out": 128,
        "latency_ms": 300,
        "status": "ok",
        "prompt_snippet": "Tell me about routing algorithms",
    })
    results = await signal_queue.recent(since_ts=0)
    assert len(results) == 1
    assert results[0]["prompt_snippet"] == "Tell me about routing algorithms"


@pytest.mark.asyncio
async def test_signal_prompt_snippet_none_ok(signal_queue):
    """Signals without prompt_snippet should still work (backward compat)."""
    await signal_queue.async_enqueue({
        "ts": 1_000_001.0,
        "from_node": "smain",
        "to_service": "gemini",
        "type": "prompt",
        "status": "ok",
    })
    results = await signal_queue.recent(since_ts=0)
    assert len(results) == 1
    assert results[0].get("prompt_snippet") is None


@pytest.mark.asyncio
async def test_signal_prompt_snippet_truncated_at_300(signal_queue):
    """Verify we can store and retrieve exactly 300 chars (boundary test)."""
    snippet = "x" * 300
    await signal_queue.async_enqueue({
        "ts": 1_000_002.0,
        "from_node": "smain",
        "to_service": "deepseek",
        "type": "prompt",
        "prompt_snippet": snippet,
    })
    results = await signal_queue.recent(since_ts=0)
    assert results[0]["prompt_snippet"] == snippet
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_signals.py -v
```

Expected: `FAILED` — `prompt_snippet` не в таблице

- [ ] **Step 3: Добавить колонку в signals.py**

В `signals.py` изменить:

```python
# Было (строка 12):
_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    from_agent TEXT,
    from_node  TEXT,
    to_service TEXT,
    type       TEXT,
    model      TEXT,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    latency_ms INTEGER,
    status     TEXT
);
"""
```

```python
# Стало:
_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    from_agent     TEXT,
    from_node      TEXT,
    to_service     TEXT,
    type           TEXT,
    model          TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    latency_ms     INTEGER,
    status         TEXT,
    prompt_snippet TEXT
);
"""
```

В `signals.py` изменить `_KNOWN_COLS` (строка 50):

```python
# Было:
_KNOWN_COLS = frozenset({
    "ts", "from_agent", "from_node", "to_service",
    "type", "model", "tokens_in", "tokens_out", "latency_ms", "status",
})
```

```python
# Стало:
_KNOWN_COLS = frozenset({
    "ts", "from_agent", "from_node", "to_service",
    "type", "model", "tokens_in", "tokens_out", "latency_ms", "status",
    "prompt_snippet",
})
```

В `signals.py` добавить миграцию в `init_table()` (после строки `self._conn.commit()`):

```python
def init_table(self) -> None:
    """Create table + indexes. Call once at startup (sync)."""
    self._conn.execute(_CREATE_SIGNALS)
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_ts    ON signals(ts);"
    )
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_agent ON signals(from_agent);"
    )
    # Migration: add prompt_snippet if upgrading from older schema
    try:
        self._conn.execute("ALTER TABLE signals ADD COLUMN prompt_snippet TEXT;")
    except Exception:
        pass  # column already exists
    self._conn.commit()
    logger.info("signals_table_initialized")
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_signals.py -v
```

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add signals.py tests/conftest.py tests/test_signals.py
git commit -m "feat: add prompt_snippet field to signals table with migration"
```

---

## Task 3: Агентская атрибуция + prompt_snippet в auto-signal

**Files:**
- Modify: `reverse_proxy.py`
- Create: `tests/test_reverse_proxy.py`

**Контекст:** `reverse_proxy.py:72-83` читает заголовки запроса в `req_headers`. Нужно извлечь `X-Agent-Name`. Затем в `reverse_proxy.py:241-257` авто-сигнал не передаёт `from_agent` и `prompt_snippet`.

- [ ] **Step 1: Написать падающие тесты**

Create `tests/test_reverse_proxy.py`:

```python
"""Tests for agent header extraction and prompt snippet extraction."""
from __future__ import annotations
import json
import pytest

# Import the module-level helper functions directly
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_extract_agent_name_from_header():
    """X-Agent-Name header must be extracted and returned."""
    from reverse_proxy import _extract_agent_name
    headers = {
        "content-type": "application/json",
        "x-agent-name": "selfcoder",
        "authorization": "Bearer sk-xxx",
    }
    assert _extract_agent_name(headers) == "selfcoder"


def test_extract_agent_name_missing():
    """Returns None when header absent."""
    from reverse_proxy import _extract_agent_name
    assert _extract_agent_name({"content-type": "application/json"}) is None


def test_extract_prompt_snippet_from_messages():
    """Extracts last user message content, truncated to 300 chars."""
    from reverse_proxy import _extract_prompt_snippet
    body = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is the capital of France?"},
        ]
    }).encode()
    result = _extract_prompt_snippet(body)
    assert result == "What is the capital of France?"


def test_extract_prompt_snippet_truncation():
    """Snippet truncated to max 300 chars."""
    from reverse_proxy import _extract_prompt_snippet
    long_content = "A" * 500
    body = json.dumps({
        "messages": [{"role": "user", "content": long_content}]
    }).encode()
    result = _extract_prompt_snippet(body)
    assert result is not None
    assert len(result) <= 300


def test_extract_prompt_snippet_invalid_json():
    """Returns None for non-JSON body."""
    from reverse_proxy import _extract_prompt_snippet
    assert _extract_prompt_snippet(b"not json") is None


def test_extract_prompt_snippet_no_messages():
    """Returns None when messages field absent."""
    from reverse_proxy import _extract_prompt_snippet
    body = json.dumps({"model": "deepseek-v4-flash"}).encode()
    assert _extract_prompt_snippet(body) is None
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_reverse_proxy.py -v
```

Expected: `FAILED` — `cannot import name '_extract_agent_name'`

- [ ] **Step 3: Добавить функции в reverse_proxy.py**

В `reverse_proxy.py` добавить две функции **до** `handle_reverse_proxy` (после строки с `logger = ...`):

```python
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
        # Walk from end to find last non-system message
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content[:max_len]
            if isinstance(content, list):
                # Anthropic-style content blocks
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            return text[:max_len]
        return None
    except Exception:
        return None
```

- [ ] **Step 4: Использовать функции в handle_reverse_proxy**

В `reverse_proxy.py` после строки `req_model = req_json.get("model", "")` (строка ~104) добавить:

```python
    # Extract agent name from header for signal attribution
    agent_name = _extract_agent_name(req_headers)
    prompt_snippet = _extract_prompt_snippet(req_body)
```

В блоке авто-сигнала `reverse_proxy.py:241-257` добавить поля:

```python
    # Auto-emit signal for dashboard
    if signals is not None:
        try:
            from db import source_host_from_ip
            asyncio.create_task(signals.async_enqueue({
                "ts": time.time(),
                "from_agent": agent_name,          # ← было None, теперь из заголовка
                "from_node": source_host_from_ip(source_ip),
                "to_service": provider,
                "type": "prompt" if status_code < 400 else "error",
                "model": req_model or None,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
                "status": "ok" if status_code < 400 else "error",
                "prompt_snippet": prompt_snippet,  # ← новое поле
            }))
        except Exception:
            pass
```

В блоке логирования в БД `reverse_proxy.py:218-238` добавить `source_agent`:

```python
    if db is not None:
        try:
            from db import source_host_from_ip
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_host": source_host_from_ip(source_ip),
                "source_agent": agent_name,        # ← новое поле
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
```

- [ ] **Step 5: Запустить тесты**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_reverse_proxy.py -v
```

Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add reverse_proxy.py tests/test_reverse_proxy.py
git commit -m "feat: extract X-Agent-Name and prompt_snippet in reverse proxy auto-signal"
```

---

## Task 4: Routing Decisions Ring Buffer

**Files:**
- Modify: `router.py`
- Create: `tests/test_router.py`

**Контекст:** `router.py:104` — `detect_context()` принимает решения молча. Нужен in-memory ring buffer последних 50 решений, доступный через `/api/routing/decisions`.

- [ ] **Step 1: Написать падающие тесты**

Create `tests/test_router.py`:

```python
"""Tests for Router routing decisions log."""
from __future__ import annotations
import pytest
from router import Router, RouteContext


SAMPLE_CONFIG = {
    "default":        {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "think":          {"provider": "deepseek", "model": "deepseek-v4-pro"},
    "longContext":    {"provider": "gemini",   "model": "gemini-3.1-pro-preview"},
    "webSearch":      {"provider": "gemini",   "model": "gemini-2.5-flash"},
    "background":     {"provider": "deepseek", "model": "deepseek-v4-flash"},
    "longContextThreshold": 60000,
}


def test_decisions_log_records_detect_context():
    """After detect_context(), decisions list must contain one entry."""
    router = Router(SAMPLE_CONFIG)
    body = b'{"messages":[{"role":"user","content":"Hello world"}]}'
    ctx = router.detect_context(body, {})
    decisions = router.recent_decisions()
    assert len(decisions) == 1
    assert decisions[0]["context"] == ctx.value
    assert decisions[0]["provider"] is not None
    assert decisions[0]["model"] is not None
    assert "ts" in decisions[0]


def test_decisions_log_captures_keywords():
    """Decision entry must record the snippet that triggered routing."""
    router = Router(SAMPLE_CONFIG)
    body = b'{"messages":[{"role":"user","content":"Enable thinking mode please"}]}'
    router.detect_context(body, {})
    dec = router.recent_decisions()[0]
    assert dec["context"] == "think"
    assert dec["snippet"] is not None


def test_decisions_log_max_50():
    """Ring buffer must not exceed 50 entries."""
    router = Router(SAMPLE_CONFIG)
    for i in range(60):
        router.detect_context(None, {})
    assert len(router.recent_decisions()) == 50


def test_decisions_log_explicit_header_override():
    """X-Lineman-Route header must be recorded as the context."""
    router = Router(SAMPLE_CONFIG)
    router.detect_context(None, {"X-Lineman-Route": "webSearch"})
    dec = router.recent_decisions()[0]
    assert dec["context"] == "webSearch"
    assert dec["triggered_by"] == "header"


def test_decisions_log_auto_detected():
    """Auto-detected context must record triggered_by='body'."""
    router = Router(SAMPLE_CONFIG)
    body = b'{"messages":[{"role":"user","content":"background task please"}]}'
    router.detect_context(body, {})
    dec = router.recent_decisions()[0]
    assert dec["triggered_by"] == "body"
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_router.py -v
```

Expected: `FAILED` — `Router has no attribute 'recent_decisions'`

- [ ] **Step 3: Добавить RoutingDecisionLog в router.py**

В `router.py` добавить импорт в начало файла:

```python
import collections
import time
```

В `router.py` изменить `__init__` класса `Router` — добавить ring buffer:

```python
def __init__(self, routing_config: dict[str, Any]) -> None:
    self._config = routing_config
    self._long_context_threshold = routing_config.get(
        "longContextThreshold", 60000
    )
    self._decisions: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)
    self._build_map()
```

В `router.py` добавить метод `recent_decisions` в класс `Router`:

```python
def recent_decisions(self) -> list[dict[str, Any]]:
    """Return recent routing decisions (newest first)."""
    return list(reversed(self._decisions))
```

В `router.py` изменить `detect_context` — добавить запись решений. Заменить весь метод `detect_context` (строки 104–149):

```python
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
            triggered_by = "header"
            route = self.resolve(ctx)
            self._decisions.append({
                "ts": time.time(),
                "context": ctx.value,
                "provider": route.provider,
                "model": route.model,
                "triggered_by": triggered_by,
                "snippet": None,
            })
            return ctx
        except ValueError:
            pass

    body_text = ""
    if request_body:
        try:
            body_text = request_body.decode("utf-8", errors="replace")
            # Extract snippet: first 120 chars of body for logging
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
```

- [ ] **Step 4: Запустить тесты**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_router.py -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add router.py tests/test_router.py
git commit -m "feat: add RoutingDecisionLog ring buffer to Router (50 entries)"
```

---

## Task 5: `/api/routing/decisions` endpoint + Report period filter

**Files:**
- Modify: `proxy_server.py`
- Create: `tests/test_report_filter.py`

**Контекст:** `proxy_server.py:1019` — `_raw_api_report` не принимает `since`/`until`. Нужно: (1) добавить параметры в `/api/report`, (2) добавить новый эндпоинт `/api/routing/decisions`.

- [ ] **Step 1: Написать падающий тест для фильтрации отчёта**

Create `tests/test_report_filter.py`:

```python
"""Tests for /api/report period filtering via RequestLogDB."""
from __future__ import annotations
import asyncio
import pytest


@pytest.mark.asyncio
async def test_query_logs_since_filter(request_log_db):
    """query_logs with since filter excludes earlier rows."""
    await request_log_db.log_request({
        "timestamp": "2026-01-01T00:00:00+00:00",
        "llm_model": "deepseek-v4-flash",
        "tokens_in": 100, "tokens_out": 50,
    })
    await request_log_db.log_request({
        "timestamp": "2026-05-15T12:00:00+00:00",
        "llm_model": "deepseek-v4-flash",
        "tokens_in": 200, "tokens_out": 80,
    })
    # Query only recent
    rows = await request_log_db.query_logs(since="2026-05-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["tokens_in"] == 200


@pytest.mark.asyncio
async def test_query_logs_until_filter(request_log_db):
    """query_logs with until filter excludes later rows."""
    await request_log_db.log_request({
        "timestamp": "2026-01-01T00:00:00+00:00",
        "llm_model": "deepseek-v4-pro",
        "tokens_in": 500, "tokens_out": 200,
    })
    await request_log_db.log_request({
        "timestamp": "2026-05-15T12:00:00+00:00",
        "llm_model": "deepseek-v4-pro",
        "tokens_in": 800, "tokens_out": 300,
    })
    rows = await request_log_db.query_logs(until="2026-02-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["tokens_in"] == 500
```

- [ ] **Step 2: Запустить тест (должен ПРОЙТИ — query_logs уже поддерживает фильтры)**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/test_report_filter.py -v
```

Expected: `2 passed` (db.query_logs уже имеет since/until). Если упал — исправить логику в db.py.

- [ ] **Step 3: Добавить since/until в _raw_api_report**

В `proxy_server.py` найти метод `_raw_api_report` (строка ~1019). Заменить блок чтения заголовков и SQL:

```python
async def _raw_api_report(
    self,
    rd: asyncio.StreamReader,
    wr: asyncio.StreamWriter,
    request_path: str,
) -> None:
    """GET /api/report?since=ISO&until=ISO — token savings vs Claude Sonnet baseline."""
    from urllib.parse import urlparse, parse_qs
    while True:
        hdr = await asyncio.wait_for(rd.readline(), timeout=5)
        if hdr in (b"\r\n", b"\n", b""):
            break

    qs = parse_qs(urlparse(request_path).query)
    since_iso = (qs.get("since") or [None])[0]
    until_iso = (qs.get("until") or [None])[0]

    # Price per 1M tokens (input, output) in USD
    PRICES: dict[str, tuple[float, float]] = {
        "deepseek-v4-flash":      (0.07,  0.28),
        "deepseek-v4-pro":        (0.55,  2.19),
        "deepseek-chat":          (0.07,  0.28),
        "deepseek-reasoner":      (0.55,  2.19),
        "gemini-2.5-flash":       (0.15,  0.60),
        "gemini-3.1-pro-preview": (1.25,  5.00),
        "gemini-2.0-flash":       (0.10,  0.40),
        "gpt-4o":                 (2.50, 10.00),
        "gpt-4o-mini":            (0.15,  0.60),
        "llama3.1:8b":            (0.00,  0.00),
    }
    CLAUDE_IN  = 3.00
    CLAUDE_OUT = 15.00

    rows: list[dict[str, Any]] = []
    total_actual   = 0.0
    total_baseline = 0.0

    try:
        # Build WHERE clause for optional date range
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
        total_actual   += actual
        total_baseline += baseline
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
```

- [ ] **Step 4: Добавить /api/routing/decisions endpoint**

В `proxy_server.py` найти блок роутинга запросов (строка ~236). После строки `elif request_path_only == "/api/routing":` добавить:

```python
            elif request_path_only == "/api/routing/decisions":
                await self._raw_api_routing_decisions(rd, wr)
                return
```

В конец класса `ProxyServer` добавить метод:

```python
    async def _raw_api_routing_decisions(
        self,
        rd: asyncio.StreamReader,
        wr: asyncio.StreamWriter,
    ) -> None:
        """GET /api/routing/decisions — last 50 routing decisions."""
        while True:
            hdr = await asyncio.wait_for(rd.readline(), timeout=5)
            if hdr in (b"\r\n", b"\n", b""):
                break

        decisions: list[dict[str, Any]] = []
        if self._router is not None:
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
```

**Важно:** `self._router` должен быть атрибутом `ProxyServer`. Проверить что он инициализируется при старте. Найти в `__init__` или `start()` где создаётся `Router(...)` и убедиться что присваивается `self._router`.

- [ ] **Step 5: Перезапустить Lineman и проверить эндпоинты**

```bash
curl http://127.0.0.1:9090/api/routing/decisions
```

Expected: `{"decisions":[],"count":0}` (или с данными если уже был трафик)

```bash
curl "http://127.0.0.1:9090/api/report?since=2026-05-01T00:00:00"
```

Expected: JSON с данными только за май

- [ ] **Step 6: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add proxy_server.py tests/test_report_filter.py
git commit -m "feat: add since/until filter to /api/report and /api/routing/decisions endpoint"
```

---

## Task 6: Dashboard — Правая панель (промпты + агент)

**Files:**
- Modify: `dashboard/index.html`

**Контекст:** `index.html:637-664` — `appendPktLog(sig)` формирует строку пакета. Нужно: добавить `prompt_snippet` в отображение. `index.html:644-646` — `fromLabel` показывает только ноду когда нет `from_agent`. Теперь `from_agent` будет заполнен — нужно убедиться что рендерится.

- [ ] **Step 1: Добавить prompt_snippet в appendPktLog**

В `dashboard/index.html` найти функцию `appendPktLog` (строка ~637). Изменить формирование `extra` и `pktLog.push(...)`:

```javascript
function appendPktLog(sig) {
  const type   = isAgentId(sig.to_service||'') ? 'agent_msg' : (sig.type || 'prompt');
  const nodeId = sig.from_node || 'smain';

  let fromLabel, toLabel;
  if (sig.from_agent) {
    const meta = agentPos[sig.from_agent+'@'+nodeId] || {};
    fromLabel = (meta.name || sig.from_agent) + '@' + nodeId;
  } else {
    fromLabel = nodeId;
  }
  toLabel = sig.to_service || '?';
  if (sig.to_agent) toLabel = sig.to_agent + ' (agent)';

  const tkn = sig.tokens_in  ? '+'+fmtN(sig.tokens_in)
            : sig.tokens_out ? '+'+fmtN(sig.tokens_out) : '';
  const lat = sig.latency_ms ? ' '+sig.latency_ms+'ms' : '';
  const extra = (tkn||lat) ? ` <span style="color:#484f58">${tkn}${lat}</span>` : '';

  const snippet = sig.prompt_snippet
    ? `<div class="pkt-snippet" title="${sig.prompt_snippet.replace(/"/g,'&quot;')}">${sig.prompt_snippet.slice(0,80)}…</div>`
    : '';

  const st     = SIG[type] || { c:'#58a6ff', sym:'●' };
  const tsStr  = fmtDateTime(sig.ts);

  pktLog.push({ fromLabel, toLabel, tsStr, type, color: st.c, sym: st.sym, extra, snippet });
  if (pktLog.length > MAX_PKT_LOG) pktLog.shift();

  $id('pkt-count').textContent = pktLog.length;

  if (!pktPaused) renderPktLog();
}
```

В `renderPktLog` добавить вывод snippet:

```javascript
function renderPktLog() {
  const body = $id('pkt-body');
  const visible = pktLog.slice(-200);
  body.innerHTML = visible.map(e =>
    `<div class="pkt-row">
      <span class="pkt-from" title="${e.fromLabel}">${e.fromLabel}</span>
      <span class="pkt-to"   title="${e.toLabel}">${e.toLabel}</span>
      <span class="pkt-ts">${e.tsStr}</span>
      <span class="pkt-type"><span class="lb lb-${e.type}" style="font-size:8px">${e.type}</span>${e.extra}</span>
      ${e.snippet||''}
    </div>`
  ).join('');
  body.scrollTop = body.scrollHeight;
}
```

Добавить CSS для `.pkt-snippet` в `<style>` секцию (найти блок с `.pkt-row`):

```css
.pkt-snippet {
  grid-column: 1 / -1;
  font-size: 9px;
  color: #6e7681;
  font-style: italic;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 4px 3px;
  border-top: 1px solid #21262d;
}
```

- [ ] **Step 2: Проверить в браузере**

```bash
curl http://127.0.0.1:9090/dashboard
```

Открыть браузер → `http://smain-ip:9090/dashboard` → правая панель → убедиться что под каждым пакетом (если есть prompt_snippet) появляется серая строка с текстом промпта.

- [ ] **Step 3: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add dashboard/index.html
git commit -m "feat: show prompt_snippet in right panel packet log"
```

---

## Task 7: Dashboard — Левая панель (алгоритм + решения)

**Files:**
- Modify: `dashboard/index.html`

**Контекст:** `index.html:706-736` — `fetchRouting()` показывает статичную таблицу конфига. Нужно дополнить: (1) секция с fallback chains, (2) секция с последними N решениями из `/api/routing/decisions`.

- [ ] **Step 1: Добавить fetchDecisions и секцию в HTML**

Найти в `dashboard/index.html` HTML-разметку левой панели (блок с `id="lp-mode"` и `id="rt-table"`). Добавить после блока таблицы маршрутизации:

```html
<div class="lp-section-title" style="margin-top:10px;font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px">Последние решения</div>
<div id="rt-decisions" style="font-size:9px;color:#8b949e;">—</div>
```

Добавить JS функцию `fetchDecisions` (после `fetchRouting`):

```javascript
async function fetchDecisions() {
  try {
    const r = await fetch('/api/routing/decisions');
    const d = await r.json();
    const el = $id('rt-decisions');
    if (!d.decisions || !d.decisions.length) {
      el.innerHTML = '<span style="color:#484f58">нет данных</span>';
      return;
    }
    const CTXCOLOR = {
      default: '#8b949e', think: '#58a6ff',
      longContext: '#f97316', webSearch: '#22c55e',
      background: '#6e7681',
    };
    el.innerHTML = d.decisions.slice(0, 15).map(dec => {
      const col = CTXCOLOR[dec.context] || '#8b949e';
      const modelShort = (dec.model||'').replace('deepseek-','ds-').replace('gemini-','gm-').replace('-preview','');
      const snip = dec.snippet ? dec.snippet.slice(0,40) : '';
      const age = Math.round(Date.now()/1000 - dec.ts);
      const ageStr = age < 60 ? age+'s' : Math.round(age/60)+'m';
      return `<div style="margin:2px 0;border-left:2px solid ${col};padding-left:4px;">
        <span style="color:${col}">${dec.context}</span>
        → <span style="color:#e6edf3">${modelShort}</span>
        <span style="color:#484f58;float:right">${ageStr}</span>
        ${snip ? `<div style="color:#484f58;font-size:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${snip}</div>` : ''}
      </div>`;
    }).join('');
  } catch(e) {
    $id('rt-decisions').innerHTML = '<span style="color:#f85149">недоступно</span>';
  }
}
```

В блоке `INIT` добавить вызовы:

```javascript
fetchDecisions();
setInterval(fetchDecisions, 5000);
```

- [ ] **Step 2: Добавить fallback chains в левую панель**

В `fetchRouting()` (строка ~706) после рендера `rt-table` добавить секцию fallback chains. Добавить в HTML блок:

```html
<div class="lp-section-title" style="margin-top:8px;font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px">Fallback цепочки</div>
<div id="rt-fallbacks" style="font-size:9px;color:#6e7681;line-height:1.6">—</div>
```

В `fetchRouting()` добавить рендер fallback chains из конфига:

```javascript
    // Fallback chains (hardcoded from router.py logic, shown statically)
    const chains = {
      'default':     ['ds-flash','ds-pro','gm-flash'],
      'think':       ['ds-pro','gm-3.1-pro','gm-flash'],
      'longContext': ['gm-3.1-pro','ds-pro'],
      'webSearch':   ['gm-flash','ds-flash'],
      'background':  ['ds-flash','ds-pro','gm-flash'],
    };
    const fbEl = $id('rt-fallbacks');
    if (fbEl) {
      fbEl.innerHTML = Object.entries(chains).map(([ctx, chain]) =>
        `<div><span style="color:#8b949e">${ctx}:</span> ${chain.map((m,i) =>
          `<span style="color:${i===0?'#e6edf3':'#484f58'}">${m}</span>`
        ).join(' → ')}</div>`
      ).join('');
    }
```

- [ ] **Step 3: Проверить в браузере**

Открыть `http://smain-ip:9090/dashboard` → левая панель → убедиться что внизу появились секции "Последние решения" и "Fallback цепочки".

- [ ] **Step 4: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add dashboard/index.html
git commit -m "feat: left panel shows routing decisions ring buffer and fallback chains"
```

---

## Task 8: Dashboard — Агент Модалка (fallback на request_log)

**Files:**
- Modify: `dashboard/index.html`

**Контекст:** `index.html:750` — `openM()` запрашивает `/api/signals?agent=...`. Если агент не использует signal_client.py, результат пустой. Нужно fallback на `/api/log?source_agent=...`.

- [ ] **Step 1: Изменить openM для fallback**

В `dashboard/index.html` найти функцию `openM` (строка ~739). Заменить блок fetch:

```javascript
async function openM(key) {
  modalKey = key;
  const [agId, ndId] = key.split('@');
  const ap = agentPos[key] || {};
  $id('moverlay').classList.add('open');
  $id('m-title').textContent  = `${ap.emoji||'🤖'} ${ap.name||agId}`;
  $id('m-node').textContent   = `node: ${ndId}`;
  $id('m-model').textContent  = `model: ${ap.model||'—'}`;
  $id('m-desc').textContent   = ap.description || '—';
  $id('mhist').innerHTML      = '<li style="color:#484f58;font-size:10px">loading…</li>';
  try {
    // Primary: signals (real-time, agent-attributed)
    const sigR = await fetch(`/api/signals?agent=${encodeURIComponent(agId)}&node=${encodeURIComponent(ndId)}&limit=20`);
    const sigData = (await sigR.json()).signals || [];
    if (sigData.length) {
      renderMHist(sigData, agId);
      return;
    }
    // Fallback: request_log (reverse proxy traffic, source_agent from X-Agent-Name header)
    const logR = await fetch(`/api/log?source_agent=${encodeURIComponent(agId)}&limit=20`);
    const logData = await logR.json();
    const logRows = logData.rows || [];
    if (logRows.length) {
      renderMHistFromLog(logRows);
      return;
    }
    $id('mhist').innerHTML = '<li style="color:#484f58;font-size:10px">нет активности (сигналы не получены, агент возможно использует forward proxy)</li>';
  } catch {
    $id('mhist').innerHTML = '<li style="color:#f85149">ошибка загрузки</li>';
  }
}
```

Добавить функцию `renderMHistFromLog`:

```javascript
function renderMHistFromLog(rows) {
  const ul = $id('mhist');
  ul.innerHTML = rows.map(r => {
    const tkn = (r.tokens_in||0) + (r.tokens_out||0);
    const snippet = r.request_body
      ? r.request_body.slice(0, 60).replace(/[<>]/g,'')
      : r.llm_model || '—';
    return `<li>
      <span class="mhd" style="color:#58a6ff">↑</span>
      <span class="lb lb-prompt">prompt</span>
      <span class="mhs">${r.llm_provider||'?'}</span>
      <span class="mhk">${tkn?fmtN(tkn)+'tkn':'—'}</span>
      <span class="mhl">${r.latency_ms||'—'}ms</span>
      <span class="mha" title="${r.timestamp}">${r.llm_model||'?'}</span>
      ${snippet?`<div style="font-size:8px;color:#484f58;margin-left:12px">${snippet}</div>`:''}
    </li>`;
  }).join('');
}
```

**Важно:** Endpoint `/api/log` с параметром `source_agent` уже существует в `proxy_server.py` (строка ~213). Убедиться что он возвращает поле `rows` с нужными колонками. Если возвращает другой формат — адаптировать `renderMHistFromLog`.

- [ ] **Step 2: Проверить формат /api/log**

```bash
curl "http://127.0.0.1:9090/api/log?limit=1"
```

Посмотреть структуру ответа. Если ответ `{logs:[...]}` а не `{rows:[...]}` — в `renderMHistFromLog` заменить `logData.rows` на `logData.logs`.

- [ ] **Step 3: Проверить в браузере**

Кликнуть на агента в дашборде → модалка → убедиться что показываются либо сигналы, либо данные из request_log, либо внятное сообщение о причине отсутствия данных.

- [ ] **Step 4: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add dashboard/index.html
git commit -m "feat: agent modal fallback to request_log when no signals available"
```

---

## Task 9: Dashboard — Отчёт с фильтром периода

**Files:**
- Modify: `dashboard/index.html`

**Контекст:** `index.html:781-791` — `openReport()` вызывает `/api/report` без параметров. Нужно добавить date picker.

- [ ] **Step 1: Добавить date picker в HTML отчёта**

Найти в `dashboard/index.html` модалку отчёта (блок с `id="roverlay"`). В её шапку добавить date range inputs. Найти где открывается модалка (кнопка `openReport`) и добавить перед `r-totals`:

```html
<div id="r-period" style="display:flex;gap:8px;align-items:center;margin-bottom:10px;font-size:10px;color:#8b949e;">
  <span>Период:</span>
  <input type="date" id="r-since" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:2px 6px;border-radius:4px;font-size:10px">
  <span>—</span>
  <input type="date" id="r-until" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:2px 6px;border-radius:4px;font-size:10px">
  <button onclick="openReport()" style="background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px">Показать</button>
  <button onclick="clearReportFilter()" style="background:none;border:none;color:#484f58;cursor:pointer;font-size:10px">Сбросить</button>
</div>
```

- [ ] **Step 2: Изменить openReport для передачи дат**

В `dashboard/index.html` заменить `openReport`:

```javascript
async function openReport() {
  $id('roverlay').classList.add('open');
  $id('r-totals').innerHTML = '<div style="color:#484f58;font-size:11px;padding:8px">Загружаем…</div>';
  $id('r-tbody').innerHTML  = '';

  const sinceEl = $id('r-since');
  const untilEl = $id('r-until');
  let url = '/api/report';
  const params = new URLSearchParams();
  if (sinceEl && sinceEl.value) params.append('since', sinceEl.value + 'T00:00:00');
  if (untilEl && untilEl.value) params.append('until', untilEl.value + 'T23:59:59');
  if (params.toString()) url += '?' + params.toString();

  try {
    const r = await fetch(url);
    renderReport(await r.json());
  } catch(e) {
    $id('r-totals').innerHTML = `<div style="color:#f85149;font-size:11px">Ошибка: ${e.message}</div>`;
  }
}

function clearReportFilter() {
  const sinceEl = $id('r-since');
  const untilEl = $id('r-until');
  if (sinceEl) sinceEl.value = '';
  if (untilEl) untilEl.value = '';
  openReport();
}
```

- [ ] **Step 3: Проверить в браузере**

Открыть отчёт → установить диапазон дат → нажать "Показать" → убедиться что данные меняются при изменении диапазона.

- [ ] **Step 4: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add dashboard/index.html
git commit -m "feat: report modal date range filter with since/until params"
```

---

## Task 10: Документация

**Files:**
- Modify: `WIKI.md`

- [ ] **Step 1: Добавить Data Flow секцию в WIKI.md**

Найти `WIKI.md`. Добавить в начало файла (после заголовка) секцию:

```markdown
## Data Flow — три пути данных

```
Path A: Forward Proxy (CONNECT tunnel)
  Агент → Lineman:9090 (CONNECT) → upstream API
  Видимость Lineman: только факт соединения (host:port)
  → НИГДЕ не логируется, дашборд не видит

Path B: Reverse Proxy (/proxy/{provider}/...)
  Агент → Lineman:9090/proxy/deepseek/... → upstream API
  Видимость Lineman: полный тело запроса, заголовки, ответ
  → Записывается в request_log (с request_body, tokens, latency)
  → Авто-эмитирует signal (с from_agent если передан X-Agent-Name)
  → Дашборд видит через /api/signals + /api/log

Path C: Manual Signal SDK (signal_client.py)
  Агент → POST /api/signal → signals table
  → Дашборд видит в реальном времени
```

## Как сделать агента видимым в дашборде

Требования:
1. Агент должен роутить через reverse proxy: `http://127.0.0.1:9090/proxy/{provider}/...`
2. Агент должен добавлять заголовок: `X-Agent-Name: {agent_id}`
3. Опционально: использовать `signal_client.py` для дополнительных сигналов (tool_call, agent_msg)

Если агент использует forward proxy (CONNECT) или прямой доступ к API — он невидим в дашборде.

## Известные ограничения

- Forward proxy (CONNECT) полностью непрозрачен для Lineman
- Сигналы хранятся 24 часа, после чего удаляются
- Решения роутера (RoutingDecisionLog) — in-memory, сбрасываются при рестарте
- request_body в сигналах ограничен 300 символами (первое сообщение пользователя)
```

- [ ] **Step 2: Commit**

```bash
cd /home/shectory/workspaces/lineman
git add WIKI.md
git commit -m "docs: add data flow diagram and agent integration checklist to WIKI.md"
```

---

## Task 11: Интеграционная проверка

- [ ] **Step 1: Запустить все тесты**

```bash
cd /home/shectory/workspaces/lineman
.venv/bin/pytest tests/ -v
```

Expected: `все тесты прошли`

- [ ] **Step 2: Перезапустить Lineman**

```bash
sudo systemctl restart lineman
sudo systemctl status lineman
```

Expected: `active (running)`

- [ ] **Step 3: Проверить ключевые эндпоинты**

```bash
curl -s http://127.0.0.1:9090/api/routing/decisions | python3 -m json.tool | head -20
curl -s "http://127.0.0.1:9090/api/report?since=2026-05-01T00:00:00" | python3 -m json.tool | head -10
curl -s http://127.0.0.1:9090/api/signals | python3 -m json.tool | head -20
```

- [ ] **Step 4: Открыть дашборд и проверить все 7 пунктов**

```
1. Правая панель: видны пакеты с from_agent (если агенты шлют X-Agent-Name)
2. Правая панель: под каждым пакетом виден сниппет промпта
3. Левая панель: секция "Последние решения" обновляется каждые 5с
4. Левая панель: fallback chains видны статично
5. Отчёт: date picker работает, данные меняются при изменении диапазона
6. Клик на агента: показывает либо сигналы либо данные из request_log
7. Нет пустых панелей без объяснения причины
```

- [ ] **Step 5: Итоговый commit**

```bash
cd /home/shectory/workspaces/lineman
git add -A
git status  # убедиться что нет лишнего
git commit -m "chore: integration verified — dashboard overhaul complete"
```

---

## Checklist ответов на исходные 7 жалоб

| # | Проблема | Решение |
|---|----------|---------|
| 1 | Пакетный менеджер не работает | Task 3: агентская атрибуция через `X-Agent-Name`, auto-signal видит все /proxy/ запросы |
| 2 | Правая панель без промптов | Task 6: `prompt_snippet` в сигнале → отображение в pkt-log |
| 3 | Некорректные токены в пакете | Task 3: токены берутся из reverse proxy парсинга (не из SDK), путь B теперь основной |
| 4 | Левая панель без алгоритма | Task 7: fallback chains + decisions ring buffer из роутера |
| 5 | Отчёт без фильтра периода | Task 5: since/until в `/api/report` + date picker в UI |
| 6 | Агент-модалка пустая | Task 8: fallback на `/api/log?source_agent=...` |
| 7 | Документация недостаточна | Task 10: data flow, integration checklist, known limitations |
