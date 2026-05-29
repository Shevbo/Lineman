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
