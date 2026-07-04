# Стандарт аутентификации пользователей — Shectory Portal (единая база учёток)

Владелец стандарта: **Klod-Access** (Lineman/Keymaster). Этот документ — точка входа для
любого агента, который делает приложение/сервис с **входом пользователей**. Не изобретай
свой логин и свою таблицу паролей — используй единый каталог.

> Статус: проверено по коду 2026-06-06 (портал `route.ts`, Lineman `proxy_server.py`,
> `ourdiary/RUNBOOK.md`). Trader-специфика (HMAC-сессии) взята из описания владельца trader
> и **не** перепроверена построчно — помечена `[consumer]`.

## Принцип (одна фраза)

**Источник истины по пользователям — центральный Shectory Portal (таблица `portal_users`).
Бэкенды НЕ хранят пароли — они проверяют их у портала через bridge, и на успехе выдают свой
подписанный сессионный токен.** Запрещено заводить автономные каталоги пользователей в
прикладных проектах (см. портальный контракт ниже).

## Брендированный экран входа (ОБЯЗАТЕЛЕН — не сырой Basic popup)

Логин-экран **любого** приложения Shectory должен быть единым, фирменным и профессиональным.
**Запрещён** браузерный Basic Auth popup как лицо входа. Обязательная композиция (стандарт
портала `docs/welcome-page-standard-ru.md` + шаблон `templates/shectory-welcome-frame/`):

- большой информационный фрейм проекта (кастомный HTML/CSS);
- **слева сверху — логотип Shectory** (анимированная гифка `shectory-portal/public/brand/shectory-logo.gif`,
  публично `https://shectory.ru/brand/shectory-logo.gif`);
- справа сверху — логотип проекта + версии модулей;
- унифицированная область логина: email + пароль, понятные ошибки, **HttpOnly cookie-сессия**,
  ссылки на восстановление пароля (портал `https://shectory.ru/login`) и поддержку.

