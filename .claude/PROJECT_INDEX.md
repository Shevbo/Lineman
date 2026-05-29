# Lineman — индекс для быстрой навигации

Карта что где читать. Не повторяй здесь содержимое, только указывай куда смотреть.

## Старт сессии — обязательное чтение

1. `CLAUDE.md` (корень) — кто ты, что разрешено, workflow
2. `.claude/memory/MEMORY.md` — индекс проектной памяти
3. `.claude/memory/01_architecture.md` — что такое Lineman, файлы, компоненты
4. `.claude/memory/03_critical_paths.md` — что нельзя ломать
5. `.claude/memory/04_incidents.md` — историю граблей

## По задачам

| Задача | Куда смотреть |
|--------|---------------|
| "Lineman не отвечает" | `02_operations.md` → "Куда смотреть когда что-то идёт не так" |
| "Хочу добавить нового LLM-провайдера" | `05_config_reference.md` → `reverse_proxy.upstreams` + `proxy_pool.routes` |
| "Хочу поднять/понизить thresholds" | `03_critical_paths.md` → P1 + `circuit_breaker.py` |
| "Хочу добавить sub-агента" | `.claude/agents/` (готовые: lineman-tester, lineman-reviewer) |
| "Хочу разобраться с маршрутом запроса" | `01_architecture.md` Path A/B/C + `proxy_server.py` + `pool.py` |
| "Был инцидент" | После фикса — добавь запись в `04_incidents.md` |
| "Нужны метрики/токены/тренды" | `analytics.py`, `metrics.py`, `lineman.db` (`request_log`, `signals`) |
| "Цены моделей" | `config.json` → `pricing`, `docs/llm-pricing-2026-05.md` |
| "Дашборд" | `WIKI.md` + nginx + `dashboard.shectory.ru` |
| "Telegram алёрты" | `notifier.py` + `/api/tg/send` + `~/keymaster/.lineman-proxy.env:TELEGRAM_BOT_TOKEN` |

## Внешние ссылки

- `~/FEDERATION.md` — топология federation, узлы, агенты, прокси-схема
- `~/AGENTS.md` — глобальные правила для всех OpenClaw-агентов
- `~/.claude/projects/-home-shectory/memory/MEMORY.md` — глобальная память Executive Advisor
- `~/.claude/projects/-home-shectory/memory/project_lineman.md` — глобальная заметка Бориса про Lineman
- `~/.openclaw/openclaw.json` — конфигурация OpenClaw (read-only из этого workspace)

## Артефакты этого workspace

```
~/workspaces/infra/lineman/
├── CLAUDE.md                       ← project instructions для главного Claude
├── AGENTS.md                       ← правила для OpenClaw-агентов
├── WIKI.md                         ← техдокументация v2
├── TZ_LINEMAN.md                   ← историческое ТЗ
├── .gitignore
├── .claude/
│   ├── PROJECT_INDEX.md           ← этот файл
│   ├── settings.json              ← project-level permissions
│   ├── settings.local.json        ← local overrides (не в git)
│   ├── agents/
│   │   ├── lineman-tester.md
│   │   └── lineman-reviewer.md
│   └── memory/
│       ├── MEMORY.md              ← индекс памяти
│       ├── 01_architecture.md
│       ├── 02_operations.md
│       ├── 03_critical_paths.md
│       ├── 04_incidents.md
│       ├── 05_config_reference.md
│       └── 06_testing.md
├── main.py, proxy_server.py, ...   ← код
├── tests/                          ← pytest
├── checks/                         ← health probes
├── docs/                           ← внутренняя docs
├── cloudflare-worker/              ← claude-connect-worker, gemini-proxy
├── lineman.db, *.json              ← persistent state (gitignored)
└── .venv/                          ← Python virtualenv (gitignored)
```
