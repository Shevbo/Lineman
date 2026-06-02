# Onboarding для агента — стандарт работы с секретами и LLM

Этот документ — обязательное чтение перед тем, как любой агент федерации (новый или существующий) начнёт работать с секретами или вызывать LLM. Универсальный, без привязки к конкретному имени.

## Кто ты в этом контексте

Ты — самостоятельный агент в федерации Shectory. У тебя:
- Уникальный `<agent_id>` (например `tank`, `nurse`, `inbox`, `eshkola`, и т. д.).
- Узел исполнения `<node>` (`smain` / `sdev` / `hoster` / `vibe` / `pi` / `pi2`).
- Возможно несколько копий — `<agent_id>@sdev` и `<agent_id>@hoster` это два разных requester'а.

Ниже — стандарт, которому ты обязан соответствовать. Главный инженер инфраструктуры (Klod-Access) проверяет это через `daily audit`, `top_offenders` и `klod-access inbox`. Несоответствие — алёрт Борису.

## Контракт в одной фразе

**Метаданные секретов → Ключник. Значения секретов → только в RAM процесса. LLM-вызовы → только через Lineman reverse-proxy. Идентификация → `X-Agent-Name` в каждом запросе.**

## 1. Секреты — пять правил

1. **Никогда** не пиши значение секрета на диск: `.env`, `config.json`, `secrets.yaml`, `.bashrc` — табу.
2. **Никогда** не клади секрет в env-переменную процесса с автозапуском (PM2 dump.pm2 запишет на диск).
3. **Никогда** не `print()`, `logger.info()`, не суй в exception text. Если нужно для debug — только `value[:4] + '***' + value[-2:]`.
4. **Никогда** не передавай через query-параметр (`?key=…`). Только в headers (`Authorization: Bearer …` / `x-goog-api-key: …`).
5. **Никогда** не копируй секреты между узлами через scp/rsync. Каждый узел самостоятельно запрашивает у Ключника.

## 2. Как получить секрет правильно

В Python — используй helper `klod_keymaster` (он есть на всех узлах: `/opt/klod_keymaster.py` на Linux, `C:\Users\Boris\klod_keymaster.py` на vibe):

```python
import socket
import sys; sys.path.insert(0, "/opt")  # или скопируй helper в свой пакет
from klod_keymaster import get, start_rotation_watcher

NODE = socket.gethostname()  # на Windows: os.environ['COMPUTERNAME'].lower()
REQUESTER = f"<agent_id>@{NODE}"

# Запускается один раз при старте процесса
start_rotation_watcher(REQUESTER)

def deepseek_key():
    return get("<UPPERCASE_SECRET_NAME>",
               requester=REQUESTER,
               purpose="<краткое описание зачем>")
```

Что под капотом:
- Первый вызов `get()`: `POST /keymaster/request-value` → если `requester` уже в `pre_approved` списке — мгновенный `approve+deliver`, иначе TG-уведомление Бори и ожидание до 60 секунд.
- Последующие вызовы: возврат из RAM-кеша (мгновенно, без сети).
- Ключник автоматически добавляет тебя в `manifest.secrets[name].used_by[]` при первом успешном `deliver`.

## 3. Pre-approve (один раз при создании или новой ноде)

Когда секрет уже существует и Боря согласовал твоё право им пользоваться, добавь себя в `pre_approved` чтобы не блокироваться ручным TG-аппрувом на каждый рестарт:

```bash
# на smain (localhost only)
curl -X POST "http://127.0.0.1:9093/keymaster/pre_approve?name=<NAME>&requester=<agent_id>@<node>"
```

Если ты на другой ноде — попроси Klod-Access или Бориса это сделать.

## 4. LLM-вызовы — только через Lineman reverse-proxy

Все LLM-провайдеры доступны на единых эндпоинтах:

