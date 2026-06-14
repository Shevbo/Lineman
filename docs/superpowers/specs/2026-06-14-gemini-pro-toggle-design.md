# Кнопка "Gemini Pro" для агентов — дизайн

Дата: 2026-06-14. Автор: Клод (главный инженер Lineman). Заказчик: Борис.

## Цель

Контролируемая, пер-агентная выдача доступа к Gemini Pro. Боря из ТГ-бота агента (Медсестра /
Карьера / Титан) одной кнопкой включает Pro этому агенту на ограниченное окно. По истечении окна
Lineman сам возвращает агента на дешёвую базовую модель. Pro-квота (2.5-pro = 1K RPD, 3.1-pro =
250 RPD) тратится только теми, кому Боря явно разрешил.

Lineman — единственный держатель ключа Gemini и точка прохода всего google-трафика
(`X-Agent-Name`), поэтому enforcement живёт в нём. Кнопки в ботах только дёргают его API.

## Политика моделей

| Состояние агента | Запрос агента | Что уходит в Google |
|---|---|---|
| Pro ВЫКЛ (нет гранта) | любой `*-pro` | **gemini-3.5-flash** (база) |
| Pro ВЫКЛ | не-pro (flash и т.п.) | как есть |
| Pro ВКЛ (грант активен) | `gemini-2.5-pro` | gemini-2.5-pro |
| Pro ВКЛ | `gemini-3.1-pro` | **gemini-2.5-pro** (3.1→2.5 всегда, беречь 250 RPD) |
| Pro ВКЛ | не-pro | как есть |

Правило 3.1-pro → 2.5-pro **глобальное** (действует и при ВКЛ): 3.1-pro слишком скуден (250 RPD).
Существующий guard переиспользуется.

## Компоненты

### 1. Стор грантов
Рантайм-файл `gemini_pro_grants.json` рядом с lineman.db: `{ "<agent>": <expires_at_epoch>, ... }`.
Не config.json — чтобы нажатие кнопки не триггерило lineman-guard и не требовало рестарта.
Модуль `gemini_pro.py`: `grant(agent, hours)`, `revoke(agent)`, `is_pro(agent) -> bool`
(учитывает истечение), `status() -> {agent: remaining_seconds}`. Атомарная запись (tmp+rename).
Кэш в памяти + mtime-перечитка, чтобы reverse_proxy не читал файл на каждый запрос.

### 2. API Lineman
Добавить в proxy_server роутинг (как у `/api/backlog`):
- `GET  /api/gemini-pro` → `{grants:[{agent,remaining_min}], default_hours}`
- `POST /api/gemini-pro/grant`  `{agent, hours?}` → ставит грант (hours по умолчанию из config)
- `POST /api/gemini-pro/revoke` `{agent}` → снимает грант

Доступ: только локалхост (как остальные /api). Авторизация «только Боря» обеспечивается на стороне
бота (проверка tg_id), т.к. к Lineman ходит сам бот.

### 3. Enforcement в reverse_proxy
В обеих точках обработки google (passthrough + main), после парсинга `rest_path`/модели:
- если в пути pro-модель и `not gemini_pro.is_pro(agent_name)` → переписать модель на `gemini-3.5-flash`
- глобально: `gemini-3.1-pro*` → `gemini-2.5-pro` (существующий guard, остаётся)
- логировать факт даунгрейда (`gemini_pro_downgrade`, agent, from, to)

Порядок: сначала 3.1→2.5 (глобально), затем пер-агентный даунгрейд pro→база.

### 4. Кнопка в ТГ-ботах
В Медсестре, Карьере (career-bot), Титане — inline-кнопка:
- ВЫКЛ: `🧠 Gemini Pro: ВЫКЛ (3.5-flash)` → тап показывает выбор 1ч / 3ч / 6ч
- ВКЛ: `🧠 Gemini Pro: ВКЛ до HH:MM (2.5-pro)` → тап = revoke
Callback проверяет `tg_id == BORIS_TG_ID`; чужой → «Pro рулит только Боря». Дёргает API Lineman.

### 5. Фикс идентичности агентов
Веб-аппы gemini-live-service (titan/nurse/career) сейчас все шлют `X-Agent-Name=gemini-live-nurse`.
Чтобы гейтить раздельно — `llm.py`/`tts.py` берут имя из конфигурации агента (titan/nurse/career),
а не хардкод. Без этого пер-агентный гейт не различит их.

## Поток
Боря жмёт кнопку → бот (проверив tg_id) POST `/api/gemini-pro/grant {agent, hours}` → Lineman пишет
грант в стор → следующие запросы агента к Pro проходят с 2.5-pro → по истечении окна `is_pro` даёт
False → запросы Pro автоматически уходят на 3.5-flash. Рестарты не нужны.

## N часов
`config.json → gemini_pro.default_hours` (дефолт 3). Кнопка предлагает 1/3/6ч.
`config.json → gemini_pro.base_model` (дефолт `gemini-3.5-flash`),
`gemini_pro.pro_model` (дефолт `gemini-2.5-pro`).

## Тестирование (pytest)
- `gemini_pro.grant/revoke/is_pro/status` + истечение по времени (мокать time).
- enforcement: грант есть → 2.5-pro проходит; нет → pro переписан на 3.5-flash; 3.1-pro → 2.5-pro
  всегда; не-pro не тронут.
- API grant/revoke/get round-trip.
- Стор: атомарная запись, перечитка по mtime.

## Вне scope
- Авто-выдача Pro без Бори. Только ручной грант.
- UI помимо кнопки (дашборд-вкладка — потом, опционально читает `GET /api/gemini-pro`).
- Гейтинг не-google провайдеров.

## Риски
- reverse_proxy + config.json правки триггерят lineman-guard (алерт Боре) — ожидаемо.
- Стор-файл повреждён → `is_pro` должен фейлиться в False (безопасно: база, не Pro).
