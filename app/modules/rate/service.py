"""Rate service: rate plans + per-night prices and restrictions.

Prices fall back to `unit_type.base_price` when a rate plan has no explicit
`price_day` row for a date. This keeps the partner cabinet usable before they
have set up full seasonal pricing.
"""

from __future__ import annotations

import datetime as dt

import asyncpg

from app.config import legal
from app.utils.logger import get_logger
from app.utils.redis import get_redis

log = get_logger(__name__)

PRICE_CACHE_TTL_SEC = 60
_PRICE_KEY = "prices:{rate_plan_id}:{from_iso}:{to_iso}"


class ActivePlanExists(Exception):
    """The unit type already has an active rate plan (one is allowed)."""


async def create_rate_plan(
    conn: asyncpg.Connection,
    unit_type_id: str,
    name: str,
    cancellation_policy: str = "flexible",
) -> dict:
    try:
        # Own savepoint: a rejected insert must not abort the caller's transaction.
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO rate_plan (unit_type_id, name, cancellation_policy)
                VALUES ($1, $2, $3)
                RETURNING id::text, unit_type_id::text, name, cancellation_policy,
                          active, commission_rate::float8
                """,
                unit_type_id,
                name,
                cancellation_policy,
            )
    except asyncpg.UniqueViolationError as exc:
        raise ActivePlanExists(unit_type_id) from exc
    assert row is not None
    return dict(row)


async def list_rate_plans(conn: asyncpg.Connection, unit_type_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT id::text, unit_type_id::text, name, cancellation_policy, active,
               commission_rate::float8
        FROM rate_plan WHERE unit_type_id = $1 ORDER BY created_at
        """,
        unit_type_id,
    )
    return [dict(r) for r in rows]


async def set_prices(
    conn: asyncpg.Connection,
    rate_plan_id: str,
    date_from: dt.date,
    date_to: dt.date,
    price: float,
    min_stay: int = 1,
) -> int:
    """Set the price for every night in [date_from, date_to).

    Existing rows are updated, missing ones created — so a partner can change
    a whole season in one call. Returns the number of affected nights.
    """
    if date_to <= date_from:
        raise ValueError("date_to must be after date_from")
    if price < 0:
        raise ValueError("price must be >= 0")
    if min_stay < 1:
        raise ValueError("min_stay must be >= 1")

    row = await conn.fetchrow(
        """
        WITH d AS (
            SELECT generate_series($1::date, ($2::date - interval '1 day')::date, '1 day')::date AS date
        )
        INSERT INTO price_day (rate_plan_id, date, price, min_stay)
        SELECT $3, d.date, $4, $5 FROM d
        ON CONFLICT (rate_plan_id, date) DO UPDATE
            SET price = EXCLUDED.price, min_stay = EXCLUDED.min_stay
        RETURNING 1
        """,
        date_from,
        date_to,
        rate_plan_id,
        price,
        min_stay,
    )
    assert row is not None
    await _invalidate(rate_plan_id)

    # Channels need to know a price changed so they can re-sync their tariffs.
    from app.modules.outbox import service as outbox_service

    property_id = await outbox_service.property_of_rate_plan(conn, rate_plan_id)
    if property_id is not None:
        await outbox_service.emit(
            conn,
            aggregate="rate_plan",
            aggregate_id=rate_plan_id,
            event_type=outbox_service.RATE_PRICES_CHANGED,
            payload={
                "rate_plan_id": rate_plan_id,
                "unit_type_id": await conn.fetchval(
                    "SELECT unit_type_id::text FROM rate_plan WHERE id = $1", rate_plan_id
                ),
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "price": price,
                "min_stay": min_stay,
            },
            property_id=property_id,
        )
    return 1