Эталон-реализация (Next.js): `shectory-portal/src/app/login/page.tsx`.
Эталон для не-портального бэкенда (статический HTML + cookie через bridge): дашборд Lineman —
`dashboard/login.html` + эндпоинты `GET /login` / `POST /api/login` / `GET /api/session-check`
/ `POST /api/logout` (см. [WIKI.md](../WIKI.md#аутентификация--стандарт-shectory-portal-bridge)).
nginx делает `auth_request` на session-check, на 401 — `302 /login` (не Basic-realm).

## Bridge-контракт (проверка пароля у портала)

```
POST {SHECTORY_PORTAL_URL}/api/internal/verify-portal-credentials
Authorization: Bearer {SHECTORY_AUTH_BRIDGE_SECRET}
Content-Type: application/json
{"email": "<полный email>", "password": "<пароль>"}
```

Портал (`shectory-portal/src/app/api/internal/verify-portal-credentials/route.ts`):
- `SHECTORY_AUTH_BRIDGE_SECRET` не задан на портале → **503** `Bridge not configured`.
- `Authorization` != `Bearer <secret>` → **403** `Forbidden` (рассинхрон секрета).
- нет `email`/`password` → **400**.
- email нормализуется `trim().toLowerCase()`; `portalUser.findUnique` → нет юзера → **401**.
- `bcrypt.compare(password, passwordHash)` не сошёлся → **401**.
- успех → **200** `{ ok: true, email, role, fullName }`. Роли: `user|admin|superadmin`.

Только **полный email** как логин (каталог не знает «локальную часть»). Роль `superadmin`
привязана к email владельца в таблице `portal_users` (конкретный адрес — в Keymaster
`OWNER_PORTAL_EMAIL`, не хардкодить в коде/доках). Роль приходит из каталога, не из конфига.

## Конфиг (env, ВЕРХНИЙ_РЕГИСТР)

| Переменная | Назначение | Примечание |
|---|---|---|
| `SHECTORY_PORTAL_URL` | база портала | прод `https://shectory.ru`; на smain локально `http://127.0.0.1:3000` |
| `SHECTORY_AUTH_BRIDGE_SECRET` | общий секрет с порталом | **то же значение** в `.env` портала и у потребителя. В чат/лог/коммит не печатать. Авто-ротируется как `*AUTH_BRIDGE*` (см. leak-протокол Keymaster) |
| `SHECTORY_LOGIN_EMAIL_DOMAIN` | `[consumer]` достроить bare-login → email | напр. домен `example.com`: `user` → `user@example.com` |
| `SHECTORY_LOCAL_USER_EMAIL` / `SHECTORY_LOCAL_USER_PASSWORD_SHA256` | `[consumer]` break-glass | один аварийный юзер, только когда портал недоступен/не-200; sha256-hex, сравнение `hmac.compare_digest` |
| `AUTH_DEBUG` | явное включение debug-входа | `1` + непрод-признак (см. ниже). По умолчанию НЕ задан = debug выключен |

### Пустой секрет = отказ (fail-closed, ОБЯЗАТЕЛЬНО)

**Секрет `SHECTORY_AUTH_BRIDGE_SECRET` пуст или не задан → любой вход отклоняется (503/401), НЕ пропускать.**
Раньше стандарт разрешал «пустой секрет → debug-пользователь» — это fail-open: незаданная
переменная окружения открывала вход кому угодно. Отменено 2026-07-04.

Debug-вход (обход bridge для локальной разработки) допустим ТОЛЬКО при одновременно:
1. `AUTH_DEBUG=1` задан явно, И
2. непрод-признак: `SHECTORY_ENV` ∈ {`dev`,`local`,`test`} ИЛИ хост биндится на `127.0.0.1`/loopback.

Если `AUTH_DEBUG=1` выставлен, но окружение выглядит продовым (`SHECTORY_ENV=prod` или публичный
бинд) — потребитель обязан игнорировать debug и работать fail-closed. Эталон (Lineman): при пустом
секрете `_verify_portal_credentials` возвращает `False`, `_verify_session_token` → `None` (вход закрыт).
Портал при пустом секрете отвечает **503**, не пропуская проверку.

## Сессия у потребителя (после успешного verify)

Портал не отдаёт свою cookie. Потребитель выпускает **свой** сессионный токен:
- **Lineman** (dashboard, `_verify_portal_credentials`): positive-cache на 300с по
  `sha256(email:password)`; nginx `auth_request` дергает `/api/portal-auth-check` на каждый запрос.
- **ourdiary** (проверено, `RUNBOOK.md` «Единый вход Shectory»): verify у портала → `upsert`
  пользователя в свою БД (роль `superadmin→SUPERADMIN`, `admin→ADMIN`, иначе `MEMBER`). Локальный
  `passwordHash` в БД дневника — fallback **только** для учёток, которых нет в `portal_users`
  (семейные аккаунты без дублирования в портале).
- **trader** `[consumer]`: HMAC-токен `email:expires:HMAC_SHA256(...)` (TTL ~30д) в httponly-cookie
  `shectory_session`; guard читает cookie/`Authorization: Bearer`/WS `?token=`.

Общее правило: локальный пароль допустим ТОЛЬКО для учёток, отсутствующих в `portal_users`.
Для портальных пользователей источник истины — портал.

## Как подключить свой сервис (чек-лист)

1. Получи `SHECTORY_AUTH_BRIDGE_SECRET` через Keymaster (`klod_keymaster.get`), то же значение
   что в `.env` портала. Не клади на диск/в дамп PM2.
2. Сделай **брендированный `/login`** по стандарту welcome-экрана (см. раздел выше), форма шлёт
   email + пароль на свой backend → bridge-запрос. НЕ используй сырой Basic popup как вход.
3. 200 `{ok:true}` → выпусти свою сессию (HttpOnly cookie/token). Не-200 → отказ (или break-glass, если задан).
4. Username для пользователя — его **email портала** (не выдуманный логин). Заведение/сброс пароля —
   только в портале (`portal_users`, bcrypt), не у себя.
5. Next.js+Prisma и нужен полноценный in-app RBAC (users+profiles, NextAuth) — см. портальный
   контракт ниже, а не bridge.

## Источник истины и примеры (НЕ дублировать — ссылаться)

- **Портальный контракт + принципы (native NextAuth, users/profiles, RBAC):**
  `CursorRPA/docs/unified-auth-users-rbac-ru.md` + федеративная вики
  `CursorRPA/docs/shectory-wikipedia.md` (раздел «Единая аутентификация»).
- **Эталон bridge-потребителя:** `ourdiary/RUNBOOK.md` → «Единый вход Shectory».
- **Реализация портала:** `shectory-portal/src/app/api/internal/verify-portal-credentials/route.ts`,
  модель `portal_users` (`prisma/schema.prisma` → `model PortalUser`).
- **Реализация потребителя (Lineman dashboard):** `proxy_server.py` →
  `_verify_portal_credentials` / `/api/portal-auth-check` + nginx `auth_request`
  (`/etc/nginx/sites-available/dashboard.shectory.ru`). Подробно — [WIKI.md](../WIKI.md#аутентификация--стандарт-shectory-portal-bridge).

## Частые грабли

- «Пароль не подходит», хотя верный → проверь **username = полный email портала**, не старый
  локальный логин (`boris` и т.п. — мёртвые htpasswd-схемы).
- Все входы падают при верном пароле → **403** на bridge = рассинхрон `SHECTORY_AUTH_BRIDGE_SECRET`
  между порталом и потребителем; **503** = секрет не задан на портале.
- 401 на всех → юзера нет в `portal_users` (создать в портале) или bcrypt-хэш не тот.
- bare-login без `@` → задай `SHECTORY_LOGIN_EMAIL_DOMAIN` (каталог принимает только полный email).
