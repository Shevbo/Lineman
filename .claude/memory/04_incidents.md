# Журнал инцидентов Lineman

## 2026-06-27 — Klod push-channel для агентов (feature)

**Что:** двусторонняя доставка reply от Klod к агентам через push-URL (раньше только pull `/outbox`).
- `klod_inbox.set_push_url(agent, url)` / `load_push_urls()` / хранилище `~/klod-access/push_urls.json` (атомарная запись).
- `deliver_reply`: POST на зарегистрированный URL (timeout 5с, JSON payload `{from,to,id,in_reply_to,ts,message}`, header `X-Klod-Channel: push`). Нет URL / 4xx / 5xx / exception → fallback на старый GET `/api/agent/<to>/message`. Запись в outbox всегда — pull продолжает работать.
- Новые ручки в `proxy_server._raw_api_klod_access`: `POST /api/agent/klod-access/push_url?agent=&url=` (пустой `url` снимает), `GET /api/agent/klod-access/push_urls`.
- Тесты: `tests/test_klod_push.py` (6 шт). Сюита: 169 → 175 passed.
- Skill `/onboarding` (~/.claude/skills/onboarding) step 6.2 = `register_push.sh` — автоматически регистрирует push если в `.onboarding/AGENT.md` есть поле `push_endpoint:`. Канон §3.1 описывает payload-контракт.

**Зачем:** убрать ~5 мин лаг pull-режима для агентов с HTTP-сервером (qaper/selfcoder/eshkola). Pull остаётся как fallback и для bash-агентов без сервера.

**Безопасность:** валидация URL `^https?://...$`. Push идёт ИЗ Lineman НА агента — не открывает новых дыр в Lineman. Агент сам фильтрует входящие POST по `X-Klod-Channel: push`.

## 2026-06-26 — Диск smain 86%: request_log раздулся до 8.8M строк + forward-proxy абузят извне

**Симптом:** Борис: «диск кончается на smain». df: `/` 86% (57G/67G). Крупнейшие пожиратели: `lineman.db` 2.0G + `lineman.db-wal` 2.0G (high-water, не усекался), `.git` домашнего репо 3.1G (мусорные недостижимые объекты), `.trash-by-klod` 1.3G.

**Trigger:** `lineman_retention.py` имел `ROW_RETAIN_DAYS=90`. Данные с 2026-05-07 (50 дней) — порог 90д ни разу не сработал, строки копились. Ингест ~275k строк/день (тело запросов всего 15MB — раздув от ЧИСЛА строк метаданных, ~230 байт/строка × 8.8M ≈ 2GB). Спайк 2026-06-20..23 до 790k/день.

**Root cause роста:** retention 90д несовместим с реальным объёмом трафика. Главный источник объёма — внешний абуз forward-proxy: `65.108.40.195` (2.6M строк → solebox/bol/vans/mediamarkt, скрапинг ритейла), `134.195.158.62` (1.5M, 99.9% ошибок 502 → cqsqwl.com/qrb6.com/ey789.cn — спам-домены), `208.82.63.245`, `134.195.157.224`. Ни один не в node_map. Это НЕ federation (та через WG 10.66.x).

**Fix (выполнено 2026-06-26):**
- `lineman_retention.py`: `ROW_RETAIN_DAYS` 90 → 14 (БД стабилизируется ~3.8M строк). Прогнан вручную: 8.8M → 5.7M строк, db 2.0G → 1.4G после VACUUM+checkpoint. ⚠️ uncommitted на момент записи.
- `PRAGMA wal_checkpoint(TRUNCATE)` дважды — WAL 2.0G → 0. ВАЖНО: VACUUM на живой WAL-БД (днём, с активными читателями) раздувает WAL до ~1.7G; всегда делать `wal_checkpoint(TRUNCATE)` после ручного VACUUM.
- `git gc --aggressive` домашнего репо: .git 3.1G → 920K.
- Корзина `.trash-by-klod` очищена (1.3G).
- Итог: диск 86% → 75%, свободно 10G → 17G.

**Forward-proxy open-proxy абуз — ЗАКРЫТО (2026-06-26, по команде Бориса):** фикс `5378cc7` (инцидент 06-17) закрыл admin-API IP-allowlist'ом, но CONNECT-туннель и absolute-URI HTTP-проксирование оставались public. Внешние IP (5.22M строк из 5.7M = 91% БД!) абузили :9090 как open-proxy.
- `proxy_server.py`: новый `_is_forward_proxy(method, request_path)` (True для CONNECT и `http(s)://` absolute-URI). Gate в `_raw_handler` сразу после admin-gate: forward-proxy запрос И `not _is_admin_allowed(source_ip)` → drain + 403 `forward_proxy_blocked`. Доверенные сети те же `_ADMIN_ALLOW_NETS` (127/8, ::1, 10.66/24 WG, 100.64/10 TS, 172.16/12 docker). Реверс `/proxy/{provider}/*` НЕ затронут (матчится в elif выше, не доходит до gate).
- Тесты: `tests/test_forward_proxy_gate.py` (8 шт). pytest 169 зелёных.
- БД: удалены все строки с публичным source_host (5.22M), VACUUM → ~514k строк (только smain/9733/hoster/sdev/pi2), db ужата до ~0.13G.

