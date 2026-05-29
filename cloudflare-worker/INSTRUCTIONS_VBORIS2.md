# Инструкция для VBoris2: Cloudflare Worker для Gemini API

## Цель
Создать Cloudflare Worker, который проксирует запросы к Gemini API (generativelanguage.googleapis.com), обходя геоблокировку РФ. Текущий прокси Proxy6 режет Gemini Pro.

## Шаг 1. Cloudflare Dashboard → Workers & Pages
- Зайди в dash.cloudflare.com → раздел **Workers & Pages**
- Создай новый Worker (кнопка Create)
- Назови: `gemini-proxy`

## Шаг 2. Код Worker
Замени код на этот (в веб-редакторе или через CLI):

```js
addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  const url = new URL(request.url);
  url.host = 'generativelanguage.googleapis.com';

  const newRequest = new Request(url.toString(), {
    method: request.method,
    headers: request.headers,
    body: request.body,
    redirect: 'follow'
  });

  return fetch(newRequest);
}
```

## Шаг 3. Деплой
- Нажми «Save and Deploy»
- Worker получит URL вида: `https://gemini-proxy.твой-субдомен.workers.dev`
- **Запиши этот URL** — он понадобится дальше

## Шаг 4. Проверка Worker
```bash
curl "https://gemini-proxy.твой-субдомен.workers.dev/v1beta/models?key=GEMINI_API_KEY"
```
Должен вернуть список моделей Gemini (не ошибку геоблокировки).

## Шаг 5. Обновить конфиг Lineman на smain
На сервере shectory-work (83.69.248.77), файл:
`~/workspaces/lineman/config.json`

В секцию `global` добавить:
```json
"gemini_cf_proxy_url": "https://gemini-proxy.твой-субдомен.workers.dev",
```

## Шаг 6. Обновить proxy_server.py
В функции `handle_request` (файл `~/workspaces/lineman/proxy_server.py`), найти строки где определяется `use_proxy` для Google-хостов и заменить на:

```python
# Gemini API → Cloudflare Worker; остальные Google API → обычный прокси
if host == "generativelanguage.googleapis.com":
    use_proxy = global_cfg.get("gemini_cf_proxy_url") or proxy_url
elif host in ("www.googleapis.com", "gmail.googleapis.com", "docs.googleapis.com"):
    use_proxy = proxy_url or None
```

## Шаг 7. Перезапуск Lineman
```bash
systemctl --user restart lineman.service
systemctl --user status lineman.service
```

## Шаг 8. Финальная проверка
```bash
curl -x http://127.0.0.1:9090 \
  "https://generativelanguage.googleapis.com/v1beta/models?key=GEMINI_API_KEY"
```
Должен пройти успешно.

---
**Ключевые файлы на smain:**
- Worker код (запасной): `~/workspaces/lineman/cloudflare-worker/gemini-proxy-worker/`
- Конфиг Lineman: `~/workspaces/lineman/config.json`
- Прокси-сервер: `~/workspaces/lineman/proxy_server.py`

**Контакты если что-то не работает:**
- Танк (smain): через скрипт на vibe или Telegram
- GEMINI_API_KEY: лежит в `~/.openclaw/openclaw.json` → models.providers.google.apiKey
