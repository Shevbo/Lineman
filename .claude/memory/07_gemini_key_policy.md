# Gemini API key — политика, ограничения, как избежать блокировок

Источник: <https://ai.google.dev/gemini-api/docs/api-key>

## Что Google поменял в 2026

| Дата | Изменение |
|------|-----------|
| 2026-05-07 | **Dormant blocker**: неактивные unrestricted-ключи получают `Blocked` тэг. Нужен periodic call. |
| 2026-06-19 | **Unrestricted keys полностью перестают работать.** Только restricted (API + Application restrictions). |
| 2026-05 (наш период) | Новые ключи имеют формат `AQ.A…` (длина 53), привязаны к service account. Старые `AIza…` (39 символов) — постепенно депрекейтятся. |

## Текущее состояние нашего ключа (после ротации 2026-05-29)

- Префикс: `AQ.A...` (новый формат, привязан к SA).
- Где живёт: `~/.openclaw/openclaw.json → models.providers.google.apiKey` + 9 копий в `messages.tts.personas.*` + `talk.providers.google.apiKey` + `~/keymaster/.lineman-proxy.env → GEMINI_API_KEY` + `~/.keymaster/credentials/gemini_api_key`.
- Egress IPs (откуда уходят запросы):
  - `86.109.80.236` — iProyal ISP Dedicated
  - `23.236.141.49` — Proxy6
- Также через CF Worker `gemini-proxy-worker.bshevelev75.workers.dev` (тогда egress — Cloudflare anycast).

## Обязательная конфигурация в GCP (зона ответственности Бориса)

В Cloud Console → APIs & Services → Credentials → ключ префикса `AQ.A`:

1. **API restrictions** → `Restrict key` → `Generative Language API`.
2. **Application restrictions** → один из:
   - **IP addresses** (рекомендую): `86.109.80.236`, `23.236.141.49` (+ IP Cloudflare worker если используется).
   - **HTTP referrers**: `*.bshevelev75.workers.dev` для CF worker маршрута.

Без этих двух — ключ будет автоматически заблокирован после 19 июня 2026.

## Что Lineman делает чтобы предупреждать блокировки

1. **Daily keep-alive Gemini probe** в [scripts/lineman_daily_audit.py](../../scripts/lineman_daily_audit.py): принудительный запрос к `/v1beta/models` — предотвращает dormant-блок.
2. **Алерт при 403 подряд**: в [scripts/lineman_daily_audit.py](../../scripts/lineman_daily_audit.py) счётчик `geoblock_403` per day; если ≥ 3 за день — TG-сообщение Борису со ссылкой на GCP.
3. **Egress IP мониторинг**: ежедневно `curl -x ${proxy} ipify.org` — сверяет с ожидаемыми. Если изменился → алерт «обнови allowlist в GCP».
4. **Маскирование при логировании**: [secret_mask.py](../../secret_mask.py) ловит и `AIza...` и `AQ....` префиксы.

## Что НЕ делать

- Не класть Google key в env-переменные процессов на которых не контролируется egress (например, любой агент через CONNECT-туннель — egress = тот upstream-проксер, его IP может меняться).
- Не использовать один ключ для нескольких проектов: с июня 2026 service-account привязка усиливается.
- Не вызывать Google API напрямую из vibe (Windows) — там egress совершенно другой. Все Google-запросы должны идти через Lineman (`/proxy/google/...`).