```
http://10.66.0.1:9090/proxy/deepseek/v1/chat/completions
http://10.66.0.1:9090/proxy/google/v1beta/models/{model}:generateContent
http://10.66.0.1:9090/proxy/anthropic/v1/messages
http://10.66.0.1:9090/proxy/openai/v1/chat/completions
http://10.66.0.1:9090/proxy/ollama-hoster/v1/chat/completions
http://10.66.0.1:9090/proxy/lm-studio/v1/chat/completions
```

В каждый запрос обязателен заголовок:

```http
X-Agent-Name: <agent_id>
Authorization: Bearer <token-from-klod_keymaster>
```

Что даёт reverse-proxy за тебя:
- Маскирование `Authorization` перед записью в `request_log` (без этого ключ просочится в SQLite-журнал).
- Геобайпас (iProyal / Proxy6 / Cloudflare-worker — pool сам выбирает).
- Подсчёт токенов, латентности, цены в твою статистику.
- Видимость в dashboard как `source_agent=<agent_id>`.
- Compression крупных хвостов истории.
- Dedup идентичных запросов в короткое окно.
- Circuit breaker при сбое апстрима — автоматический failover.

## 5. Telegram — особый случай

Telegram-бот идёт через системный HTTPS (не через reverse-proxy), но Lineman всё равно пробивает геоблок:

```python
import requests
tgbot = get("TELEGRAM_BOT_TOKEN_<NAME>", requester=REQUESTER, purpose="...")
requests.post(
    f"https://api.telegram.org/bot{tgbot}/sendMessage",
    json={"chat_id": ..., "text": ...},
    proxies={"https": "http://10.66.0.1:9090"},
)
```

`proxies=...` для Telegram обязательно — без него запрос пойдёт прямо и упрётся в геоблок RU.

## 6. Ротация — твоя реакция

Когда Боря присылает новое значение секрета через TG-бота Ключника, Keymaster автоматически шлёт `signal type=key_rotated key_name=<NAME> to_service=<your_agent_id>` через Lineman.

Если ты вызвал `start_rotation_watcher(REQUESTER)` — он сам поймает signal и сбросит кеш конкретного ключа. Следующий `get()` возьмёт свежее значение **без перезапуска процесса**.

Если не используешь watcher — обязан опрашивать `/api/signals?since=N` каждые 30 секунд и при `type=key_rotated key_name=<X>` вызывать `klod_keymaster.on_rotation("<X>")`.

## 6.5 Что делать если ты увидел утечку секрета

**Обнаружив строку похожую на секрет** — в чужом чате, в request body, в коммите, в логе, в файле, в issue tracker — НЕМЕДЛЕННО:

```python
import urllib.request, json
urllib.request.urlopen(urllib.request.Request(
    "http://10.66.0.1:9090/api/keymaster/leak_alert",
    data=json.dumps({
        "secret_name": None,                # или конкретное имя если знаешь
        "where": "chat:agent-X/msg-42",     # или file:path:line / request_log:N
        "snippet": "<первые 24 символа>",   # или полный, маска применится
        "source_agent": "<твой agent_id>@<node>",
        "severity": "high",
    }).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
), timeout=4).read()
```

Что произойдёт **автоматически** (без LLM, без задержек):

1. Запись в `~/.keymaster/leak_alerts.log` (audit).
2. Сообщение в Klod-Access inbox с пометкой `ROTATION_NEEDED` — я увижу в начале моей следующей сессии вместе с context.
3. TG-уведомление Борису через `@ShectoryKeyMasterBot` со снэппетом и инструкцией.
4. **Auto-rotation** для internal токенов (`*GATEWAY*`/`*INTERNAL*`/`*AUTH_BRIDGE*`): keymaster сам сгенерирует `openssl rand -hex 24` и опубликует новое значение. Старое мёртвое через секунды.
5. Для external (TG bot / Google API / DeepSeek / OpenAI): Боре приходит готовый гайд «зайди сюда, revoke здесь, пришли новое в формате X». 

Время от «нашёл утечку» до «старый токен мёртв» — секунды для internal, минуты для external.

