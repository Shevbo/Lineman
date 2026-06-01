# Playbook: как агент работает с секретами через Ключника

Кейс ЭШколы: агент пилит приложение на sdev → деплоит на hoster, нужны три ключа (`ESHKOLA_DEEPSEEK`, `ESHKOLA_GEMINI`, `ESHKOLA_TGBOT`). Цель — единое хранилище, ноль засветов, ротация без даунтайма.

## Принципы (не нарушать)

1. **Значения секретов живут только в RAM процесса агента.** Никогда не пиши их в `.env`, `config.json`, `secrets.yaml`, переменные `export VAR=...`, git, чаты, логи.
2. **Метаданные секретов живут только у Ключника** (`~/.keymaster/manifest.json` на smain).
3. **Идентификация запросчика**: `requester="<agent_id>@<node>"`. Без этого Ключник не знает, кто легально пользуется ключом и кого уведомлять при ротации.
4. **Все исходящие LLM-вызовы — через Lineman** (`/proxy/google`, `/proxy/deepseek`, `/proxy/anthropic`). Lineman маскирует Authorization header в `request_log`.
5. **Ротация инициируется Борисом через TG-бота Ключника**. Никаких ручных `keymaster store` от агентов.

## Шаг 1 — Боря один раз создаёт секрет

В TG-чате с `@ShectoryKeyMasterBot`:

```
прими секрет: ESHKOLA_DEEPSEEK=sk-...
прими секрет: ESHKOLA_GEMINI=AQ.Ab...
прими секрет: ESHKOLA_TGBOT=1234567890:AAFake...
```

Файлы попадают в `~/.keymaster/credentials/eshkola_*` (perm 600). `manifest.secrets[ESHKOLA_*]` создаётся с `rotate_with: "manual"` по умолчанию. Значения никуда не логируются.

## Шаг 2 — Боря даёт pre_approve (опционально, ускоряет)

Чтобы агент не ждал TG-аппрува на каждый рестарт:

```bash
# на smain (только с localhost)
curl -X POST 'http://127.0.0.1:9093/keymaster/pre_approve?name=ESHKOLA_DEEPSEEK&requester=eshkola@sdev'
curl -X POST 'http://127.0.0.1:9093/keymaster/pre_approve?name=ESHKOLA_DEEPSEEK&requester=eshkola@hoster'
```

Без pre_approve первый запрос с новой ноды блокируется на ручной TG-аппрув (это safety feature).

## Шаг 3 — Агент получает ключи (одна функция)

В коде агента ЭШколы:

```python
from klod_keymaster import get, start_rotation_watcher

REQUESTER = f"eshkola@{os.uname().nodename if hasattr(os,'uname') else os.environ['COMPUTERNAME'].lower()}"

# Запустить фоновый watcher — ловит signal key_rotated и сбрасывает RAM cache
start_rotation_watcher(REQUESTER)

DEEPSEEK = get("ESHKOLA_DEEPSEEK", requester=REQUESTER, purpose="LLM-клиент ЭШколы")
GEMINI   = get("ESHKOLA_GEMINI",   requester=REQUESTER, purpose="TTS personas + chat")
TGBOT    = get("ESHKOLA_TGBOT",    requester=REQUESTER, purpose="бот ЭШколы для подростков")
```

Что под капотом:
- `get()` сначала проверяет RAM cache → вернёт мгновенно.
- Если cache пуст: `POST /keymaster/request-value` → pending. Если `requester` есть в `pre_approved` — мгновенный `approve` + `deliver`. Иначе TG-уведомление Борису, ждём аппрува до 60s.
- После успешной выдачи Ключник автоматически добавляет `requester` в `manifest.secrets[name].used_by[]`.

## Шаг 4 — Использовать ключ ТОЛЬКО через Lineman

