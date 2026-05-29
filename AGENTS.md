# Lineman — кто и как работает с этим кодом

Lineman — единственный прокси и сигнальный шлюз federation. Падение Lineman = остановка всех агентов, Telegram молчит, dashboard гаснет. Поэтому правила доступа разные для разных ролей.

## Главный инженер: Claude Code (Opus 4.7) в этом workspace

Когда Борис открывает этот репозиторий в VS Code и запускает Claude Code (Opus 4.7) — этот сессионный Claude **имеет полные права** на правки, тесты, коммиты, push.

Его инструкции — `CLAUDE.md` в корне. Его проектная память — `.claude/memory/`. Его sub-агенты — `.claude/agents/`. Его permissions — `.claude/settings.json`.

Главный Claude несёт **личную ответственность** за каждое изменение:
- Перед правкой — pytest baseline зелёный
- После правки — pytest + smoke зелёные
- Перед коммитом — `lineman-reviewer` если правка касается P0-путей (см. `.claude/memory/03_critical_paths.md`)
- После критичного фикса — запись в `.claude/memory/04_incidents.md`

Согласование с Борисом нужно только для:
- Изменения proxy_pool credentials
- Удаления/миграции `lineman.db`
- Изменения circuit breaker thresholds
- Push --force / reset --hard
- Деплой `cloudflare-worker/`

## OpenClaw-агенты (qaper, selfcoder, и др.) — только чтение и сигналы

**ЗАПРЕЩЕНО** OpenClaw-агентам:
- Редактировать ЛЮБЫЕ `.py` файлы
- Редактировать `config.json`
- Перезапускать `lineman.service`
- Делать `git commit` или `git push`
- Создавать новые файлы

**РАЗРЕШЕНО**:
- Читать любые файлы для контекста
- Запускать `pytest tests/` в read-only режиме
- Curl на `http://127.0.0.1:9090/health`, `/metrics`, `/api/log/stats`
- Слать сигнал/проблему главному Claude через:
  ```bash
  curl "http://127.0.0.1:9090/api/agent/main/message?from=<твой_id>&message=Lineman:%20<описание>"
  ```

Нарушение → автоматический алерт Борису через Telegram (через `inotifywait` на критичные файлы — см. ниже).

## Активная защита (для OpenClaw-агентов)

`inotifywait` следит за: `main.py`, `proxy_server.py`, `_http_raw.py`, `pool.py`, `db.py`, `config.json`, `circuit_breaker.py`. Изменение **извне сессии главного Claude** → мгновенный алерт.

Главный Claude в этом workspace детектируется по working directory сессии (`/home/shectory/workspaces/infra/lineman/`).

## Текущее окружение

- Процесс: `systemctl --user {status,restart} lineman`
- Сервис: `lineman.service` → `run-lineman.sh` → `.venv/bin/python3 main.py`
- Порт: `http://127.0.0.1:9090` (на 0.0.0.0:9090 для WG-федерации)
- Health: `curl http://127.0.0.1:9090/health`
- Логи: `journalctl --user -u lineman -f`
- БД: `lineman.db` (~440MB), WAL до 1GB

## Если что-то сломалось

Если OpenClaw-агент видит проблему с Lineman:
1. Сообщи Борису точный текст ошибки одним сообщением в TG
2. Напиши главному Claude (на smain) через `/api/agent/main/message`
3. Жди — не повторяй упавший вызов

**ЗАПРЕЩЕНО:** молча пробовать тот же вызов ещё раз с другими параметрами.

---

## 📒 Дисциплина памяти и федерации

**ПРАВИЛО:** любое **ключевое** изменение Lineman (роутинг, прокси-пул, схема БД, upstreams, thresholds, новые маршруты) — **обязательно** в том же ходу:

1. Запись в `.claude/memory/` (соответствующий файл: architecture / operations / critical_paths / incidents / config_reference / testing).
2. Обновление `WIKI.md` если затронуты публичные контракты.
3. Обновление `~/FEDERATION.md` (раздел Lineman + журнал инфра-фиксов) — это живая документация всей сети.
4. Если изменение касается поведения других агентов — оповестить через federation API:
   ```bash
   curl "http://127.0.0.1:9090/api/agent/<agent_id>/message?from=lineman-curator&message=ОБНОВЛЕНИЕ:%20..."
   ```

**Why:** без записи в память следующая сессия повторит уже отлаженное расследование с нуля.

**Триггеры (без исключений):**
- Изменение `config.json` (особенно `proxy_pool`, `routing`, `reverse_proxy.upstreams`)
- Изменение P0-путей (`_http_raw.py`, `pool.py`, `proxy_server.py`, `db.py`)
- Любой инцидент → запись в `04_incidents.md` по шаблону YYYY-MM-DD
- Изменение circuit breaker / dedup thresholds

Конкретные **значения** секретов в память **НЕ** писать — только имя env-переменной и путь к файлу.