async def get_prices(
    conn: asyncpg.Connection,
    rate_plan_id: str,
    date_from: dt.date,
    date_to: dt.date,
) -> list[dict]:
    """Per-night prices over [date_from, date_to).

    Falls back to unit_type.base_price where no explicit price row exists.
    """
    if date_to <= date_from:
        raise ValueError("date_to must be after date_from")

    rows = await conn.fetch(
        """
        SELECT
            d.date,
            COALESCE(pd.price::float8, ut.base_price::float8) AS price,
            COALESCE(pd.min_stay, 1)                          AS min_stay,
            COALESCE(pd.max_stay, 0)                          AS max_stay,
            COALESCE(pd.cta, true)                            AS cta,
            COALESCE(pd.ctd, true)                            AS ctd,
            COALESCE(pd.stop_sell, false)                     AS stop_sell
        FROM generate_series($2::date, ($3::date - interval '1 day')::date, '1 day') AS d(date)
        LEFT JOIN price_day pd ON pd.rate_plan_id = $1 AND pd.date = d.date
        LEFT JOIN rate_plan  rp ON rp.id = $1
        LEFT JOIN unit_type  ut ON ut.id = rp.unit_type_id
        ORDER BY d.date
        """,
        rate_plan_id,
        date_from,
        date_to,
    )
    return [dict(r) for r in rows]


async def get_effective_rate(
    conn: asyncpg.Connection,
    unit_type_id: str,
) -> tuple[str | None, float]:
    """Commission share that applies to a unit type, with its source.

    An active rate plan carrying ``commission_rate`` wins; otherwise the
    platform default from ``app.config.legal``. Returns the rate plan id (None
    when the default applied) so the caller can report which rule priced the
    booking, the same way prices report their plan.
    """
    row = await conn.fetchrow(
        """
        SELECT rp.id::text, rp.commission_rate::float8
        FROM rate_plan rp
        WHERE rp.unit_type_id = $1 AND rp.active
        ORDER BY rp.created_at
        LIMIT 1
        """,
        unit_type_id,
    )
    if row is not None and row["commission_rate"] is not None:
        return row["id"], row["commission_rate"]
    return (row["id"] if row is not None else None), legal.COMMISSION_DEFAULT_RATE


async def update_rate_plan(
    conn: asyncpg.Connection,
    rate_plan_id: str,
    commission_rate: float | None,
) -> dict:
    """Set the commission share of a rate plan.

    ``None`` clears the override and falls the unit type back to the platform
    default. The rate is read at hold time and snapshotted on the booking, so
    this never reprices a booking already confirmed.
    """
    if commission_rate is not None and not (0 <= commission_rate <= 1):
        raise ValueError("commission_rate must be between 0 and 1")

    row = await conn.fetchrow(
        """
        UPDATE rate_plan
           SET commission_rate = $2
         WHERE id = $1
        RETURNING id::text, unit_type_id::text, name, cancellation_policy,
                  active, commission_rate::float8
        """,
        rate_plan_id,
        commission_rate,
    )
    if row is None:
        raise KeyError(rate_plan_id)
    return dict(row)


async def get_effective_price(conn: asyncpg.Connection, rate_plan_id: str, date: dt.date) -> float:
    """Single-night price with fallback, used by availability."""
    row = await conn.fetchrow(
        """
        SELECT COALESCE(pd.price::float8, ut.base_price::float8) AS price
        FROM rate_plan rp
        LEFT JOIN unit_type ut ON ut.id = rp.unit_type_id
        LEFT JOIN price_day pd ON pd.rate_plan_id = $1 AND pd.date = $2
        WHERE rp.id = $1
        """,
        rate_plan_id,
        date,
    )
    return float(row["price"]) if row else 0.0


async def _invalidate(rate_plan_id: str) -> None:
    try:
        redis = get_redis()
        async for key in redis.scan_iter(match=f"prices:{rate_plan_id}:*"):
            await redis.delete(key)
    except RuntimeError:
        pass  # Redis not initialised (unit tests)
    except Exception as exc:
        log.warning("price-cache-invalidate-failed", error=str(exc))