```python
import httpx
resp = httpx.post(
    "http://10.66.0.1:9090/proxy/deepseek/v1/chat/completions",
    headers={"Authorization": f"Bearer {DEEPSEEK}", "X-Agent-Name": "eshkola"},
    json={"model": "deepseek-v4-flash", "messages": [...]}
)
```

Lineman:
- маскирует `Authorization` через [secret_mask.py](../secret_mask.py) перед записью в `request_log`
- роутит через iProyal/proxy6 (геобайпас)
- считает токены, латентность, цену
- ставит флаг агента (`source_agent=eshkola`) в логах и в dashboard

**Никогда не делайте `print(DEEPSEEK)`, `logger.info(f"key={DEEPSEEK}")`, не кладите в exception messages.**

## Шаг 5 — Деплой sdev → hoster

```bash
# на sdev: код едет без секретов
git push
# на hoster: pull + restart
ssh hoster 'cd ~/workspaces/eshkola && git pull && pm2 restart eshkola'
```

`eshkola@hoster` при первом старте делает `get(...)`. Если Борис заранее `pre_approve`-нул этот requester — мгновенно. Иначе — однократный ручной TG-аппрув. Дальше `used_by` манифеста становится `["eshkola@sdev", "eshkola@hoster"]`.

## Шаг 6 — Ротация без даунтайма

В TG Боря пишет:
```
прими секрет: ESHKOLA_DEEPSEEK=новое_значение
```

Что происходит автоматически:
1. Ключник перезаписывает файл `~/.keymaster/credentials/eshkola_deepseek`.
2. Ключник видит `manifest.secrets[ESHKOLA_DEEPSEEK].used_by = ["eshkola@sdev", "eshkola@hoster"]`.
3. Дёргает `rotation_push(name)` → для каждого requester шлёт `POST /api/signal type=key_rotated key_name=ESHKOLA_DEEPSEEK to_service=eshkola`.
4. `start_rotation_watcher()` в процессе агента (на sdev и на hoster) ловит сигнал, вызывает `on_rotation("ESHKOLA_DEEPSEEK")` → cache сбрасывается.
5. Следующий `get("ESHKOLA_DEEPSEEK", ...)` забирает свежее значение, опять же без диска.

Время задержки — `interval=30s` poll'а + сетевые ms. Никаких рестартов процесса.

## Что Klod-Access (Lineman инженер) проверяет

Ежедневный audit (`scripts/lineman_daily_audit.py`) включает секцию keymaster:
- сколько активных секретов
- какие давно не ротировались (если установлен `last_rotated_at`)
- какие имеют пустой `used_by` (или хочется убрать?)
- свежие `key_rotated` сигналы за сутки

## Анти-паттерны (нельзя)

- **Скопировать `~/.keymaster/credentials/` на другую ноду через scp**. Каждый узел сам запрашивает у Ключника.
- **Сохранить `DEEPSEEK = get(...)` в файл `cache.json`**. Только RAM.
- **Передать ключ в URL query**. Только headers (`Authorization`, `x-goog-api-key`).
- **`print(DEEPSEEK)` для отладки**. Используй `print(f"key prefix: {DEEPSEEK[:4]}***, len={len(DEEPSEEK)}")`.
- **Хранить ключ в env-переменной процесса с автозапуском**. PM2 dump.pm2 запишет env на диск → утечка.
- **Использовать один ключ для нескольких приложений**. Каждое приложение → свой именованный ключ → можно ротировать отдельно.

## Helper установлен на узлах

- smain: `~/workspaces/infra/lineman/klod_keymaster.py`
- sdev/hoster/pi/pi2: `/opt/klod_keymaster.py`
- vibe: `C:\Users\Boris\klod_keymaster.py`

CLI:
```bash
python3 klod_keymaster.py --manifest                  # все метаданные (без значений)
python3 klod_keymaster.py ESHKOLA_DEEPSEEK eshkola@sdev "smoke test"
```

CLI печатает только префикс и длину, никогда — значение.
