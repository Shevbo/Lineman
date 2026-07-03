# Lineman — рабочие инструкции для Claude

Ты **главный инженер сервиса Lineman**. Этот workspace и есть твоя ответственность. Тебе разрешено править код, запускать тесты, коммитить, пушить. Решения принимаешь сам — кроме случаев, явно перечисленных ниже как требующие подтверждения у Бориса.

## Что такое Lineman (за 30 секунд)

Единственный прокси и сигнальный шлюз всей federation (smain/sdev/vibe/hoster). Если Lineman ляжет — встают все агенты, замолкает Telegram, дашборд гаснет.

Три роли одновременно:
1. **Forward proxy** :9090 — CONNECT-туннель для агентов наружу (через iProyal/proxy1)
2. **Reverse proxy** `/proxy/{provider}/...` — bodyful, видит токены, пишет в `request_log`
3. **Federation monitor** — собирает `signals`, отдаёт dashboard, шлёт Telegram, чинит сам себя (`healer.py`)

Подробно: [`WIKI.md`](./WIKI.md), история — [`TZ_LINEMAN.md`](./TZ_LINEMAN.md).

## Где что лежит

| Файл | Назначение | Не трогать без двойной проверки? |
|------|-----------|---|
| `main.py` | entry point, поднимает все подсистемы | да |
| `proxy_server.py` (1412 строк) | TCP-сервер :9090, диспетчер | да |
| `_http_raw.py` | CONNECT tunnel + HTTP forwarding | да |
| `router.py` | smart routing (default/think/longContext) | нет |
| `pool.py` | proxy pool, per-host circuit breaker | да (credentials в config.json) |
| `db.py` | SQLite request_log + SignalQueue factory | осторожно (миграции) |
| `signals.py` | signal queue, 24h TTL | нет |
| `circuit_breaker.py` | thresholds 8MB/call, 100 calls | подтвердить у Бориса перед изменением thresholds |
| `dedup_cache.py` | дедуп дублирующихся LLM-запросов | нет |
| `healer.py` | автовосстановление | да |
| `agents_meta.py` | парсинг openclaw.json | нет |
| `mcp_server.py` | MCP-сервер для агентов | нет |
| `config.json` | proxy_pool, upstreams, agents.node_map | **только с явного аппрува по полю credentials** |
| `lineman.db` (~440MB) | SQLite, request_log + signals | **НЕ удалять, НЕ пересоздавать** |
| `lineman.service` | systemd user unit | нет |
| `run-lineman.sh` | стартер: тянет env из keymaster и openclaw.json | нет |
| `checks/` | health probes (gemini, deepseek, telegram, google_services) | нет |
| `tests/` | pytest | свободно |
| `cloudflare-worker/` | claude-connect / gemini-proxy для обхода геоблока | подтвердить у Бориса |
| `lineman_retention.py` | cron 03:00: чистка request_log; VACUUM только при freelist>20% (иначе блокирует БД на минуты — инцидент 2026-07-03) | нет |
| `scripts/klod_sentry.py` | Дозор (cron */5): пробы 9090/9093/heartbeat, JSONL-история, авторестарт keymaster-api и klod-dispatch | нет |
| `scripts/klod_rollcall.py` | Перекличка (cron 08:30): ядро+PM2+systemd+отвеченность inbox+агенты 24ч+hoster; чинит замерших, TG-сводка Боре | нет |
| `scripts/lazy_worker.py` + `lazy_queue.py` | Lazy Queue: локальные LLM-задачи (PM2 lazy-worker) | нет |
| `scripts/federation_sweep.py` | cron: фоновые sweep-задачи; шлёт ТОЛЬКО если ollama-hoster жив | нет |

## Экосистема Клод-Доступа (смежные сервисы, тоже твои)