**Не пытайся** угадывать какой это секрет или ждать что Боря сам заметит. Просто шли алёрт — секрет может быть фейковый, тогда ничего страшного не случится. Если настоящий — спас федерацию.

## 7. Жалобы / связь с Klod-Access

Когда что-то непонятно или сломалось:

```python
from klod_client import complain, notify, ask

complain("<agent_id>@<node>", "401 на gemini-flash 10 минут подряд")
# Klod-Access увидит в начале своей следующей сессии вместе с triage
# твоих последних 5 ошибочных запросов из request_log.

notify("<agent_id>@<node>", "запустил миграцию X")
# Информация без триажа.

ask("<agent_id>@<node>", "можно использовать Gemini Pro для этой задачи?")
# Klod-Access ответит через /reply, ответ придёт тебе в твой /api/agent/<id>/message.
```

Helper `klod_client.py` лежит в `~/workspaces/infra/lineman/klod_client.py`, без зависимостей.

## 8. Acceptance criteria (проверь себя)

Прежде чем считать что присоединился к стандарту:

- [ ] `grep -rE "AIza|sk-[A-Za-z0-9_-]{10,}|bot[0-9]+:[A-Za-z0-9_-]{30,}|0x[A-Fa-f0-9]{40,}" .` в твоём репо ничего не выводит.
- [ ] Нет файлов `.env`, `*.env`, `secrets.*` с реальными значениями. `.gitignore` содержит `.env` и `secrets.*`.
- [ ] Каждый секрет получается через `klod_keymaster.get()`.
- [ ] Каждый LLM-вызов идёт через `http://10.66.0.1:9090/proxy/...` с заголовком `X-Agent-Name`.
- [ ] В Lineman dashboard за последний час видны signals с твоим `source_agent`.
- [ ] `curl /keymaster/manifest?name=<TWOIY_KEY>` показывает тебя в `used_by`.
- [ ] При тесте ротации (Боря шлёт новое значение) твой процесс через 30 секунд начинает использовать новое значение **без рестарта**.

## 9. Запрещено

- `os.environ.get("...API_KEY")` для секретов из Ключника — обязательно `klod_keymaster.get()`.
- Прямые запросы к `https://api.deepseek.com`, `https://generativelanguage.googleapis.com`, `https://api.anthropic.com`, `https://api.openai.com` — только через `/proxy/{provider}/`.
- Логирование тел LLM-запросов на своей стороне (Lineman это делает за тебя с маскированием).
- Создание собственных `cron` или `setInterval` которые тебе не нужны (LLM-cron'ы — особенно дорого, см. [SECRETS_PLAYBOOK.md](SECRETS_PLAYBOOK.md)).

## 10. Что ты получаешь как соответствующий стандарту

1. **Прозрачность**: твои запросы видны в dashboard, токены считаются, ошибки атрибутируются.
2. **Ротация без даунтайма**: Боря меняет ключ — ты автоматически подхватываешь.
3. **Геобайпас**: ходи как обычно, Lineman сам разруливает iProyal/Proxy6/CF-worker.
4. **Защита от утечки**: даже если случайно положишь ключ в request body — Lineman вырежет перед записью в БД.
5. **Audit-след**: в `~/.keymaster/audit.log` зафиксировано когда ты впервые получил каждый ключ.
6. **Connect-protection**: при подозрительном паттерне (3 подряд 403, утечка, OOM) Klod-Access получит P0-алёрт и придёт чинить.

## 11. Связанные документы

- [SECRETS_PLAYBOOK.md](SECRETS_PLAYBOOK.md) — глубокие правила и анти-паттерны.
- [REPORT_2026-05-29.md](REPORT_2026-05-29.md) — что было ДО внедрения стандарта (12 предложений P0/P1/P2).
- [DAILY_*.md](.) — ежедневные KPI-отчёты от Klod-Access.

## 12. Куда жаловаться если документ непонятен

`klod_client.complain("<agent_id>@<node>", "пункт N в onboarding непонятен: ...")` — Klod-Access увидит в начале следующей сессии и переформулирует.
