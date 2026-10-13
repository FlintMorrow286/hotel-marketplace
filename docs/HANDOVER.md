# Handover — контекст для нового чата

**Проект:** B2B2C маркетплейс бронирования жилья (отели, квартиры, апарты, комнаты).
Партнёры заводят объекты в нашем кабинете, гости бронируют и платят **у нас**,
комиссия с подтверждённой брони. Гео — Крым. Instant book, 100% предоплата,
отмена бесплатна за сутки до заезда.

**Source of truth — наша PostgreSQL.** Внешние площадки (Avito, Суточно.ру,
Яндекс, Airbnb) — каналы через адаптеры (Phase 2+), не главный календарь.
Double booking невозможен по архитектуре.

## Стек

- Бэк: Python 3.12, FastAPI, Uvicorn, asyncio
- PostgreSQL 16 + **asyncpg** (без ORM, чистый SQL), ручные `.sql` миграции
  (ранер `app/db/migrate.py`, состояние в `schema_migrations`)
- Redis (кэш доступности/цен), Pydantic v2, JWT, structlog, aiogram 3
- Фронт: React 18 + TS + Vite + Tailwind 4, Lucide-иконки, TanStack Query.
  **Single-app: FastAPI отдаёт собранную сборку** (не отдельный сервер)
- Деплой self-hosted; локально postgres@16 и redis через Homebrew запущены

## Что готово

**Бэкенд, Phase 1 (срезы 0–6) — основа, всё зелёное:**

- 0 — skeleton, JWT (scopes partner/admin), healthz/readyz, пул asyncpg, рипер
- 1 — партнёр + property CRUD, публичный каталог (только published)
- 2 — генерация inventory_day на 730 дней, `GET /availability`, закрытие дат, unit-types
- 3 — **ядро бронирования**: `POST /bookings/hold`, SERIALIZABLE + `FOR UPDATE`
  по диапазону дат, hold TTL 15 мин, idempotency-key, рипер протухших hold.
  Тест: 20 параллельных hold на 1 номер → ровно 1 success
- 4 — оплата: `PaymentProvider` (stub/fail/tinkoff), hold→sold→confirmed
  одной транзакцией, refund
- 5 — rate plans, цены по дням (fallback на base_price), `GET /partner/calendar` (шахматка)
- 6 — правило отмены (24ч), комиссия (accrued/void), нотификации (email логируется
  + aiogram Telegram), `GET /v1/admin/reports/commission`

**Фронтенд (готов):**

- Лендинг, поиск, карточка объекта, checkout (выбор дат → наличие → посуточный
  прайс → hold → оплата), экран успеха
- Checkout: живой таймер до `hold_expires_at`, состояния «истекла»/«подтверждена»
- Шахматка партнёра: ряды — типы номеров, колонки — даты; состояния ячеек
  свободно/hold/продано/закрыто с иконкой+цветом (WCAG), залипающий заголовок
  строки, горизонтальный скролл, sr-only описание ячеек
- Кабинет партнёра: три состояния (нет объектов / один / много), login gate,
  модалка создания объекта. Пустое состояние объясняет ценность, один доминантный CTA
- Фото объектов: multipart-загрузка в кабинете (full + thumb WebP), обложка в
  каталоге и на странице объекта, удаление
- Базовые компоненты: Button, Input, Badge, Modal, Checkbox
- Панель бронирования: две отдельные галочки (оферта и согласие на обработку ПД)
  со ссылками на документы в новой вкладке, страницы `/legal/:slug` (тексты —
  заглушки до юриста) и ссылки на них в футере
- Оплата при `PAYMENT_MODE=mock_acquiring`: чекаут уводит на страницу оплаты
  «банка», `/checkout/:id/return` спрашивает результат и ведёт на успех, отказ
  или «Проверить ещё раз». Строка «Тестовая оплата» — только если API говорит
  `payment_test_mode` (`GET /v1/config`)
- Выплаты: кабинет партнёра «Выплаты» (остаток, брони за ним, перечисления со строками, CSV)
  и админка «Выплаты» (остатки по партнёрам, черновик → утверждение → отметка с номером
  платёжки). Правила «когда платить» нет: заглушки `PAYOUT_*` в `app/config/legal.py`