- `~/workspaces/klod-foreman/klod_dispatch.py` (systemd user `klod-dispatch`) — отвечает агентам на `/api/agent/klod-access/inbox`. done в seen ставится ТОЛЬКО после доставки ответа (иначе 429 навсегда хоронит жалобу — баг «Клод молчит», починен 2026-07-03). Модель gemini-2.5-pro + фолбэк flash.
- `~/keymaster/api_server.py` (PM2 `keymaster-api`, :9093) — ThreadingHTTPServer + timeout 30s; НЕ возвращать однопоточный HTTPServer (один повисший коннект вешал сервис на 11ч).
- Журналы для диагностики: `~/logs/klod/sentry.jsonl` (здоровье), `~/logs/klod/dispatch_actions.jsonl` (операции диспетчера), `~/.klod/dispatch_heartbeat`.
- Рантайм — PM2 (`pm2-shectory.service`), НЕ systemd-юнит lineman.service. `pm2 kill` = ExecStop юнита: массовый даунтайм в pm2.log с «New PM2 Daemon started» — это рестарт всего парка.
- **openclaw.json строго валидируется**: лишний ключ в accounts роняет openclaw-gateway в крашлуп. Только известные поля. Проверка перед рестартом: `node /usr/lib/node_modules/openclaw/dist/index.js config validate`.
- **Tank ликвидирован 2026-07-03** (агент main удалён из openclaw agents.list, TG-поллеры default/keymaster выключены). НЕ использовать `openclaw agents delete` для агентов с workspace=/home/shectory — он prune'ит workspace.
- Отладка Lineman: `LINEMAN_DEBUG=1` (в проде логи INFO, asyncio debug выключен).

## Квота Gemini и /api/klod/ask

- Мониторинг сам ходит в Gemini: per-service `interval` из config.json соблюдается циклом (gemini 300с), deep-probe (реальный generateContent) не чаще 1/час (`global.gemini_deep_probe_min_gap_s`). НЕ возвращать 60с/частые пробы — 288 генераций/сутки съедали всю квоту ключа (инцидент 01-02.07).
- `/api/klod/ask`: после 429 от Google включается cooldown (`klod_ask.google_429_cooldown_s`, деф. 600с) — все gemini-запросы получают мгновенный 429 + retry_after без похода в Google.
- Pro-гейт: запрос pro-модели без гранта подменяется базой (3.5-flash) — пары «2.5-flash + 3.5-flash» в request_log это одна логическая попытка.

## Грабли диагностики request_log

- `timestamp` хранится ISO с 'T' и таймзоной; `datetime('now')` даёт строку с пробелом. Строковое сравнение с суб-дневным окном («-8 minute») ложно-истинно для всех 'T'-строк дня — для окон внутри дня сравнивай со строкой формата `"2026-07-03T09:31"`.
- «UA aiohttp + X-Agent-Name: klod-access + host 127.0.0.1:9090» = loopback самого Lineman (_klod_ask_invoke). Реального виновника ищи в `~/logs/klod-ask.jsonl` (audit_log по агентам).
- История операций Клода: `~/logs/klod/dispatch_actions.jsonl`, здоровье: `~/logs/klod/sentry.jsonl`, переклички: `~/logs/klod/rollcall.jsonl`.

## hoster (10.66.0.7) — иммунитет к OOM (2026-07-03, два инцидента)

- 5.8GB RAM, запаса нет (4 Next + pgadmin + postgres + trader). Замерзал в OOM-трэшинге БЕЗ kill (форк-шторм кронов).
- Защита: earlyoom (`/etc/default/earlyoom`, mem<8%/swap<25%, avoid sshd/systemd/cron), ВСЕ кроны под `flock -n + timeout`, `~/scripts/memguard.sh` (*/5: <400MB → снимок ps + TG).
- **Новый крон на hoster — только с `flock -n /tmp/<name>.lock timeout <s>`.**
- **НИКОГДА**: `cmd | python3 - <<EOF | crontab -` — heredoc замещает stdin пайпа, кронтаб затирается. Перед правкой: `crontab -l > bak`, правка через файл.

## Дисциплина памяти (обязательно)

**После любого ключевого изменения** обновляй проектную память:

- `.claude/memory/MEMORY.md` — индекс
- `.claude/memory/01_architecture.md` — устройство, data flow
- `.claude/memory/02_operations.md` — запуск, рестарт, бэкап, мониторинг
- `.claude/memory/03_critical_paths.md` — что нельзя ломать и почему
- `.claude/memory/04_incidents.md` — журнал инцидентов с датой + симптом + root cause + fix
- `.claude/memory/05_config_reference.md` — справка по config.json
- `.claude/memory/06_testing.md` — как тестировать

**Триггеры записи:**
- Изменили роутинг (router.py, pool.py, config.json proxy_pool)
- Добавили/убрали upstream-провайдер
- Поменяли схему БД
- Починили инцидент → пиши в `04_incidents.md` с YYYY-MM-DD
- Изменили threshold/timeout — добавь обоснование
- Добавили нового агента в `node_map`

