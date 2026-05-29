# Claude Connect Worker — инструкция по развёртыванию

## Назначение
WebSocket TCP-туннель для обхода геоблокировки claude.ai/auth.anthropic.com.
Lineman (smain) подключается по WebSocket к этому воркеру; воркер открывает TCP-сокет к цели через Cloudflare.

## Шаг 1. Деплой через Cloudflare Dashboard

1. Зайди в **dash.cloudflare.com** → Workers & Pages → **Create**
2. Выбери **"Hello World" Worker** → нажми **Deploy**
3. Назови: `claude-connect`
4. Нажми **Edit code** → вставь содержимое `src/index.js`
5. Добавь в начало `wrangler.toml`:
   ```toml
   compatibility_flags = ["nodejs_compat"]
   ```
   _(нужно для cloudflare:sockets)_
6. **Save and Deploy**

Воркер получит URL: `https://claude-connect.bshevelev75.workers.dev`

## Шаг 2. Проверка

```bash
# Должен вернуть "Claude Connect Tunnel"
curl https://claude-connect.bshevelev75.workers.dev

# Тест WebSocket-туннеля
curl -x http://localhost:9090 -s -o /dev/null -w "Proxy: %{http_connect}\nHTTP: %{http_code}\n" --max-time 10 https://claude.ai/
```

## Шаг 3. Включить в Lineman

В `~/workspaces/lineman/config.json`, найти:
```json
{
  "id": "claude-connect",
  ...
  "enabled": false
}
```
Поменять на `"enabled": true`, затем:

```bash
systemctl --user restart lineman.service
```

## Деплой через wrangler CLI (альтернатива)

```bash
cd ~/workspaces/lineman/cloudflare-worker/claude-connect-worker
HTTPS_PROXY=http://g3FLjE:v5aJS3@45.155.200.232:8000 npx wrangler login
HTTPS_PROXY=http://g3FLjE:v5aJS3@45.155.200.232:8000 npx wrangler deploy
```
