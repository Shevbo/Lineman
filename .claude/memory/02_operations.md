# Операции Lineman — runbook

## Запуск / перезапуск

```bash
# Статус
systemctl --user status lineman
systemctl --user is-active lineman

# Старт / стоп / рестарт
systemctl --user start lineman
systemctl --user stop lineman
systemctl --user restart lineman

# Логи
journalctl --user -u lineman -f               # follow
journalctl --user -u lineman -n 100 --no-pager
journalctl --user -u lineman --since "10 minutes ago"
```

**Если systemctl --user не работает** — проверь что `loginctl enable-linger shectory` выполнен и что service-файл лежит в `~/.config/systemd/user/lineman.service` (симлинк на `~/workspaces/infra/lineman/lineman.service` или копия).

## Health-check

```bash
curl -sS http://127.0.0.1:9090/health | jq .
# → {"status": "ok", "uptime_seconds": N, "checks": {...}}

curl -sS http://127.0.0.1:9090/metrics | jq .
# → {"error_rate": 0.01, "uptime_seconds": N, "latency_p50": ..., ...}
```

## Smoke-тесты после рестарта

```bash
# Forward proxy (через iProyal, должен дать exit-IP iProyal)
curl -sS -x http://127.0.0.1:9090 https://api.ipify.org
# → 86.109.80.236

# Reverse proxy Gemini
curl -sS "http://127.0.0.1:9090/proxy/google/v1beta/models?key=$GEMINI_API_KEY" -o /dev/null -w "HTTP:%{http_code}\n"
# → HTTP:200

# Reverse proxy DeepSeek (нужен fake-message для теста)
curl -sS http://127.0.0.1:9090/proxy/deepseek/v1/models -H "Authorization: Bearer $DEEPSEEK_API_KEY" -o /dev/null -w "HTTP:%{http_code}\n"
# → HTTP:200

# TG send (если бот настроен)
curl -sS -X POST "http://127.0.0.1:9090/api/tg/send" -H "Content-Type: application/json" -d '{"account":"default","chat_id":36910539,"text":"smoke"}'
```

## БД и retention

```bash
# Размеры
du -sh lineman.db lineman.db-wal lineman.db-shm

# Базовые счётчики
sqlite3 lineman.db "SELECT COUNT(*) AS rows FROM request_log;
                    SELECT COUNT(*) FROM signals;
                    SELECT MIN(ts), MAX(ts) FROM request_log;"

# Размер по провайдерам за сутки
sqlite3 lineman.db "SELECT llm_provider, COUNT(*), SUM(tokens_in), SUM(tokens_out)
                    FROM request_log WHERE ts > datetime('now', '-1 day')
                    GROUP BY llm_provider;"

# Retention (запускается ежедневно cron'ом lineman_retention.py)
.venv/bin/python3 lineman_retention.py    # NULL body > 7d → set; rows > 90d → delete; vacuum

# WAL truncate (если разросся)
sqlite3 lineman.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

## Бэкап

config.json делает auto-backup при изменении (`config.json.bak-YYYYMMDD-HHMMSS`). БД бэкап вручную:

```bash
# Live backup (с активной БД)
sqlite3 lineman.db ".backup /tmp/lineman.db.backup-$(date +%Y%m%d-%H%M%S)"
```

## Дашборд

`https://dashboard.shectory.ru` (Basic Auth: boris / *пароль в `.htpasswd` nginx*). nginx → reverse-proxy на 127.0.0.1:9090 (или :9094, проверить конфиг nginx).

При проблемах с SSL:
```bash
sudo nginx -t                    # синтаксис
sudo systemctl reload nginx      # перезагрузка
```

## Cron-таски, завязанные на Lineman

- `lineman_retention.py` — daily (system или user crontab)
- `lineman-hourly-check` — qaper job (см. `~/.claude/projects/-home-shectory/memory/project_qaper_cron.md`)

## Куда смотреть когда что-то идёт не так

| Симптом | Куда лезть |
|---------|-----------|
| Lineman не отвечает на :9090 | `systemctl --user status lineman` → `journalctl --user -u lineman -n 200` |
| Forward proxy → 403/CONNECT failed | `pool.py` (circuit breaker upstream), `config.json` proxy_pool credentials |
| Reverse proxy → 502 | upstream API down или `aiohttp` timeout (`_CONNECT_TIMEOUT` в `_http_raw.py`) |
| Telegram не шлёт | `notifier.py`, rate limit 15s, `TELEGRAM_BOT_TOKEN` в env |
| Метрики не обновляются | `metrics.py`, файл `metrics.json` право на запись |
| Дашборд белая страница | nginx, basic auth, /api/* endpoints |
| Растёт BD/WAL | `lineman_retention.py` не отработал — запустить руками + `wal_checkpoint(TRUNCATE)` |
| Дублирующиеся LLM-запросы | `dedup_cache.py` — окно, ключ хэширования |
