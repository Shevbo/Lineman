# ТЗ: Lineman v2 — умный API-шлюз + мониторинг

**Продукт:** автономный прокси-сервер с умным роутингом, аналитикой и авторемонтом
**Модель исполнения:** фоновый процесс (systemd user unit), Python 3.12+

> **Актуальная документация:** `WIKI.md` в этой же папке.  
> Это ТЗ — исторический документ фазы разработки.

---

## Статус реализации (2026-05-13)

| Фаза | Что | Статус |
|------|-----|--------|
| v1 core | Forward proxy :9090, CONNECT tunnel, smart routing | ✅ Done |
| v1 db | SQLite request_log, health checks, metrics | ✅ Done |
| v1 pool | Proxy pool (proxy1 + iproyal), route-based selection | ✅ Done |
| v2 rproxy | Reverse proxy `/proxy/{provider}/...`, token counting from body | ✅ Done |
| v2 signals | SQLite signals table, SignalQueue, 24h TTL | ✅ Done |
| v2 dashboard | SVG topology, packet animations, agent modal | ✅ Done |
| v2 nginx | dashboard.shectory.ru, SSL (LE), Basic Auth | ✅ Done |
| v3 | Multi-node /api/nodes, from_agent header, tool_call signals | 📋 Planned |

---

## 1. Архитектура v2

```
lineman/
├── main.py              # точка входа: запуск proxy + мониторинга
├── proxy_server.py       # HTTP forward proxy (:9090)
├── router.py             # умный роутинг моделей (ccr-логика)
├── analytics.py          # аналитика токенов (ccusage-логика)
├── rtk.py                # интеграция с RTK (сжатие dev-вывода)
├── config.json           # конфигурация (сервисы + роутинг + аналитика)
├── checks/               # health-check модули
│   ├── __init__.py
│   ├── deepseek.py
│   ├── gemini.py
│   ├── google_services.py
│   └── telegram.py
├── healer.py             # авторемонт (7+ сценариев)
├── notifier.py           # оповещение агентов
├── reporter.py           # ежедневный отчёт (Google Doc)
├── metrics.py            # сбор метрик
├── retry.py              # exponential backoff helper
└── state.json            # текущее состояние (persistent)
```

---

## 2. Компоненты Lineman v2

### 2.1 HTTP Forward Proxy (proxy_server.py) — P0

- Слушает `localhost:9090`
- Принимает HTTP и HTTPS (CONNECT) запросы
- Маршрутизирует по host к правильному upstream
- Инжектит API-ключи (из env → openclaw config)
- Выбирает upstream прокси: DeepSeek напрямую, Gemini/Google через осн.прокси, Telegram через Proxy6
- Логирует все запросы: метод, URL, статус, латентность, размер ответа

**Формат прокси-заголовков для клиентов:**
```
X-Lineman-Route: default|think|background|longContext|webSearch
X-Lineman-Model: <model_id>
```

### 2.2 Умный роутинг (router.py) — P0

По мотивам claude-code-router. Правила:

| Контекст | Критерий | Модель по умолчанию |
|----------|----------|---------------------|
| default | обычный запрос | deepseek-v4-flash |
| think | thinking/размышления | deepseek-v4-pro |
| longContext | >60K токенов | gemini-3.1-pro-preview |
| background | фоновые задачи | deepseek-v4-flash |
| webSearch | веб-поиск | gemini-2.5-flash |

**Определение контекста:**
- `X-Lineman-Route` заголовок от клиента (приоритет)
- Авто-определение по содержимому запроса:
  - `background`: фоновые задачи (system prompt содержит "background"/"фон")
  - `think`: запросы с `thinking: high` или `reasoning`
  - `longContext`: контекст > 60000 токенов
  - `webSearch`: запрос содержит инструменты веб-поиска
  - `default`: всё остальное

**Fallback при DOWN:**
- default/background: Flash → Pro (тот же провайдер) → Gemini Flash
- think: Pro → другой провайдер Pro → Gemini 3.1 Pro
- longContext: Gemini 3.1 Pro → DeepSeek Pro
- webSearch: Gemini Flash → DeepSeek Flash

### 2.3 Аналитика токенов (analytics.py) — P1

По мотивам ccusage:
- Парсит JSONL-логи Claude Code (`~/.claude/projects/*/`)
- Считает токены: input/output, стоимость
- Группирует по дням, месяцам, сессиям
- Выдаёт через HTTP API: `GET /analytics?period=day|month|session`
- Формат: таблица с daily/monthly/session breakdown
- Интеграция с reporter.py для ежедневного отчёта

### 2.4 RTK интеграция (rtk.py) — P1

- Lineman запускает dev-команды через RTK для сжатия вывода
- RTK устанавливается как бинарник: `~/.local/bin/rtk`
- API: `POST /rtk` — принять команду, выполнить через RTK, вернуть сжатый вывод
- Поддерживаемые команды: git, ls, cat, grep, find, cargo, npm, pytest, docker, gh
- RTK gain-статистика выводится в ежедневном отчёте

### 2.5 Health-monitoring — P2 (уже есть, расширить)

- Добавить health-check самого proxy (localhost:9090)
- Добавить health-check RTK (rtk --version)
- Добавить health-check ccusage (пакет установлен)

### 2.6 HTTP API (proxy_server.py)

Lineman отдаёт JSON на этих эндпоинтах:

| Метод | Путь | Назначение |
|-------|------|------------|
| GET | /health | Статус всех служб |
| GET | /metrics | Текущие метрики (JSON) |
| GET | /analytics | Аналитика токенов |
| POST | /rtk | Выполнить dev-команду через RTK |
| GET | /state | Полное состояние (state.json) |

---

## 3. Конфигурация (config.json) — расширенная

Добавляются секции:
- `routing` — правила роутинга
- `proxy` — настройки прокси-сервера
- `analytics` — пути к логам для аналитики

---

## 4. Приоритеты реализации

| Приоритет | Компонент | Оценка |
|-----------|-----------|--------|
| P0 | proxy_server.py (forward proxy) | 300 строк |
| P0 | router.py (умный роутинг) | 200 строк |
| P0 | config.json (расширение) | обновить |
| P0 | main.py (запуск proxy + мониторинг) | адаптировать |
| P1 | analytics.py (ccusage) | 200 строк |
| P1 | rtk.py (интеграция) | 100 строк |
| P2 | health-check proxy в checks/ | добавить |

---

## 5. Интеграция с OpenClaw

- systemd user unit: `~/.config/systemd/user/lineman.service`
- Gateway config: все провайдеры → `"proxy": "http://localhost:9090"`
- Агенты: все HTTP-запросы через `http://localhost:9090`
- API-ключи: из `openclaw config get` или env
- Логи: structlog, JSON-формат для прода
