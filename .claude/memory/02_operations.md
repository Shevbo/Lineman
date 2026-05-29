# Операции Lineman — runbook

## Запуск / перезапуск (PM2, не systemd)

С 2026-05-29 Lineman живёт в PM2 как процесс `lineman-gateway` вместе с компаньонами `lineman-censor`, `lineman-guard`, `keymaster-api`, `inbox-watcher`, `vibe-tunnel`, `gemini-live-service`, `federation-inbox-poll`. Systemd-юнит `lineman.service` отключён (`systemctl --user disable lineman`), бэкап файла остался в репо для отката.

```bash
# Статус
npx pm2 list
npx pm2 describe lineman-gateway

# Старт / стоп / рестарт
npx pm2 start lineman-gateway
npx pm2 stop lineman-gateway
npx pm2 restart lineman-gateway --update-env    # подхватить новый ~/keymaster/.lineman-proxy.env
npx pm2 reload lineman-gateway                  # graceful (если возможно)

# Логи
npx pm2 logs lineman-gateway --lines 200 --nostream
tail -f ~/.pm2/logs/lineman-gateway-out.log
tail -f ~/.pm2/logs/lineman-gateway-error.log
```

**Если PM2 не доступен** (`npx pm2 not found`) — проверь `which npx` (должно быть `/usr/bin/npx`), либо запусти PM2-daemon снова через `npx pm2 resurrect`.

**Аварийный возврат на systemd** (на случай если PM2 умер): `systemctl --user enable lineman && systemctl --user start lineman` поднимет копию из `~/.config/systemd/user/lineman.service`. Перед этим `npx pm2 delete lineman-gateway`, чтобы не было EADDRINUSE.

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

- `lineman_retention.py` — daily в 03:00 (user crontab) — NULL request_body > 7d, delete rows > 90d, vacuum
- `scripts/lineman_daily_audit.py` — daily в 09:23 (user crontab) — KPI-сводка в `docs/DAILY_YYYY-MM-DD.md`, TG-алерт при P0
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