**Lesson:**
1. retention-окно должно соответствовать ингесту, не «на глаз». При 275k/день даже 14д = ~1GB.
2. Ручной VACUUM в WAL-режиме днём раздувает WAL — обязателен последующий checkpoint(TRUNCATE). Ночной cron (3:00) чище, т.к. читателей меньше.
3. `.git` home-репо растёт недостижимыми объектами — периодический `git gc`.
4. open-proxy абуз = и ресурс, и ИБ. CONNECT без auth/allowlist — открытая дыра.

## 2026-06-17 — Аудит ИБ №2: management API торчал в публичный интернет без auth

**Симптом:** Сосед в ходе ревью указал на `ourdiary/.env` с реальными ключами. Глубокая проверка вскрыла больше: `proxy_server.host = 0.0.0.0` в config.json, ss подтверждает `LISTEN 0.0.0.0:9090`, `curl http://83.69.248.77:9090/api/log?limit=1` возвращал 200 с дампом `request_log`. То же для `/api/backlog`, `/api/registry`, `/metrics`, `/api/watchdog` и др. В логах живые подключения с публичных сканеров (170.39.193.242, 134.195.157.224). nft `inet filter input` пустая.

**Trigger:** Эволюция API за полгода (от чисто-federation до dashboard/miniapp/builder/backlog) шла без переоценки сетевой модели. CORS `Allow-Origin: *` довешен везде без чёткого разделения public/admin путей. Аудит №1 (предыдущий) пропустил, потому что смотрел только содержимое самой Lineman-репы, не покрывая сетевую поверхность.

**Root cause:** Нет архитектурного разделения «public API» (auth-эндпоинты + миниаппа) и «admin API» (управляющая поверхность). Кодовая база росла как whitelist по path в dispatcher'е, без сетевого фильтра по source_ip.

**Fix:** commit `5378cc7` — IP-allowlist на dispatcher entry. `ProxyServer._is_admin_allowed(source_ip)` + `_path_requires_admin(path)`. Allowlist: `127/8, ::1, 10.66.0.0/24 (WG), 100.64.0.0/10 (TS), 172.16/12 (docker)`. Публично оставлены `/health`, forward-proxy CONNECT, `/proxy/{provider}/*` и явный whitelist API: `/api/login`, `/api/logout`, `/api/portal-auth-check`, `/api/session-check`, `/api/tg/miniapp-auth`, `/api/gemini-pro/*`. CORS `*` → `https://voice.shectory.ru` на gemini-pro (+ `Vary: Origin`), удалён с `/api/routing*` (admin-only, same-origin).

Также в этом же commit:
- `secret_mask.py`: generic `\b[A-Za-z0-9]{30,}\b` переставлен в конец списка — стоял перед AIza-специфичным и делал его мёртвым кодом.

Smoke после restart `lineman-gateway`: `/api/log` loopback=200/public=403, `/metrics` loopback=200/public=403, `/api/backlog` loopback=200/public=403, `/api/login` public=400 (живой), `/api/gemini-pro/*` public=400 (живой), forward-proxy через iProyal=86.109.80.236, `/proxy/google/v1beta/models`=200, WG `/api/log`=200. pytest 119/119.

