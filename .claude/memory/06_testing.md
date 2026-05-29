# Тестирование Lineman

## pytest

```bash
# baseline (всё, тихо)
.venv/bin/python3 -m pytest tests/ -q

# конкретный модуль
.venv/bin/python3 -m pytest tests/test_router.py -v

# с покрытием (если установлен pytest-cov)
.venv/bin/python3 -m pytest tests/ --cov=. --cov-report=term-missing

# stop after first failure
.venv/bin/python3 -m pytest tests/ -x
```

### Что покрыто
- `test_router.py` — smart routing rules (default/think/longContext/webSearch)
- `test_reverse_proxy.py` — `/proxy/{provider}/...` body parsing, token counting
- `test_signals.py` — SignalQueue, 24h TTL
- `test_summarise.py` — text summarisation utility

### Что НЕ покрыто (риски)
- CONNECT-tunnel (`_http_raw.py`) — нет unit-тестов, только smoke вручную
- Per-host circuit breaker (`pool.py`) — нет тестов на edge cases (mass-fail сценарий)
- `healer.py` — нет тестов автовосстановления
- БД миграции — нет тестов schema-compat

При правке этих модулей **обязательно** smoke против живого Lineman + ручная регрессия (см. `02_operations.md`).

## Smoke против живого Lineman

Полный чеклист — в `02_operations.md` → "Smoke-тесты после рестарта". Минимум перед коммитом:

```bash
# 1. forward proxy
curl -sS -x http://127.0.0.1:9090 https://api.ipify.org
# → 86.109.80.236

# 2. reverse proxy Gemini
curl -sS "http://127.0.0.1:9090/proxy/google/v1beta/models?key=$GEMINI_API_KEY" \
  -o /dev/null -w "HTTP:%{http_code}\n"
# → HTTP:200

# 3. health
curl -sS http://127.0.0.1:9090/health | jq .status
# → "ok"

# 4. metrics
curl -sS http://127.0.0.1:9090/metrics | jq .error_rate
# → < 0.05

# 5. logs
journalctl --user -u lineman --since "5 minutes ago" | grep -iE "error|exception|traceback" | head
```

## Нагрузочное

Нет dedicated load-tests. Если нужна нагрузка — использовать `wrk`, `hey` или `vegeta` против `http://127.0.0.1:9090/health` и `/proxy/google/v1beta/models?key=...`. **Не запускать load против reverse-proxy без аппрува** — это реальные API-вызовы с реальными токенами.

## Что добавить в тесты (TODO)

- [ ] `test_pool.py` — per-host circuit breaker, route matching, direct route для WG
- [ ] `test_connect_tunnel.py` — mock CONNECT клиента, проверить релэй
- [ ] `test_healer.py` — детектирование DOWN провайдера + рестарт
- [ ] `test_db_retention.py` — `lineman_retention.py` корректно режет старые строки
- [ ] `test_dedup_cache.py` — окно, ключ хэширования, max_retries
- [ ] `test_circuit_breaker.py` — thresholds, recovery

## Конвенция для новых тестов

- Имя файла: `test_<модуль>.py`
- Использовать `pytest.fixture` из `conftest.py` (если уместно)
- Mock external HTTP — через `aioresponses` или `pytest-httpserver`
- Не лезть в реальный API в unit-тестах. Реальные API — только smoke (отдельно от pytest).
- Покрывать happy-path и хотя бы один edge case
