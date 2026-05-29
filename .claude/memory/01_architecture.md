# Архитектура Lineman

## Три роли одновременно

| Роль | Endpoint | Что видит | Куда пишет |
|------|----------|-----------|------------|
| **Forward proxy** | `:9090` CONNECT-туннель | host:port + факт соединения | НИКУДА (опасный путь, невидим в dashboard) |
| **Reverse proxy** | `:9090/proxy/{provider}/...` | полное тело + заголовки + ответ + токены | `request_log` (с request_body до 4096 chars, tokens_in/out, latency) |
| **Federation monitor** | `/api/signal`, `/api/log`, `/api/signals`, `/api/nodes` | сигналы от агентов, health прoб, метрики | `signals` (24h TTL), `metrics.json` |

## Data flow

```
Path A (forward):  Агент → :9090 CONNECT → upstream-proxy (iProyal/proxy1) → API
                                                                     ↓ Lineman ничего не пишет

Path B (reverse):  Агент → :9090/proxy/google/v1beta/... → Lineman parses body
                       ↓
                   request_log + auto-signal (если X-Agent-Name задан)
                       ↓
                   upstream-proxy → api.googleapis.com

Path C (signal SDK): Агент → POST /api/signal (signal_client.py) → signals table
```

**Только Path B и C видны в dashboard.** Forward proxy непрозрачен — это by design (некоторые агенты ходят через CONNECT, например VS Code/claude CLI).

## Компоненты (файлы)

| Файл | LOC | Назначение |
|------|-----|------------|
| `main.py` | 17K | Entry point, поднимает: TCP-сервер, signal queue, health checks, healer, retention timer |
| `proxy_server.py` | 1412 | TCP-сервер :9090, диспетчер запросов, API-эндпоинты, reverse proxy logic |
| `_http_raw.py` | ~400 | CONNECT tunnel + HTTP forwarding low-level handler |
| `router.py` | 186 | Smart routing (default/think/longContext/background/webSearch) по claude-code-router |
| `pool.py` | 359 | Proxy pool, per-host circuit breaker (per upstream host) |
| `db.py` | 250 | SQLite: request_log + factory для SignalQueue |
| `signals.py` | 156 | Signal queue, 24h TTL, /api/signal[s] |
| `circuit_breaker.py` | 158 | Thresholds: 8MB/call, 100 calls, recovery window |
| `dedup_cache.py` | ~200 | Дедуп одинаковых LLM-запросов (короткое окно) |
| `healer.py` | ~180 | Авто-восстановление: рестарт upstream проб, alert в TG |
| `agents_meta.py` | 67 | Парсинг openclaw.json → `_agents_meta` (federation_id, node, workspace) |
| `mcp_server.py` | ~360 | MCP-сервер для агентов (Claude Code и др.) |
| `analytics.py` | ~150 | Aggregated stats для `/api/log/stats` |
| `metrics.py` | 163 | Latency, error rate, uptime для `/metrics` |
| `notifier.py` | ~50 | Telegram алёрты (через `/api/tg/send` локально или прямой бот) |
| `lineman_retention.py` | 50 | Daily cron: NULL body > 7d, delete rows > 90d, vacuum |

## Поддиректории

- `tests/` — pytest (test_router, test_reverse_proxy, test_signals, test_summarise)
- `checks/` — health-probes (gemini, deepseek, telegram, http_generic, google_services)
- `docs/` — внутренняя документация (superpowers/)
- `cloudflare-worker/` — claude-connect-worker (обход геоблока auth.anthropic.com), gemini-proxy-worker (workers-ai для Gemini)
- `dashboard/` — публичный SVG-дашборд (был, сейчас удалён в текущем diff — проверить статус!)

## Persistent state

| Файл | Размер | Назначение |
|------|--------|------------|
| `lineman.db` | ~440MB | SQLite — request_log, signals |
| `lineman.db-wal` | до 1GB | WAL, периодически truncate |
| `lineman.db-shm` | ~1MB | Shared memory |
| `metrics.json` | 2KB | Текущие метрики (rotating) |
| `harvester_state.json` | 248KB | Состояние harvester'а (если используется) |
| `config.json` | 9KB | Конфиг (бэкапы `.bak-YYYYMMDD-HHMMSS`) |

## Entrypoint

```bash
~/workspaces/infra/lineman/run-lineman.sh
  → source ~/keymaster/.lineman-proxy.env
  → собирает GEMINI_API_KEY, DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN из openclaw config
  → exec .venv/bin/python3 main.py
```

Управляется systemd user unit `lineman.service` → `systemctl --user {status,restart,...} lineman`.

## Federation топология

См. `~/FEDERATION.md` — таблица узлов и агентов. Кратко: smain (10.66.0.1) хостит Lineman, sdev (10.66.0.4) / hoster (10.66.0.7) / cloud (10.66.0.3) ходят через WG к нему, vibe (10.66.0.6) через SSH reverse tunnel (127.0.0.1:19090 → smain:9090).