- Письма: страницы подтверждения, «Моя бронь» и возврата с оплаты обещают письмо только
  при `email_enabled` (`MAIL_BACKEND=smtp`), иначе просят сохранить код брони; в админке
  страница «Письма» (очередь, текст писем log-бэкенда, «Повторить»)
- Тёмная тема следует `prefers-color-scheme`
- Каталог и шахматка берут реальные данные из API (rate plan + сезонные цены
  показываются в шахматке корректно)

## Запуск

```bash
cd /Users/guuu/Desktop/hotel-marketplace
uv sync --extra bot                           # бэкенд + aiogram (Telegram-бот)
uv run python -m app.db.migrate               # миграции (1..29)
uv run uvicorn app.main:app --port 8002       # API + собранный фронт на :8002
uv run pytest tests/ -q                       # 885 тестов
uv run ruff check app tests && uv run ruff format app tests

cd web && npm run build                       # пересобрать фронт после правок
cd web && npx tsc -b --noEmit                 # проверка типов
```

БД: `hotel_mp`, юзер `hotel_user / hotel_pass`. `.env` создан из `.env.example`.
Сид-данные: демо-партнёр с объектами в Коктебеле (seed чинили — Pydantic
отвергает `.local` в email; перезапуск привязывает существующие объекты
к текущему партнёру).

## API (полный список, все под /v1)

auth: register, login, me
partner: properties (GET/POST/PATCH), unit-types (POST, GET by property),
inventory/generate, inventory/close, rate-plans (POST/GET), prices (POST),
calendar (GET)
public: properties (GET list/detail), availability
booking: hold (header Idempotency-Key; в теле consent — оферта, ПД, версия
документов), GET/{id}, cancel, pay (эквайринг: 202 + payment_url), payment/sync, refund
legal: GET /legal → {docs_version}
payments: POST /payments/notifications/{provider} (подпись провайдера)
site: GET /config → {payment_test_mode, email_enabled}
admin: emails (GET очередь писем, POST {id}/retry), payouts (balances, generate, список, approve/paid/cancel, CSV)
partner: payouts (summary, pending, список, строки, CSV)
admin: reports/commission
healthz, readyz

## Структура

```
hotel-marketplace/
├── migrations/0001..0029*.sql
├── app/modules/{auth,property,inventory,booking,payment,rate,admin,notification,legal,site,payout}/
├── app/{jobs/{reaper,payments,mail}.py, db/{pool,migrate}.py, config/{settings,legal,payment}.py}
├── web/src/{pages/{guest,partner}, components/ui, api/}
├── tests/   (pytest + pytest-asyncio)
└── docs/{ARCHITECTURE.md, LEGAL.md, HANDOVER.md}
```

## Сделано в этой сессии

**Каталог и поиск (гостевой путь):**

- `GET /v1/properties` стал постраничным: `city`, `guests`, `q`, `limit`,
  `offset` + `total`. `guests` оставляет объект, у которого есть *хотя бы один*
  номер вместимостью от N — отель не выпадает из-за того, что его самый
  дешёвый номер одноместный. Объекты без номеров не выпадают из выдачи, но
  имеют `min_price: null` («цена не указана»)
- `min_price` — `min(base_price)` по типам номеров, сразу в выдаче каталога и
  в `GET /v1/properties/{id}`; карточка и страница объекта показывают
  «от N ₽ за ночь»
- Поиск на фронте: дебаунс 300 мс, фильтр гостей, пагинация, серверный
  подсчёт. Форматирование денег — `web/src/utils/format.ts`
- `PATCH /v1/partner/unit-types/{id}`: имя, вместимость, `base_price`.
  `total_units` намеренно нельзя менять через PATCH — это потолок инвентаря,
  под который блокируется бронирование (см. инвариант ниже); пустой body → 422
- Цена номера редактируется прямо в кабинете (`UnitTypePrice`): клик по числу
  → инпут, Enter/blur сохраняет, Esc отменяет

**Харденинг:**

