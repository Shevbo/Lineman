# Критические пути — что нельзя ломать

Lineman это **единственный** прокси и сигнальный шлюз federation. Любая регрессия здесь — это деградация всей системы. Перед изменением одного из перечисленных — обязательно ревью через `lineman-reviewer` sub-agent и smoke-тесты до и после.

## P0 — нельзя ломать ни в коем случае

### 0. Сетевой барьер management API (`ProxyServer._is_admin_allowed`, `_path_requires_admin`)
Внесён 2026-06-17 после аудита ИБ №2 (см. [[04_incidents]]). `0.0.0.0:9090` остаётся для federation/forward-proxy, но `/api/*` (кроме явного whitelist'а), `/metrics`, `/state` доступны только из `127/8, ::1, 10.66.0.0/24, 100.64.0.0/10, 172.16/12`.

**Опасные правки:**
- Добавление нового публичного `/api/*` без явной записи в `_PUBLIC_API_PATHS` или `_PUBLIC_API_PREFIXES` (если эндпоинт имеет свою auth — добавь, иначе оставь admin-only).
- Расширение `_ADMIN_ALLOW_NETS` (`0.0.0.0/0`, `83.69.248.77`, любая публичная сеть = выкатить request_log наружу).
- Удаление dispatch-check после `request_path_only = ...` в `_raw_handler` — это единственная точка фильтра.
- Возврат `Access-Control-Allow-Origin: *` на admin-эндпоинтах (вместе с XSS на dashboard.shectory.ru = слив).
- Bind `127.0.0.1` (вместо `0.0.0.0`) — сломает WG-федерацию: агенты со sdev/hoster/vibe идут через `10.66.0.1:9090`.

**Тест:** `curl http://83.69.248.77:9090/api/log?limit=1` должен возвращать 403, а `curl http://127.0.0.1:9090/api/log?limit=1` — 200.

### 1. CONNECT-tunnel handler (`_http_raw.py`)
Через него ходят: `claude` CLI, agents с `HTTPS_PROXY`, любой клиент с прямым forward proxy. Сломать = убить интерактивную работу всех агентов.

**Опасные правки:**
- Изменение синтаксиса CONNECT-ответа (HTTP/1.1 200 …)
- Изменение upstream selection (`_select_proxy`) без учёта per-host circuit breaker
- Снижение `_CONNECT_TIMEOUT` ниже 10 секунд
- Удаление "LLM via CONNECT" safety net (это намеренная диагностика — если LLM-запрос пришёл через CONNECT, значит клиент не настроен на reverse proxy)

### 2. Proxy pool selection (`pool.py`)
- `per-host circuit breaker` (фикс из коммита `3af1675`) — один upstream-host не должен косить весь пул
- Route matching `proxy_pool.routes` — порядок имеет значение; первое совпадение выигрывает
- Direct route для `10.66.0.0/24` (коммит `48ed119`) — внутренний WG-трафик не через iProyal

### 3. Reverse proxy body parsing (`proxy_server.py`)
- Извлечение `model`, `stream`, токенов из тела запроса/ответа
- Streaming passthrough (`ef4be6b`) — большие uploads не буферизировать целиком
- Логирование тела в `request_log.request_body` — обрезать до 4096 chars, не утечь API-keys

### 4. БД и миграции (`db.py`, `signals.py`)
- Не ломать обратную совместимость со старыми записями
- WAL-режим — не переключать
- При schema-changes — `ALTER TABLE`, не `DROP`/`CREATE` поверх
- `lineman.db` — **никогда не удалять/пересоздавать**

### 5. Telegram-route (`config.json` proxy_pool.routes для `api.telegram.org`)
После коммита `0781ce9` Telegram идёт **напрямую** (bypass iProyal — iProyal возвращает 502 на TG). Не менять обратно.

## P1 — менять только с обоснованием

### Circuit breaker thresholds (`circuit_breaker.py`)
- `max_calls=100` (был 30, поднят в `0e5835c`)
- `8MB/call` ([[project_lineman]] в global memory)
- `llm_queue concurrent=10` (был 5)
- `timeout=60s` (был 30)

Снижение → могут начать резаться нормальные запросы. Повышение → можем не успевать ловить runaway.

### Router rules (`router.py`)
- Текущие: default → deepseek-flash, think → deepseek-pro, longContext → gemini-pro, background → deepseek-flash, webSearch → gemini-flash
- Изменения требуют обновления `WIKI.md` и алёрта всем агентам

### Cloudflare worker (`cloudflare-worker/`)
- `claude-connect-worker` — обход геоблока auth.anthropic.com для claude CLI OAuth
- `gemini-proxy-worker` — обход геоблока Gemini
- Деплой через `wrangler deploy` — это внешний сервис, аппрув Бориса обязателен

## Запрещено

- Удалять `lineman.db` или его WAL
- `git push --force` в master
- `git reset --hard` если есть некоммитнутые правки
- Менять credentials в `config.json → proxy_pool` без явного аппрува Бориса
- Логировать значения секретов (даже временно для debug)
- Bypass-ить keymaster API при работе с секретами

## Контрольные сигналы что что-то идёт не так

После любой правки **обязательно** прогнать через `lineman-tester`:

- `pytest` зелёный
- `/health` отвечает
- `/metrics` error_rate < 5%
- Forward proxy → iProyal IP получен
- Reverse proxy `/proxy/google` → HTTP 200
- `journalctl` без ERROR за последние 5 минут

Любой "✅ кажется работает" без этих проверок — это нарушение протокола.