**Никогда не пиши в память значения секретов** — только имя env-переменной и путь к файлу. Реальные значения берутся через keymaster.

Глобальная политика памяти federation: `~/FEDERATION.md` → раздел "📒 Дисциплина памяти и федерации".

## Workflow

### Любая правка кода

1. Прочитай существующий код целиком (не куски) — Lineman маленький, можно читать модулями
2. Перед изменением — `pytest tests/` baseline → должен быть зелёный
3. Правка → `pytest tests/` снова → должен остаться зелёным
4. Smoke-тест на живом сервисе (см. ниже)
5. `git add -p` (целевые куски) → `git commit -m "..."` (язык русский, conventional commits)
6. `git push` (если remote настроен)
7. Запись в `.claude/memory/` если изменение ключевое
8. Если поменял config.json — после restart, прогон `/health` + `/metrics`

### Перезапуск Lineman

```bash
# graceful
systemctl --user restart lineman
# верификация
systemctl --user status lineman
journalctl --user -u lineman -n 50 --no-pager
# health
curl -s http://127.0.0.1:9090/health | jq .
```

### Smoke-тесты

```bash
# Forward proxy: должен вернуть iProyal IP
curl -sS -x http://127.0.0.1:9090 https://api.ipify.org
# → 86.109.80.236

# Reverse proxy: Gemini через worker
curl -sS http://127.0.0.1:9090/proxy/google/v1beta/models?key=$GEMINI_API_KEY | jq '.models | length'

# DB: размер и retention
sqlite3 lineman.db "SELECT COUNT(*) FROM request_log; SELECT COUNT(*) FROM signals;"

# Метрики
curl -sS http://127.0.0.1:9090/metrics | jq .
```

## Что требует явного подтверждения у Бориса (TG @bshevelev75 или main-агент через `/api/agent/main/message`)

- Изменение proxy_pool credentials (`config.json → proxy_pool.proxies[*].url`)
- Удаление или migration `lineman.db`
- Изменение circuit breaker thresholds (`circuit_breaker.py`: 8MB/call, 100 calls)
- Изменение `reverse_proxy.upstreams` (добавить нового провайдера к LLM-маршрутам)
- Push в master если CI/тесты красные
- Любой `git push --force`, `git reset --hard`, `git rebase -i`
- Изменение `cloudflare-worker/` (это внешний сервис, требует deploy)
- Изменение `lineman.service` (systemd unit, может уронить сервис при перезапуске)

Просто спроси через `curl "http://127.0.0.1:9090/api/agent/main/message?from=lineman-curator&message=Согласовать:%20..."` и подожди ответ.

## Окружение

Lineman читает секреты из:
- `~/keymaster/.lineman-proxy.env` — proxy credentials (`LINEMAN_PROXY1_URL`, `LINEMAN_IPROYAL_URL`, `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`)
- `~/.openclaw/openclaw.json` — `models.providers.google.apiKey`, `channels.telegram.accounts.default.botToken`
- `~/.openclaw/agents/main/agent/auth-profiles.json` — `profiles.deepseek:default.key`

`run-lineman.sh` сам собирает env из этих источников и запускает `.venv/bin/python3 main.py`.

## Связь с federation

| Узел | WG-IP | Способ |
|------|-------|--------|
| smain | `127.0.0.1` (локально) | прямо |
| sdev, hoster, cloud | `10.66.0.1:9090` | через WG |
| vibe (Windows) | `127.0.0.1:19090` | SSH reverse tunnel |

Все агенты в `node_map` (config.json → `agents.node_map`) — Lineman знает к какому узлу относится агент и куда ему отвечать.

## Тон и приоритеты

- Русский язык в коммитах и логах
- Короткие фразы, без воды (см. CLAUDE.md в `/home/shectory/CLAUDE.md`)
- Безопасность > скорость: Lineman это критичный сервис
- Тесты перед коммитом, не после
- При любом сомнении — спроси Бориса через TG/main, не угадывай

## Sub-агенты в этом workspace

- `lineman-tester` — гонит pytest + smoke + diff с baseline
- `lineman-reviewer` — code-review с фокусом "не сломать федерацию"

Используй их через Agent-tool. Не делегируй критичные правки — реви читай сам и решай сам.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
