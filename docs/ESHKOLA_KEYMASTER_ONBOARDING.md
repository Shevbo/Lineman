# ЭШкола — onboarding на инфраструктуру секретов

Ты — агент, работающий над ЭШколой (обучающее приложение для подростков). Ты **первый клиент** новой инфраструктуры секретов на федерации Shectory. Это значит, что в твоей работе нет прецедентов и сложившихся практик — но есть готовые инструменты и playbook. Прочитай этот файл и [SECRETS_PLAYBOOK.md](SECRETS_PLAYBOOK.md) полностью до того, как тронешь код.

## Что уже есть

Боря дал тебе раньше **три ключа** напрямую, они оказались в `.env` файле на `hoster` (и, возможно, на `sdev`). Это исторический долг — раньше так делали все. Ключи:

- `ESHKOLA_DEEPSEEK` — DeepSeek API key
- `ESHKOLA_GEMINI` — Google Gemini API key  
- `ESHKOLA_TGBOT` — Telegram bot token для ЭШколы

Сейчас они также **уже лежат в Ключнике** (`~/.keymaster/credentials/eshkola_*`). Источник истины — Ключник, не `.env`. Подтверди это перед началом работы:

```bash
# с smain или sdev через WG
curl -s 'http://10.66.0.1:9093/keymaster/manifest?name=ESHKOLA_DEEPSEEK' | jq
curl -s 'http://10.66.0.1:9093/keymaster/manifest?name=ESHKOLA_GEMINI' | jq
curl -s 'http://10.66.0.1:9093/keymaster/manifest?name=ESHKOLA_TGBOT' | jq
```

Если какого-то нет — попроси Бориса добавить через `@ShectoryKeyMasterBot`: «прими секрет: ESHKOLA_X=...».

## Что считается плохим (текущее состояние)

1. **Любое значение секрета в `.env`, `config.json`, `secrets.yaml`** на диске узла.
2. **`os.environ.get("ESHKOLA_DEEPSEEK")`** в твоём коде — env-переменные пишутся в PM2 dump.pm2, могут быть прочитаны через `/proc/PID/environ`.
3. **`print(api_key)` / `logger.info(f"key={k}")` / exception messages с ключом**.
4. **`scp .env`** между узлами. Каждый узел должен сам ходить к Ключнику.
5. **Один ключ для нескольких приложений**. У ЭШколы — отдельные именованные ключи.

## Что станет (целевое состояние)

```python
# eshkola/keys.py — единственный файл, который думает про секреты
import os, socket
from klod_keymaster import get, start_rotation_watcher

NODE = os.environ.get("LINEMAN_NODE") or socket.gethostname()
REQUESTER = f"eshkola@{NODE}"

# Запустить фоновый watcher — ловит Лиман-сигнал key_rotated и сбрасывает RAM cache
start_rotation_watcher(REQUESTER)

def deepseek_key() -> str:
    return get("ESHKOLA_DEEPSEEK", requester=REQUESTER, purpose="LLM-клиент уроков ЭШколы")

def gemini_key() -> str:
    return get("ESHKOLA_GEMINI", requester=REQUESTER, purpose="TTS personas + chat")

def tgbot_token() -> str:
    return get("ESHKOLA_TGBOT", requester=REQUESTER, purpose="бот ЭШколы для учеников")
```

Все вызовы LLM — **через Lineman** (геобайпас, маскирование, токены, dashboard):

```python
import httpx
resp = httpx.post(
    "http://10.66.0.1:9090/proxy/deepseek/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {deepseek_key()}",
        "X-Agent-Name": "eshkola",   # ОБЯЗАТЕЛЬНО — без этого ты невидим в dashboard
    },
    json={"model": "deepseek-v4-flash", "messages": [...]},
)
```

Аналогично для Gemini — `http://10.66.0.1:9090/proxy/google/v1beta/models/...:generateContent`.

Telegram-бот пользуйся прямой `https://api.telegram.org/bot{tgbot_token()}/...` через системный http (Lineman сам пробивает геоблок для TG).

## План миграции (точные шаги)

### Шаг 1 — Inventory: где сейчас секреты в твоём коде

```bash
cd ~/workspaces/eshkola   # или где у тебя репо
grep -rnE "ESHKOLA_(DEEPSEEK|GEMINI|TGBOT)|sk-|AIza|bot[0-9]+:" --include='*.py' --include='*.ts' --include='*.js' .
cat .env 2>/dev/null
ls -la **/.env* 2>/dev/null
```
Запиши все найденные места. Это будет твой migration checklist.

### Шаг 2 — Поставить себе helper

```bash
# на узле где разрабатываешь (sdev и/или hoster):
ls -la /opt/klod_keymaster.py     # должен существовать (Klod-Access уже задеплоил)
python3 /opt/klod_keymaster.py --manifest ESHKOLA_DEEPSEEK    # smoke
```

В Python `sys.path`:

```python
import sys; sys.path.insert(0, "/opt")
from klod_keymaster import get, start_rotation_watcher
```

Или скопируй `klod_keymaster.py` в свой пакет (это всего 200 строк, без зависимостей).

### Шаг 3 — Pre-approve у Бори

Сам Боря (или ты через скрипт от его имени) делает на smain один раз:

```bash
for name in ESHKOLA_DEEPSEEK ESHKOLA_GEMINI ESHKOLA_TGBOT; do
  curl -X POST "http://127.0.0.1:9093/keymaster/pre_approve?name=$name&requester=eshkola@sdev"
  curl -X POST "http://127.0.0.1:9093/keymaster/pre_approve?name=$name&requester=eshkola@hoster"
done
```

После этого `klod_keymaster.get()` от `eshkola@sdev` и `eshkola@hoster` будет идти мгновенно, без TG-аппрува на каждый рестарт.

### Шаг 4 — Заменить чтение секретов в коде

Заменяй точечно, по одному месту за раз. Каждое:
- `os.environ["ESHKOLA_DEEPSEEK"]` → `deepseek_key()` из `eshkola/keys.py`.
- Hardcoded строка → удалить.
- Чтение из конфига → удалить из конфига и заменить на функцию.

После каждой замены — проверь `git diff` что значения не закоммитились случайно (`AIza`, `sk-`, `<digits>:<35chars>`).

### Шаг 5 — Удалить локальные .env

```bash
# на каждом узле где живёт ЭШкола
cd ~/workspaces/eshkola
mv .env ~/secure/eshkola-env.bak-$(date +%s)   # на случай если что-то ещё читает — пока сохрани в ~/secure (700)
chmod 600 ~/secure/eshkola-env.bak-*
grep "^\.env" .gitignore || echo ".env" >> .gitignore
```

После того как код полностью мигрирует и smoke на hoster покажет что приложение живо без `.env` — удали бэкап.

### Шаг 6 — Smoke на sdev

```bash
cd ~/workspaces/eshkola
unset $(env | grep -oE '^ESHKOLA_[A-Z_]+' | tr '\n' ' ')  # очистить env от старых
python3 -m eshkola.cli ping       # или любой entrypoint, не уйдёт из shell
```

Проверки:
- В Lineman dashboard (https://dashboard.shectory.ru) появились signals от `source_agent=eshkola`.
- `curl -s 'http://10.66.0.1:9093/keymaster/manifest?name=ESHKOLA_DEEPSEEK' | jq .used_by` теперь содержит `eshkola@sdev`.
- В выводе `klod_keymaster.get()` не видно значения (только prefix+len если CLI).

### Шаг 7 — Деплой на hoster

```bash
git push   # код БЕЗ .env, без секретов в коммите
ssh hoster '
  cd ~/workspaces/eshkola && git pull
  rm -f .env   # старого больше нет
  pm2 restart eshkola
'
ssh hoster 'pm2 logs eshkola --lines 30 --nostream'
```

Проверка: в manifest появится `eshkola@hoster` в `used_by`. Никакого ручного `scp .env` между нодами.

## Acceptance criteria (когда задача закрыта)

- [ ] `grep -rE "AIza|sk-[A-Za-z0-9_-]{10,}|bot[0-9]+:[A-Za-z0-9_-]{30,}" ~/workspaces/eshkola/` ничего не выводит.
- [ ] `.env` файлы удалены на всех нодах ЭШколы. В `.gitignore` строка `.env`.
- [ ] `curl /keymaster/manifest?name=ESHKOLA_DEEPSEEK` показывает `used_by: [eshkola@sdev, eshkola@hoster]`.
- [ ] В Lineman dashboard видны signals `source_agent=eshkola` (открой dashboard и сделай 1-2 тестовых LLM-вызова).
- [ ] Все LLM-запросы идут через `/proxy/{deepseek|google}/...`, не напрямую к `api.deepseek.com`/`generativelanguage.googleapis.com`. Проверь: `request_log` в `lineman.db` за час должен показывать `route_applied LIKE 'rproxy:%'` для каждого LLM-вызова ЭШколы.
- [ ] Тест ротации (Боря инициирует один раз): Боря шлёт «прими секрет: ESHKOLA_DEEPSEEK=новое»; в течение 30s `klod_keymaster.get()` начинает возвращать новое значение **без рестарта твоего процесса**.
- [ ] Приложение не падает после рестарта pm2 (значит секреты подтягиваются из Keymaster, а не из мёртвого .env).

## Контакты

- Когда тебе непонятно или что-то сломалось — пиши мне (Клод-Доступ) через `klod_client.complain("eshkola", "сообщение")`. Я увижу твоё сообщение в начале своей сессии вместе с автоматическим triage твоих ошибок из `request_log`.
- Для согласования архитектурных решений — спрашивай Бориса через `curl "http://10.66.0.1:9090/api/agent/main/message?from=eshkola&message=<вопрос>"`.

## Что ты получаешь как «почётный первый клиент»

1. Полная прозрачность: твои запросы видны в dashboard, токены считаются, ошибки логируются с твоим именем.
2. Ротация без даунтайма: Боря меняет ключ, ты ничего не замечаешь.
3. Геобайпас и failover: Lineman сам разруливает iProyal/Proxy6/CF-worker, ты пишешь один URL.
4. Маскирование секретов: даже если ты случайно положишь ключ в request body — Lineman вырежет перед записью в БД.
5. Audit-след: в `~/.keymaster/audit.log` зафиксировано, когда ты впервые получил каждый ключ и от какого узла.

Когда закончишь — отметь это сообщение в [04_incidents.md](../.claude/memory/04_incidents.md) как «closed: 2026-XX-XX, ЭШкола — первый успешный клиент Keymaster по playbook».
