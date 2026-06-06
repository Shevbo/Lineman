# Lineman — Вики

**Версия:** 2026-05-13  
**Расположение:** smain, `/home/shectory/workspaces/lineman/`  
**Процесс:** `python3 main.py` (запускается вручную или через systemd user unit)  
**Порт:** `127.0.0.1:9090`  
**Публичный дашборд:** [https://dashboard.shectory.ru](https://dashboard.shectory.ru) (вход по единой учётке **Shectory Portal**: username = **email** портала, напр. `bshevelev@mail.ru`; не `boris`)

---

## Data Flow — три пути данных

```
Path A: Forward Proxy (CONNECT tunnel)
  Агент → Lineman:9090 (HTTP CONNECT) → upstream API
  Lineman видит: только факт соединения (host:port), не тело
  Логируется: НИГДЕ
  Дашборд: не видит этот трафик

Path B: Reverse Proxy (/proxy/{provider}/...)
  Агент → Lineman:9090/proxy/deepseek/... → upstream API
  Lineman видит: полный тело запроса, заголовки, ответ, токены
  Логируется: request_log (с request_body, tokens_in/out, latency)
  Авто-сигнал: эмитируется в signals (с from_agent если X-Agent-Name задан)
  Дашборд: видит через /api/signals + /api/log

Path C: Manual Signal SDK (signal_client.py)
  Агент → POST /api/signal → signals table
  Дашборд: видит в реальном времени
```

**Вывод:** Дашборд видит только Path B и Path C трафик. Path A невидим.

---

## Как сделать агента видимым в дашборде

Минимальные требования:
1. Агент роутит через reverse proxy: `http://127.0.0.1:9090/proxy/{provider}/...`
2. Агент добавляет заголовок: `X-Agent-Name: {agent_id}` (иначе виден как "smain", без имени)

Опционально:
3. Использовать `signal_client.py` для дополнительных сигналов (tool_call, agent_msg, success)

Если агент использует forward proxy (CONNECT) или прямой доступ к API — он невидим в дашборде.

---

## Известные ограничения

- **Forward proxy (CONNECT)** — полностью непрозрачен, трафик не логируется
- **Сигналы живут 24 часа** — после TTL удаляются; агентская история ограничена
- **RoutingDecisionLog** — in-memory deque(50), сбрасывается при рестарте Lineman
- **prompt_snippet** — первые 300 символов последнего user-сообщения; не весь промпт
- **request_body в request_log** — обрезается до 4096 символов

---

## Что такое Lineman

Lineman — умный API-шлюз и монитор федерации агентов. Работает на smain, слушает на `127.0.0.1:9090`.

Три роли одновременно:
1. **Forward proxy** — OpenClaw и агенты ходят через него к LLM API (DeepSeek, Gemini, Anthropic)
2. **Reverse proxy** — принимает plaintext HTTP `/proxy/{provider}/...`, видит тело запроса/ответа, считает токены в реальном времени
3. **Federation monitor** — проверяет здоровье сервисов, накапливает сигналы от агентов, отдаёт дашборд

---

## Файловая структура

```
lineman/
├── main.py              # точка входа, запускает все подсистемы
├── proxy_server.py      # TCP-сервер :9090, диспетчер запросов + API
├── reverse_proxy.py     # /proxy/{provider}/... — bodyful forwarding
├── router.py            # умный роутинг моделей (default/think/longContext/...)
├── db.py                # SQLite: request_log + фабрика SignalQueue
├── signals.py           # сигнальная очередь (table signals, 24h TTL)
├── agents_meta.py       # парсинг openclaw.json → метаданные агентов
├── signal_client.py     # SDK: fire-and-forget emit() для агентов
├── analytics.py         # аналитика токенов из ~/.claude/projects/
├── pool.py              # пул прокси-серверов с routing rules
├── checks/              # health-check модули (deepseek, gemini, telegram, google)
├── healer.py            # авторемонт сервисов
├── notifier.py          # уведомления о смене состояния
├── reporter.py          # ежедневный отчёт в Google Doc
├── metrics.py           # метрики (EWMA latency, error rate)
├── token_harvester.py   # фоновый импорт токенов из ccusage-логов
├── config.json          # вся конфигурация (см. ниже)
├── lineman.db           # SQLite (request_log + signals)
├── state.json           # текущее состояние сервисов
├── metrics.json         # накопленные метрики
└── dashboard/
    └── index.html       # дашборд (SVG-топология + анимация пакетов)
```

---

## Режимы работы прокси

### 1. Forward proxy (CONNECT tunnel)

Классический HTTP-прокси. Клиент отправляет `CONNECT host:443`, Lineman пробрасывает TLS-туннель.  
**Минус:** видит только зашифрованный трафик, токены не считаются.

### 2. Reverse proxy (baseUrl mode) ← активный режим

OpenClaw настроен отправлять plaintext HTTP напрямую:

```
openclaw.json:
  models.providers.deepseek.baseUrl = "http://127.0.0.1:9090/proxy/deepseek"
  models.providers.google.baseUrl   = "http://127.0.0.1:9090/proxy/google"
```

Lineman получает тело запроса в открытом виде → извлекает `model`, `stream`, токены из ответа → пишет в `request_log` и `signals`.

**URL-формат:** `/proxy/{provider}/{rest...}`  
**Поддерживаемые провайдеры:**

| provider | upstream |
|----------|----------|
| `deepseek` | `https://api.deepseek.com` |
| `google` | `https://gemini-proxy-worker.bshevelev75.workers.dev` |
| `anthropic` | `https://api.anthropic.com` |
| `openai` | `https://api.openai.com` |

Настраивается в `config.json → reverse_proxy.upstreams`.

---

## API-эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Статус всех сервисов + uptime |
| GET | `/metrics` | Latency, error rate, uptime |
| GET | `/state` | Полный state.json |
| GET | `/api/log` | Лог запросов (фильтры: since, until, source_host, llm_provider, llm_model, limit) |
| GET | `/api/log/stats` | Агрегированная статистика запросов |
| POST | `/api/log` | Ручной insert строки в request_log |
| GET | `/api/signals` | Сигналы (фильтры: since, limit, agent, node) |
| POST | `/api/signal` | Ручной сигнал от агента (используется signal_client.py) |
| GET | `/api/nodes` | Топология федерации: ноды + агенты + сервисы + статистика |
| GET | `/api/pool/stats` | Статистика прокси-пула |
| GET | `/api/pool/hitparade` | Топ использования прокси |
| GET | `/dashboard` | HTML дашборд |
| `ANY` | `/proxy/{provider}/...` | Reverse proxy к LLM API |
| POST | `/rtk` | Запуск dev-команды через RTK |

### Примеры

```bash
# Последние 5 сигналов
curl http://127.0.0.1:9090/api/signals?limit=5

# Сигналы от агента main за последние 60 секунд
curl "http://127.0.0.1:9090/api/signals?agent=main&since=$(date -d '-60 seconds' +%s)"

# Топология федерации
curl http://127.0.0.1:9090/api/nodes | python3 -m json.tool

# Статистика токенов
curl http://127.0.0.1:9090/api/log/stats
```

---

## База данных (lineman.db)

### Таблица `request_log`

Все запросы через Lineman (reverse proxy режим + ручные POST /api/log).

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | INTEGER | autoincrement |
| timestamp | TEXT | ISO UTC |
| source_host | TEXT | "smain", "sdev", ... (из WG IP) |
| source_agent | TEXT | имя агента (если передан) |
| llm_provider | TEXT | "deepseek", "gemini", ... |
| llm_model | TEXT | "deepseek-v4-flash", ... |
| target_url | TEXT | полный upstream URL |
| tokens_in / tokens_out | INTEGER | из тела ответа |
| cache_hit | INTEGER | 1 если cache_read_input_tokens > 0 |
| latency_ms | INTEGER | время ответа |
| status_code | INTEGER | HTTP статус upstream |
| error | TEXT | текст ошибки или NULL |

### Таблица `signals`

Сигналы от агентов для дашборда. TTL = 24 часа (старые удаляются при каждом insert).

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | INTEGER | autoincrement |
| ts | REAL | unix timestamp |
| from_agent | TEXT | "main", "selfcoder", null |
| from_node | TEXT | "smain", "sdev", ... |
| to_service | TEXT | "deepseek", "gemini", "telegram", ... |
| type | TEXT | `prompt` / `response` / `tool_call` / `error` / `success` / `document` / `image` / `message` |
| model | TEXT | модель или null |
| tokens_in / tokens_out | INTEGER | токены |
| latency_ms | INTEGER | задержка |
| status | TEXT | "ok" / "error" |

**Автоматические сигналы:** каждый запрос через `/proxy/` автоматически создаёт сигнал.

**Ручные сигналы:** через SDK (signal_client.py) или POST /api/signal.

---

## SDK для агентов (signal_client.py)

Агенты могут слать собственные сигналы на дашборд — без LLM, чистый Python, fire-and-forget.

```python
from signal_client import emit

# Агент отправляет промпт в DeepSeek
emit("main", "deepseek", "prompt", model="deepseek-v4-flash", tokens_in=1247)

# Агент отправил сообщение в Telegram
emit("inbox", "telegram", "message", latency_ms=120)

# Ошибка при вызове инструмента
emit("selfcoder", "github", "error", status="error")
```

**Типы сигналов:**
- `prompt` — запрос к LLM
- `response` — ответ от LLM
- `tool_call` — вызов инструмента
- `message` — отправка сообщения (Telegram, email)
- `document` — работа с документом
- `image` — работа с изображением
- `success` — успешное завершение задачи
- `error` — ошибка

Цвета на дашборде: prompt=синий, response=зелёный, error=красный, tool_call=жёлтый, message=фиолетовый.

---

## Дашборд (dashboard.shectory.ru)

**URL:** https://dashboard.shectory.ru  
**Auth:** единая учётка **Shectory Portal** (Basic-prompt, но проверка через портал, НЕ htpasswd). Username = **email** портала (напр. `bshevelev@mail.ru`, role `admin`), не `boris`. Подробно — раздел [Аутентификация](#аутентификация--стандарт-shectory-portal-bridge) ниже.  
**Обновление:** сигналы — каждые 3 секунды, топология — каждые 30 секунд

### Что показывает

```
┌─────────────────────────────────────────────────────────┐
│ 🚦 SHECTORY FEDERATION  [tkn today: 277k]  smain ● ok  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  [DeepSeek☁] [Gemini☁] [Telegram📱] [OpenAI☁] [Google📧]│
│         \        |          |           |       /       │
│          ╰───────┴──[ 🚦 LINEMAN ]──────╯               │
│               /      |      |      \                    │
│          [Tank🛠️] [SC⚡] [QA🔍] ... [Inbox📥]           │
│                                                         │
│  ●●● анимированные пакеты летят по bezier-путям ●●●    │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ prompt │ main→deepseek 1247tkn 800ms │ ...              │
└─────────────────────────────────────────────────────────┘
```

**Топология:**
- Верхний ряд — внешние сервисы (DeepSeek, Gemini, Telegram, OpenAI, Google)
- Центр — Lineman (хаб)
- Нижний ряд — агенты smain (из openclaw.json)
- Линии — постоянные соединения
- Цветные шарики — анимированные пакеты по кубическим bezier-кривым

**Клик по агенту** → модальное окно:
- Имя, эмодзи, нода, модель
- Описание (из openclaw.json)
- История последних 20 сигналов

---

## Пул прокси

Настраивается в `config.json → proxy_pool`.

```json
{
  "proxy_pool": {
    "proxies": [
      { "id": "proxy1", "url": "http://user:pass@45.155.200.232:8000", "priority": 1, "enabled": true },
      { "id": "iproyal", "url": "http://user:pass@86.109.80.236:12323", "priority": 2, "enabled": true }
    ],
    "routes": [
      { "hosts": ["*.googleapis.com", "api.telegram.org"], "proxies": ["proxy1", "iproyal"] },
      { "hosts": ["api.deepseek.com", "api.anthropic.com", "api.openai.com"], "proxies": [] },
      { "hosts": ["*.workers.dev"], "proxies": [] },
      { "hosts": ["*"], "proxies": [] }
    ]
  }
}
```

Логика выбора: для каждого upstream host ищется первый matching route → берётся первый доступный (не деградировавший) прокси из списка.

**Текущие прокси:**
- `proxy1` — датацентр 45.155.200.232 (приоритет 1)
- `iproyal` — резидентный 86.109.80.236:12323 (приоритет 2, credentials в `/home/shevbo/secure/tss.json` на sdev)

---

## Конфигурация (config.json) — ключевые секции

```jsonc
{
  "proxy_server": {
    "host": "127.0.0.1",  // nginx смотрит снаружи
    "port": 9090
  },
  "reverse_proxy": {
    "upstreams": {
      "deepseek": "https://api.deepseek.com",
      "google": "https://gemini-proxy-worker.bshevelev75.workers.dev",
      "anthropic": "https://api.anthropic.com",
      "openai": "https://api.openai.com"
    }
  },
  "agents": {
    "node_map": {
      "smain": ["main","selfcoder","qaper","virtual-boris","titan","nurse",
                "guilya","jobsearch-scanner","resume-editor","interview-coach","inbox"]
    }
  },
  "federation": {
    "local_node": "smain",
    "nodes": []  // V2: сюда добавить WG-ноды для мульти-нодного дашборда
  },
  "routing": {
    "default":   { "provider": "deepseek", "model": "deepseek-v4-flash" },
    "think":     { "provider": "deepseek", "model": "deepseek-v4-pro" },
    "longContext":{ "provider": "gemini",   "model": "gemini-3.1-pro-preview" },
    "longContextThreshold": 60000,
    "webSearch": { "provider": "gemini",   "model": "gemini-2.5-flash" }
  }
}
```

---

## Роутинг моделей

OpenClaw ставит заголовок `X-Lineman-Route`, Lineman выбирает модель:

| Route | Когда | Модель |
|-------|-------|--------|
| `default` | обычный запрос | deepseek-v4-flash |
| `think` | thinking/reasoning | deepseek-v4-pro |
| `background` | фоновые задачи | deepseek-v4-flash |
| `longContext` | > 60k токенов | gemini-3.1-pro-preview |
| `webSearch` | веб-поиск | gemini-2.5-flash |

---

## WireGuard IP → hostname

```python
WG_HOST_MAP = {
    "10.66.0.1": "smain",
    "10.66.0.2": "pi",
    "10.66.0.3": "cloud",
    "10.66.0.4": "sdev",
    "10.66.0.5": "pi2",
    "10.66.0.6": "vibe",
    "10.66.0.7": "hoster",
}
```

Источник: `db.py`. `source_host_from_ip("127.0.0.1")` определяет локальную ноду через `ip -4 addr show wg0`.

---

## Запуск и управление

```bash
# Запуск
cd /home/shectory/workspaces/lineman
python3 main.py

# Убить и перезапустить
fuser -k 9090/tcp && python3 main.py &

# Проверка что работает
curl http://127.0.0.1:9090/health
curl http://127.0.0.1:9090/api/nodes | python3 -m json.tool

# Лог
tail -f /tmp/lineman.log
```

---

## Аутентификация — стандарт Shectory Portal (bridge)

Дашборд защищён **единой учёткой Shectory Portal** (общая база `portal_users`, не htpasswd) через
**брендированный экран входа** (НЕ браузерный Basic popup). Логин = **email портала** (напр.
`bshevelev@mail.ru`). htpasswd-схема выпилена 2026-06-05; Basic popup заменён на cookie-сессию 2026-06-06.

Поток (cookie-сессия + фирменная страница):
1. Нет сессии → nginx `auth_request /_session_check` → 401 → `302 /login`.
2. `/login` — брендированная страница Lineman (`dashboard/login.html`: гифка Shectory, инфо-фрейм,
   форма email+пароль, тёмная тема портала). Форма шлёт `POST /api/login {email,password}`.
3. Lineman `_verify_portal_credentials` → `POST $SHECTORY_PORTAL_URL/api/internal/verify-portal-credentials`
   (`Authorization: Bearer $SHECTORY_AUTH_BRIDGE_SECRET`) → портал сверяет `portal_users` (bcrypt).
4. Успех → `Set-Cookie shectory_session=<HMAC>` (HttpOnly, Secure, SameSite=Lax, 7д; подпись —
   тем же `SHECTORY_AUTH_BRIDGE_SECRET`) → редирект на `?next`. Ошибка → 401 с текстом на странице.
5. Дальше nginx `auth_request /_session_check` → Lineman `GET /api/session-check` валидирует cookie
   (HMAC+срок) → 200/401. `POST /api/logout` чистит cookie.

Эндпоинты Lineman: `GET /login`, `POST /api/login`, `GET /api/session-check`, `POST /api/logout`
(+ defence-in-depth: `/klod-chat` принимает cookie напрямую). Хелперы `_make/_verify_session_token`,
`_session_email_from_cookie` (тесты — `tests/test_portal_auth.py`).

Env у Lineman: `SHECTORY_PORTAL_URL` (на smain `http://127.0.0.1:3000`), `SHECTORY_AUTH_BRIDGE_SECRET`
(тот же, что в `.env` портала; в чат/лог не печатать).

**Часть единого стандарта федерации.** Канон + брендированный welcome-экран — [`docs/PORTAL_AUTH_STANDARD.md`](docs/PORTAL_AUTH_STANDARD.md).

### nginx (dashboard.shectory.ru)

Полный конфиг — [`nginx/dashboard.shectory.ru.conf`](nginx/dashboard.shectory.ru.conf) (источник истины).
Применение (sudo у Бориса): `sudo cp nginx/dashboard.shectory.ru.conf /etc/nginx/sites-available/dashboard.shectory.ru && sudo nginx -t && sudo systemctl reload nginx`.

```nginx
location = /_session_check {                 # internal: валидация cookie-сессии
    internal; proxy_pass http://127.0.0.1:9090/api/session-check;
    proxy_pass_request_body off; proxy_set_header Cookie $http_cookie;
}
location = /login      { proxy_pass http://127.0.0.1:9090/login; }       # публичные
location = /api/login  { proxy_pass http://127.0.0.1:9090/api/login; }
location = /api/logout { proxy_pass http://127.0.0.1:9090/api/logout; proxy_set_header Cookie $http_cookie; }
location /     { auth_request /_session_check; error_page 401 = @login_redirect; proxy_pass http://127.0.0.1:9090/dashboard; proxy_set_header Cookie $http_cookie; }
location /api/ { auth_request /_session_check; error_page 401 = @login_redirect; proxy_pass http://127.0.0.1:9090/api/; proxy_set_header Cookie $http_cookie; }
location @login_redirect { return 302 /login?next=$request_uri; }
```

**Сброс пароля пользователя** — только в портале (`portal_users`, bcrypt; UI `https://shectory.ru/login` → «Забыли пароль»), не в nginx.

---

## Roadmap

### V1 (готово ✅)
- Reverse proxy с извлечением токенов
- SQLite request_log + signals таблица
- API: /api/signal, /api/signals, /api/nodes, /dashboard
- Дашборд: SVG-топология, анимация пакетов, agent modal
- nginx + SSL + Basic Auth на dashboard.shectory.ru
- Автоматические сигналы от каждого LLM-запроса
- signal_client.py SDK для агентов
- Пул прокси (proxy1 + iproyal) с route-based selection

### V2 (планируется)
- `from_agent` по `X-Openclaw-Agent` заголовку или trajectory correlation
- Мульти-нодный `/api/nodes` (опрос других WG-нод)
- Сигналы для инструментов (tool_call) — требует интеграции в OpenClaw
- Исторические графики токенов (Chart.js или SVG sparklines)
- Alerts при деградации сервисов (Telegram notification)
