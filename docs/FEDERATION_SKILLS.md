# Каталог навыков и сервисов федерации Shectory

**Владелец:** Klod-Access (klod-foreman/Lineman/Keymaster). **Канон** — этот файл (git).
Из него генерируется шпаргалка klod-access (`KLOD_CHEATSHEET`) и (после re-auth gog) синк в
человекочитаемую Google-вики. Цель: единый ПРОВЕРЕННЫЙ список того, что у федерации реально
работает — чтобы агенты и авто-ответчик не конфабулировали «не существует» по работающим сервисам.

**Легенда статуса:** ✅ проверено вживую · ⚠️ есть, но сломано (причина) · ❓ есть, не проверено (Phase 2) · ☠️ мёртв.
**Последняя инвентаризация:** 2026-06-07 (v1, частичная верификация — Phase 2 продолжается).

> Правило против амнезии: статус ставится ТОЛЬКО после smoke-теста. «Не помню» ≠ «не существует».

---

## 1. Эндпоинты Lineman (база: `http://10.66.0.1:9090`, на smain `127.0.0.1:9090`)

| Навык | Как звать | Статус |
|---|---|---|
| Веб-поиск (keyless) | `GET /api/search?q=&limit=` → {results:[{title,url,snippet}]} | ✅ |
| YouTube-поиск (keyless) | `GET /api/youtube?q=&limit=` → {results:[{title,url,videoId}]} | ✅ |
| LLM-прокси | `/proxy/{deepseek,google,anthropic,openai,lm-studio}/...` (X-Agent-Name) | ✅ |
| LM Studio vision (бесплатно) | `POST /proxy/lm-studio/v1/chat/completions` model `qwen/qwen3.5-9b` | ✅ |
| Тикет Билдеру | `POST /api/build?repo=&from=` body=задача → klod-builder | ✅ |
| Мониторинг Билдера | `GET /api/builder/tickets` (+ дашборд `/api/builder`) | ✅ |
| Жалоба/вопрос Клоду | `POST /api/agent/klod-access/message?from=&node=`; ответ `GET .../outbox?to=&since=` | ✅ |
| Агент↔агент | `POST /api/agent/<id>/message?from=&message=` | ✅ |
| Telegram Боре | `POST /api/tg/send` {account,chat_id,text} | ✅ |
| Бюджет | `GET /api/budget` | ✅ (учёт anthropic завышен — bug, проверить) |
| Lazy Queue (локальные LLM-джобы) | `from lazy_client import submit_and_wait`; kinds vision/ocr/summarise/... | ❓ (бэкенд ollama-hoster выключен 2026-06-07, фолбэк) |
| Утечка секрета | `POST /api/keymaster/leak_alert` | ❓ |
| Вход пользователей (portal) | `POST {SHECTORY_PORTAL_URL}/api/internal/verify-portal-credentials` (см. [PORTAL_AUTH_STANDARD.md](PORTAL_AUTH_STANDARD.md)) | ✅ |

## 2. Скиллы OpenClaw (`~/skills`, `~/.openclaw/skills`, `plugin-skills`)

| Скилл | Что | Статус |
|---|---|---|
| **gog** | Google Workspace CLI (Drive/Docs/Sheets/Gmail/Calendar). `/usr/local/bin/gog`, аккаунт bshevelev75@gmail.com, env GOG_KEYRING_PASSWORD+GOG_ACCOUNT в openclaw-gateway. Вики федерации = Google Doc. | ⚠️ **OAuth-токен протух** (invalid_grant, токен от 2026-05-22; Google testing-mode ~7д). Нужен `gog auth login` (Боря) + регистрация в Keymaster Self-Refresh |
| youtube | YouTube Data API v3 — поиск/описания (отдельно от keyless /api/youtube) | ❓ |
| youtube-parse | Разбор видео — субтитры, ссылки из описания | ❓ |
| image-gen | Генерация картинок (Google Imagen 4) | ❓ |
| screenshot-reader | Анализ скриншотов/картинок | ❓ |
| voice-parser | Парсинг голосовых (.ogg) | ❓ |
| voice-profiles | TTS-голоса: create/edit/test/assign | ❓ |
| browser-automation | Управление веб-страницами через OpenClaw browser | ❓ |
| github-repo | GitHub: create/push/remote/collaborators | ❓ |
| summarize-master | Суммаризация чата/handoff | ❓ |
| billing-monitor | Балансы/подписки (Proxy6/Gemini/Cursor/OpenRouter) | ❓ |
| meeting | Общий сбор агентов | ❓ |
| submit-resume | Отправка резюме | ❓ |
| self-improving-agent | Захват learnings/ошибок | ❓ |
| task-orchestra | Оркестрация суб-агентов (в openclaw.json `entries.task-orchestra.enabled=false`) | ☠️ выключен |
| polar-accesslink / polar-link | Polar Flow (тренировки) | ❓ (Accesslink исторически проблемный, см. инциденты) |

## 3. Скрипты-хелперы (`~/scripts`)

`ask-claude.sh` (запрос к Клоду-модели; валиден, но claude-CLI зависит от подписки/лимита),
`escalate.sh` (мультиканальная эскалация: ask-claude → claude-inbox → TG; env-passing корректен),
`read-file.py`, `asr_gemini.py` (ASR), `claude_token_refresh.sh` (cron refresh claude OAuth),
`agent-inbox.py`, `federation-inbox-poll.sh`, `go2lineman.sh`, `lineman-guard.sh` (tripwire),
`claude_health_check.py`, `deploy-ssh-recovery.sh`. Статус большинства — ❓ (Phase 2).

## 4. Процесс (как держать каталог живым)

- **Discover** — свип `~/skills`+`~/.openclaw/skills`+`plugin-skills`, эндпоинты Lineman (dispatch в proxy_server.py), `~/scripts`, манифест Ключника, AGENTS.md/FEDERATION.md, Google-вики.
- **Verify** — по каждому smoke-тест → статус. Никаких статусов на память.
- **Distribute** — отсюда обновляется `KLOD_CHEATSHEET` (klod-foreman/medic/resolver.py) → klod-access отвечает агентам по факту.
- **Keep fresh** — крон периодически перепроверяет (особенно OAuth-сервисы вроде gog — токены тихо протухают).

## 5. Открытые задачи Phase 2
- Verify всех ❓ (по одному smoke-тесту).
- Re-auth gog (Боря) → синк этого каталога в Google-вики через `gog docs write`.
- Расследовать завышенный `/api/budget` (anthropic used_usd).
- Поднять lazy_queue ollama-hoster обратно (~через 3-5 дней, см. [[incident-hoster-oom-ollama]]).
- Свести дубли документации (исторически: дубли lineman/WIKI, стухшие заметки).
