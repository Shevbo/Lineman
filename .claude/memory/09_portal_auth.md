# Shectory Portal — единый каталог пользователей federation

Единая учётка `bshevelev@mail.ru` (роль `admin`) живёт в Postgres `portal_users` на hoster (`83.69.248.175`) и проверяется через portal-bridge.
Lineman гейтит dashboard.shectory.ru через этот каталог — нет локальных htpasswd / boris.

**ОБНОВЛЕНО 2026-06-06:** Basic-popup заменён на **брендированный экран входа** (cookie-сессия).
Поток: нет сессии → nginx `302 /login` → фирменная страница (`dashboard/login.html`, гифка Shectory)
→ `POST /api/login` (verify через bridge → HttpOnly cookie `shectory_session`, HMAC) →
nginx `auth_request /api/session-check` валидирует cookie. Эндпоинты: `GET /login`,
`POST /api/login`, `GET /api/session-check`, `POST /api/logout`. nginx-конфиг —
`nginx/dashboard.shectory.ru.conf` (применён). Basic (`/api/portal-auth-check`) оставлен
как defence-in-depth fallback для прямого доступа к :9090. Диаграмма ниже — историческая (Basic).

## Архитектура auth

```
браузер → https://dashboard.shectory.ru/api/klod-chat
   ↓ Basic Auth (bshevelev@mail.ru:***)
nginx :443 (sites-available/dashboard.shectory.ru)
   ├─ auth_request → /_portal_auth (internal)
   │     proxy_pass http://127.0.0.1:9090/api/portal-auth-check
   │     forwards Authorization: Basic ...
   ├─ on 200 → проксирует исходный запрос → Lineman :9090/dashboard или /api/...
   └─ on 401 → @portal_login → WWW-Authenticate: Basic realm="Shectory Portal"
                                                         
Lineman /api/portal-auth-check
   ↓ POST http://127.0.0.1:3000/api/internal/verify-portal-credentials
   ↓ Authorization: Bearer $SHECTORY_AUTH_BRIDGE_SECRET, body {email, password}
shectory-portal.service (Next.js на :3000)
   ↓ bcrypt-check Postgres portal_users
```

## Ключевые файлы

| Где | Что |
|-----|-----|
| `proxy_server.py` `_read_headers`, `_parse_basic_auth`, `_verify_portal_credentials`, `_send_401_basic` | helpers Basic Auth |
| `proxy_server.py` `/api/portal-auth-check` | endpoint для nginx auth_request |
| `proxy_server.py` `/klod-chat`, `/api/klod-chat` | defence-in-depth: in-app Basic Auth (на случай прямого доступа к Lineman :9090) |
| `/etc/nginx/sites-available/dashboard.shectory.ru` | auth_request + @portal_login |
| `~/keymaster/.lineman-proxy.env` | `SHECTORY_PORTAL_URL`, `SHECTORY_AUTH_BRIDGE_SECRET` |
| `tests/test_portal_auth.py` | unit-тесты helpers |

## Env-переменные

| Имя | Назначение | Значение хранится |
|-----|-----------|-------------------|
| `SHECTORY_PORTAL_URL` | base url portal Next.js | `~/keymaster/.lineman-proxy.env` |
| `SHECTORY_AUTH_BRIDGE_SECRET` | Bearer-токен bridge | `~/keymaster/.lineman-proxy.env` (источник: `shectory-portal/.env`) |

Никогда не писать значение секрета в код/мемори.

## Кэш

Положительные проверки кэшируются in-memory (`self._portal_auth_cache`) на 5 минут (`_portal_auth_ttl`). Ключ — `sha256(email_lower:password)`. Cap 256 записей. Кэш сбрасывается перезапуском Lineman.

## Что НЕ изменили (и почему)

- Reverse-proxy `/proxy/{provider}/...` — не гейтим, это рабочая поверхность LLM-проксирования, идёт от агентов с WG/cookie контекстом, не от браузеров. Не ломать.
- Forward proxy CONNECT :9090 — без auth (так и было).
- Federation-only API эндпоинты (`/api/agent/<id>/message`, `/api/signal*`, и т.д.) — на 127.0.0.1:9090 без auth остаются, агенты бьют напрямую/через WG; auth только когда трафик идёт через nginx с Host `dashboard.shectory.ru`.

## Канон федерации (Klod-Access владеет стандартом)

Это Lineman-реализация общего стандарта. Канон + чеклист подключения для ЛЮБОГО сервиса федерации:
[`docs/PORTAL_AUTH_STANDARD.md`](../../docs/PORTAL_AUTH_STANDARD.md). Туда же направляет
Keymaster-онбординг (раздел 5.6) и авто-ответчик klod-access (KLOD_CHEATSHEET). Источник истины
по принципам/RBAC — портальный `CursorRPA/docs/unified-auth-users-rbac-ru.md`.

## Связь с другими memory

- [03_critical_paths.md](03_critical_paths.md) — добавить `/api/portal-auth-check` и nginx auth_request в список того, что нельзя ломать (если выпадет — потеряем доступ ко всему дашборду).
- [02_operations.md](02_operations.md) — рестарт Lineman теперь должен передавать env `SHECTORY_AUTH_BRIDGE_SECRET` (через `start.sh` → `keymaster/.lineman-proxy.env`).

## Деградация / failure modes

- Portal :3000 лежит → `_verify_portal_credentials` ловит exception → возвращает False → 401. Дашборд недоступен пока portal не поднимется. Это намеренно: лучше 401 чем bypass.
- bridge secret отсутствует в env → лог `portal_auth_no_secret` → всегда 401.
- Кэш-хит позволяет залогиненному юзеру пользоваться dashboard в окне 5 мин даже если portal моргнул.