- Невалидный UUID в пути (`/v1/properties/not-a-uuid`) раньше давал 500
  (Postgres rejects → asyncpg `DataError`), теперь 404. Обработчик один на всё
  приложение: `app/utils/uuid_http.py`, ставится в `create_app`. Тест:
  `test_malformed_uuid_path_is_404_not_500`
- Модули тестов, которые открывали свой пул в обход session-фикстуры
  (`test_photos`, `test_ratelimit`, `test_booking`, `test_channel_read`),
  падали при запуске по одному — база `hotel_mp_test` не создавалась.
  Теперь их `world`/direct-connection фикстуры зависят от `_test_db`
 (создаётся и мигрируется один раз за сессию, затем дропается).
  Раньше «случайно работало», потому что другой модуль создавал БТ первым

## Что дальше (по плану)

1. **Phase 2/3:** iCal, каналы, outbox — *сделано ранее*
2. **Сейчас: механика сайта.** Каталог/поиск/цены добиты (см. выше).
   Открытого явного долга нет; естественное продолжение — i18n (отложен
   намеренно) либо следующий сценарий из `docs/ARCHITECTURE.md`.

## Нюансы, которые надо помнить

- Юр/налоги — `docs/LEGAL.md` (паркинг для юриста), в коде константы
  `app/config/legal.py`. Отвечаю только за технику
- Документы перед бронированием: hold без обоих согласий (оферта, ПД) — 422, с
  не текущей `LEGAL_DOCS_VERSION` — 409; версия и время пишутся в
  `booking.docs_version/docs_accepted_at`, у броней каналов NULL. Версию менять
  при любой правке текстов (`web/src/legal/documents.ts` — пока заглушки).
  В тестах тело согласия — `tests/_consent.py`
- Оплата: `PAYMENT_MODE=stub` (мгновенно) или `mock_acquiring` (страница оплаты,
  уведомление, возврат гостя; ключей не нужно, `docs/ARCHITECTURE.md` §4.9).
  Настоящий эквайринг = реализация `HostedPaymentProvider`; чек 54-ФЗ не сделан.
  Брони каналов эквайринг не видят (их деньги собрала OTA)
- Выплаты (`app/modules/payout`, ARCHITECTURE §4.11): строка выплаты — РАЗНИЦА между тем, что бронь
  приносит партнёру сейчас, и уже включённым в утверждённые/выплаченные выплаты; поэтому бронь платится
  один раз, а возврат после выплаты приходит минусом в следующую. Только веб-брони. Правила нет —
  `PAYOUT_*` в `legal.py` (по умолчанию: со дня выезда, без ожидания, без минимума). Модуль не блокирует
  брони и инвентарь. `commission_status` не трогает. Деньги переводят вне системы; реквизитов партнёров
  в системе нет
- Письма: строка в `email_message` пишется в транзакции подтверждения/возврата, отправляет
  джоб `mail` (`docs/ARCHITECTURE.md` §4.10). Пока нет SMTP — `MAIL_BACKEND=log`: ничего не
  уходит, текст писем виден в админке. Письма гостю — только по веб-броням, партнёру — по всем.
  Токен брони в письмо не попадает никогда
- Для эквайринга `APP_BASE_URL` — адрес, на котором гость открыл сайт (оттуда
  строятся страница оплаты и адрес возврата, токен брони живёт в sessionStorage
  вкладки). e2e и CI гоняются на `mock_acquiring`
- Часовой пояс Крыма — `Europe/Simferopol` (не Asia)
- Комиссия снапшотится при подтверждении; отчёты её не пересчитывают
- Пароль в auth — argon2id, как и API-ключи; старые SHA-256 строки
  доживают свой век и пере-хешируются при следующем входе. Секрет
  webhook-подписки опечатан (`WEBHOOK_SEAL_KEY`), без ключа доставка
  не подписывается. Ротация ключа — без даунтайма: исходящий ключ в
  `WEBHOOK_SEAL_KEY_PREVIOUS` + `scripts/rotate_seal_key.py` (idempotent),
  после него PREVIOUS удаляется
- Seed: перезапуск привязывает все объекты к текущему партнёру (фикс от
  предыдущей сессии)
