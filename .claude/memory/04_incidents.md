# Журнал инцидентов Lineman

Шаблон записи:

```
## YYYY-MM-DD — Краткое название

**Симптом:** что было видно (ошибка, метрика, поведение)
**Trigger:** что предшествовало (правка, рост нагрузки, внешнее событие)
**Root cause:** настоящая причина
**Fix:** что сделали + ссылка на коммит/PR
**Lesson:** что добавить в чеклисты, тесты, мониторинг

[[ссылки на соседние файлы памяти если затронуты]]
```

## Известные инциденты (из git log и памяти Бориса)

### 2026-05-29 — PM2 vs systemd дублирование владельца Lineman

**Симптом:** `systemctl --user restart lineman` валился `OSError: [Errno 98] address already in use`, но Lineman при этом обслуживал трафик. Менялся config.json, рестарт через systemd проходил с ошибкой, изменения не применялись.
**Trigger:** Lineman управляется PM2 (`lineman-gateway` PID 488685, 30h uptime), а systemd-юнит `lineman.service` пытается стартовать тот же `main.py` — порт 9090 уже занят PM2-процессом.
**Root cause:** В проде Lineman живёт под PM2 (ecosystem с `lineman-gateway`, `lineman-censor`, `lineman-guard`, плюс `keymaster-api`, `vibe-tunnel`, `gemini-live-service`, `inbox-watcher`, `federation-inbox-poll`). systemd-unit устарел, но не был отключён.
**Fix:** В рамках сеанса 2026-05-29 остановил systemd-юнит (`systemctl --user stop lineman`) и сделал `npx pm2 restart lineman-gateway --update-env`. После этого Lineman v2 поднялся с новым `proxy_pool` (см. ниже).
**Lesson:**
- Документация (CLAUDE.md, [02_operations.md](02_operations.md)) ссылается на systemd-юнит — надо привести в соответствие с реальной PM2-инфраструктурой или решить вернуться на systemd.
- Рестарт Lineman: `npx pm2 restart lineman-gateway --update-env` (или `pm2 reload`).
- Перед стартом systemd-юнита надо `pm2 stop lineman-gateway`.
- TODO: согласовать с Борисом, отключаем ли `systemctl --user disable lineman` или мигрируем обратно на systemd.

### 2026-05-29 — Добавлен Proxy6 как secondary в proxy_pool

**Контекст:** Борис попросил добавить второй прокси на случай провала iProyal. Credential `PROXY6_CRED` (формат `ip:port:user:pass`) в keymaster (`~/.keymaster/credentials/proxy6_cred`).
**Изменения:**
- В `~/keymaster/.lineman-proxy.env` добавлена `export LINEMAN_PROXY6_URL=http://user:pass@23.236.141.49:9219`.
- `config.json`:
  - `global.proxy6_url` теперь `${LINEMAN_PROXY6_URL}` (для legacy code path в `_http_raw.py` и `healer.py` который смотрит на TG-маршрут).
  - `proxy_pool.proxies[]` дополнен записью `proxy6` (priority 2, enabled true).
  - Catch-all route `*` теперь содержит `["iproyal", "proxy6"]`.
- Бэкап: `config.json.bak-20260529-151125-pre-proxy6`.
**Smoke:** `curl -x ${LINEMAN_PROXY6_URL} https://api.ipify.org` → `23.236.141.49`. TG корень → 302 (норма). Gemini корень → 404 (норма).
**Поведение:** `ProxyPool.select` сортирует кандидатов по `(error_rate, avg_latency_ms, priority)`. Пока iproyal здоров — он работает. На per-host trip iproyal автоматом уходит на proxy6 для этого хоста. Если иproyal деградирует глобально (error_rate растёт) — proxy6 начинает обслуживать первым.
**Lesson:** При добавлении прокси не забывать смотреть на `_http_raw.py` legacy path (для CONNECT без pool) и `healer.py` (telegram alert чейн).

