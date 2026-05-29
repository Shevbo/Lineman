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
