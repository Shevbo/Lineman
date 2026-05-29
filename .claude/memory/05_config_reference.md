# config.json — справка

Auto-backup при изменении: `config.json.bak-YYYYMMDD-HHMMSS`. Перед правкой — взгляни на последний bak, чтобы понимать diff.

## Структура верхнего уровня

```
services[]          health-probes (lm-studio, ollama-hoster, deepseek-flash/pro, gemini-flash/pro,
                    google-drive/gmail/calendar, telegram)
routing             smart-routing rules (default/think/background/longContext/webSearch/local/batch)
proxy_server        host, port, max_connections, request_timeout, llm_queue limits
analytics           claude_logs_path, enabled
rtk                 RTK binary path, enabled
global              интервалы health checks, gemini_cf_proxy_url, state_file, metrics_file
pricing             prices per model (для analytics/dashboard)
dedup_cache         ttl/max_entries/window/max_retries
circuit_breaker     window/max_calls/max_bytes_per_call/max_bytes_window
reverse_proxy       upstreams: {provider: url}
agents.node_map     какие агенты на каком узле
federation          local_node + node_agents{} (для dashboard)
proxy_pool          proxies + routes + host_circuit_breaker
```

## Ключевые значения (актуально на 2026-05-29)

### proxy_server
- `host: 0.0.0.0` (доступен снаружи WG — нужно для sdev/hoster через 10.66.0.1:9090)
- `port: 9090`
- `max_connections: 100`
- `request_timeout: 120`
- `llm_queue.max_concurrent: 10` (для api.deepseek.com и generativelanguage.googleapis.com)
- `llm_queue.queue_timeout_s: 60`

### circuit_breaker
- `window_secs: 60`
- `max_calls: 100`     (был 30, поднят `0e5835c`)
- `max_bytes_per_call: 8_000_000`
- `max_bytes_window: 40_000_000`
- `alert_cooldown_secs: 120`

### dedup_cache
- `ttl_secs: 30`, `max_entries: 200`, `window_secs: 60`, `max_retries: 3`

### proxy_pool
- proxies: только `iproyal` (`${LINEMAN_IPROYAL_URL}` из env). proxy1 был раньше, сейчас удалён из config.
- routes:
  - `10.66.0.0/24` + `127.0.0.1` + `localhost` → **direct** (proxies: [])
  - `*` → iproyal
- host_circuit_breaker: `window=300s`, `error_threshold=10`, `recovery=1800s`

### reverse_proxy.upstreams
- `lm-studio: http://127.0.0.1:1234`
- `ollama-hoster: http://10.66.0.7:11434/v1`
- `deepseek: https://api.deepseek.com`
- `google: https://generativelanguage.googleapis.com`

**ВНИМАНИЕ:** `anthropic` НЕ зарегистрирован. `/proxy/anthropic/*` вернёт 400 "Unknown provider". Если кому-то понадобится reverse-proxy для Anthropic с метриками — добавлять upstream + маршрут в `proxy_pool.routes` (api.anthropic.com через что? через iProyal или через `cloudflare-worker/claude-connect-worker`?). До этого момента — claude CLI ходит через CONNECT (forward proxy путь A, без логирования).

### routing
- `default`: deepseek-flash
- `think`: deepseek-pro
- `background`: deepseek-flash
- `longContext` (>60K токенов): gemini-3.1-pro-preview
- `webSearch`: gemini-2.5-flash
- `local`: lm-studio (gemma-4-e4b)
- `batch`: ollama-hoster (llama3.2:3b)

### agents.node_map
Только смайн: main, selfcoder, qaper, virtual-boris, titan, nurse, guilya, jobsearch-scanner, resume-editor, interview-coach, inbox

### federation.node_agents
- `sdev`: main (TankDev 🛠️), selfcoder ⚡, qaper 🔍
- `hoster`: main (Hoster 🏠), shopin 🛒, inbox 📥
- `vibe`: virtual-boris (VBoris2 🧠), inbox 📥
- `cloud`, `pi`, `pi2`: пусто

### pricing
Источник цен — `docs/llm-pricing-2026-05.md`. Обновлено 2026-05-17. Скидка 75% на DeepSeek до 31.05.2026.

## Что меняется чаще всего

- `agents.node_map` / `federation.node_agents` — при добавлении нового агента/узла
- `routing` — при изменении модели по умолчанию
- `reverse_proxy.upstreams` — при добавлении нового LLM-провайдера в reverse-mode
- `proxy_pool.routes` — при добавлении исключений (например, "этот host напрямую")

## Что меняется ОЧЕНЬ редко (с двойной проверкой)

- `proxy_pool.proxies[*].url` — credentials, требуют аппрува Бориса и ротации env
- `circuit_breaker.max_*` — могут начать резаться нормальные запросы
- `proxy_server.host/port` — потребует обновления клиентов и systemd unit

## Hot reload?

Lineman читает `config.json` при старте. **Если в коде нет логики watch-reload — нужен `systemctl --user restart lineman`** после изменений. Проверь `main.py` если не уверен.