### 2026-05-29 — iProyal 403 CONNECT с sdev (не Lineman, но рядом)

**Симптом:** Claude CLI на sdev "потерял связь" — `403 CONNECT tunnel failed`.
**Root cause:** Egress IP sdev динамический (CGNAT: `134.255.210.31` / `2.63.176.183` чередуются). `~/scripts/vscode-proxy-sync.py` подставлял прямой `$LINEMAN_IPROYAL_URL` в `http.proxy` и `claudeCode.environmentVariables` VS Code settings вместо Lineman endpoint.
**Fix:** vscode-proxy-sync.py переписан на `http://10.66.0.1:9090`; добавлен HTTPS_PROXY-блок в `~/.profile` (не `.bashrc` — non-interactive шеллы не подхватили бы). Подробно: `~/.claude/projects/-home-shectory/memory/feedback_https_proxy_via_lineman.md`.
**Lesson:** На всех узлах federation `HTTPS_PROXY` должен указывать на Lineman (`10.66.0.1:9090`), не на upstream proxies напрямую. См. также раздел "Куда класть export" в `FEDERATION.md`.

### 2026-05-28 — Per-host circuit breaker

**Симптом:** Один деградирующий upstream блокировал весь пул прокси.
**Fix:** Коммит `3af1675 fix(pool): per-host circuit breaker for proxy failures` — теперь circuit breaker не shared между upstream'ами.
**Lesson:** При добавлении нового upstream — проверить что per-host state корректно инициализируется.

### 2026-05-28 — Telegram через iProyal даёт 502

**Симптом:** TG-сообщения не доставлялись.
**Root cause:** iProyal закрыл маршрут на `api.telegram.org` (502).
**Fix:** Коммит `0781ce9 fix: route Telegram direct (bypass IProyal — smain reaches TG natively, IProyal returns 502)` — для TG hosts `proxies: []` в `config.json`.
**Lesson:** Не все upstream API дружат с iProyal. Если внезапно 502 на конкретный host — попробовать direct.

### 2026-05-?? — Circuit breaker слишком жёсткий

**Симптом:** Нормальные LLM-запросы рубились по лимиту.
**Fix:** Коммит `0e5835c fix: raise circuit_breaker.max_calls 30→100, llm_queue concurrent 5→10, timeout 30→60s`.
**Lesson:** Текущие thresholds — компромисс. Снижать обратно — только с обоснованием через `lineman-reviewer`.

### Streaming uploads больших тел

**Симптом:** Память Lineman росла при больших uploads.
**Fix:** Коммит `ef4be6b feat(lineman): add streaming passthrough for large uploads`.
**Lesson:** Не буферизировать тело целиком на reverse-proxy пути — `iter_chunked` / passthrough.

### BATCH routing + tool schemas

**Симптом:** Ollama отвергал запросы с tool_schemas в BATCH-режиме.
**Fix:** Коммит `935d8d5 fix(lineman): skip BATCH routing for requests with tools (Ollama rejects tool schemas)`.

### WG-направления через прокси

**Симптом:** Внутренний WG-трафик (между узлами federation) уходил через iProyal.
**Fix:** Коммит `48ed119 fix(lineman): add direct route for 10.66.0.0/24 WireGuard, fix ollama-hoster URL`.

### Provider/ prefix в model

**Симптом:** model имена с префиксом провайдера (`google/gemini-2.5-flash`) → 404 апстрим.
**Fix:** Коммит `fc9228d fix(lineman): strip provider/ prefix from model names + fix Telegram health token`.

### NO_PROXY для vibe Windows

**Симптом:** VBoris2 на vibe (Windows) ходил через datacenter proxy для локальных вызовов.
**Fix:** Коммит `f7b511e fix(lineman): add NO_PROXY cmd_prefix for vibe Windows SSH calls`.

---

Когда происходит новый инцидент — добавляй сюда сразу, по шаблону вверху. **Без записи инцидент будет повторён.**