**Lesson:**
1. Аудит ИБ — это «сетевая поверхность × токены × CORS», не только grep по литералам в репе.
2. При каждом новом `/api/*` сразу решать: public или admin. Public требует своей auth, admin — IP+nginx-уровневой защиты.
3. `_PUBLIC_API_PATHS` и `_PUBLIC_API_PREFIXES` в `ProxyServer` — единственное место добавлять новые публичные эндпоинты. Если новый /api/* туда не попал — он автоматически admin-only.
4. См. также [[03_critical_paths]] про `_path_requires_admin`/`_is_admin_allowed` как inviolable invariant.

Парная задача — backlog `b1781682102835` (high): миграция ключей из 9 `.env` приложений в Keymaster. Сейчас файлы 600+gitignore+никогда-не-коммитились, но ротация ключа = руками в 10 файлах.

---

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

### 2026-06-01 — Federation node statuses всегда unknown, нет heartbeat

**Симптом:** dashboard `/api/nodes` показывал pi/pi2/sdev/hoster/cloud все `status=unknown`. В топологии узлы пустые, агентов не видно. KPI «понимать кто кому» нарушен.
**Root cause:** Никакого механизма heartbeat от non-local-нод не существовало. `proxy_server._raw_api_nodes` хардкодом ставил `"status": "unknown"` для всего что не smain. PM2 и cron на других узлах не запускали `signal_client.emit()`.
**Fix:**
- [scripts/lineman_heartbeat.py](../../scripts/lineman_heartbeat.py) — лёгкий скрипт, читает `~/.openclaw/openclaw.json:agents.list`, шлёт `POST /api/signal type=heartbeat` для каждого агента (или `_node` если openclaw нет).
- Развёрнуто на 4 нодах: sdev, hoster, pi, pi2. Cron `* * * * *` через `/opt/lineman_heartbeat.py`. На pi2 нет openclaw.json — шлёт только node-level heartbeat.
- `proxy_server._raw_api_nodes` теперь выводит status из `signals_list` (heartbeat за 5 мин). Поле `last_heartbeat_age_s` в ответе.
- `config.federation.decommissioned = ["cloud"]` — отметка вместо мёртвой пустой записи. _raw_api_nodes показывает `status=decommissioned`.
**vibe (Windows 10.66.0.6) — закрыто 2026-06-01:**
- На vibe есть `C:\Python314\python.exe` (3.14) и Node.js v24.13.1 (`C:\Program Files\nodejs\node.exe`).
- `lineman_heartbeat.py` скопирован через `scp vibe:lineman_heartbeat.py` в `C:\Users\Boris\lineman_heartbeat.py`.
- Зарегистрирован Windows scheduled task: `schtasks /Create /TN LinemanHeartbeat /TR "...python lineman_heartbeat.py" /SC MINUTE /MO 1 /F`.
- Два мини-фикса в скрипте: (a) заменил Unicode `→` на ASCII `->` (Windows консоль cp1251, не utf-8); (b) `_NOPROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))` — обход system HTTP_PROXY который на Windows-машинах возвращает 407 для внутренних WG-адресов.
- Smoke: `[21:31:25] node-level heartbeat -> 200`.
**Lesson:** Когда что-то называется «federation», все узлы должны emit-ить сигнал жизни. Любой `"unknown"` в /api/nodes без heartbeat-source — баг архитектуры, а не данных.

### 2026-05-29 — GitGuardian leak: .openclaw/openclaw.json в истории shectory-infra

**Симптом:** GitGuardian → email Борису: OpenClaw Auth Token + Telegram Bot Token в `Shevbo/shectory-infra` (push 15:01 UTC).
**Trigger:** Файл `.openclaw/openclaw.json` был закоммичен в коммитах `dc8d8f4 → 01019e5 → 9c4ab42 → 68ed8a4`, удалён через `git rm` в `e9c5159`. Удаление не очистило историю.
**Утекло (paths only):** 9 TG bot tokens (default/guilya/main-sdev/resume-editor/interview-coach/keymaster/titan/virtual-boris/nurse), 1 Google API key (повторён 8 раз), 1 OpenClaw gateway token. Полный inventory: `~/workspaces/infra/shectory-infra/SECURITY_INCIDENT_2026-05-29.md`.
**Fix (мой):**
- `.gitignore` уже содержит `openclaw.json` — новых утечек не будет.
- В Lineman runtime: модуль [secret_mask.py](../../secret_mask.py), маскирование в [reverse_proxy.py:812](../../reverse_proxy.py) (request_body) и [proxy_server.py:490](../../proxy_server.py) (`/api/log` endpoint). 11 unit-тестов в `tests/test_secret_mask.py`.
- Ретроактивно замаскировано 4457 строк в `request_log` (где было `api_key`/`sk-`/`Bearer`/`AIza`).
- В `shectory-infra/scripts/`: `scrub_history.sh` (git-filter-repo + force-with-lease push, ждёт ротации) и `install_secret_guard.sh` (pre-commit + pre-push гарды). Уже установлено в `.git/hooks/`.
**Что должен сделать Борис вручную:** ротация 9 TG bot tokens через BotFather, regenerate Google API key, новый OpenClaw gateway token, обновить keymaster и openclaw config. Только потом — команда мне «зачищай историю».
**Lesson:**
- Никогда не коммитить openclaw.json / .lineman-proxy.env / auth-profiles.json даже временно для синка.
- `git rm` не очищает историю — нужен `git filter-repo`.
- Daily audit ([scripts/lineman_daily_audit.py](../../scripts/lineman_daily_audit.py)) уже ловит leak-count > 0.

### 2026-05-29 — ollama-hoster полностью не работал (config + no models)

**Симптом:** За 14 дней 143/144 запроса к ollama-hoster — ошибки 403/400/502. KPI «ollama для простых» не выполняется.
**Root cause (два независимых бага):**
1. `reverse_proxy.upstreams.ollama-hoster = "http://10.66.0.7:11434/v1"` — двойной /v1 при склейке с rest_path `/v1/...` давало `/v1/v1/...` → 404.
2. На hoster (10.66.0.7) Ollama жив, но `models: []` — ни одна модель не подгружена.
**Fix:** В [config.json](../../config.json) `reverse_proxy.upstreams.ollama-hoster` → `http://10.66.0.7:11434` (без /v1). После рестарта `/proxy/ollama-hoster/v1/models` отвечает 200.
**Что нужно от Бориса:** на hoster `ssh ... 'ollama pull llama3.2:3b && ollama pull nomic-embed-text'` или указать другие модели — добавить в [router.py](../../router.py) FALLBACK_CHAINS.BATCH соответствующее имя.
**Lesson:** При прописывании upstream URL в Lineman config — не дублировать prefix который агенты сами шлют. Health-probe не ловил это, потому что probe.health_endpoint = `/api/tags` (а не `/v1/...`).

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
