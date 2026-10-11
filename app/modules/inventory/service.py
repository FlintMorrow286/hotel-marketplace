"""Inventory service.

`inventory_day` is generated N days ahead per unit_type. The availability read
goes to Postgres every time: it is an indexed range read (about a millisecond
for half a year of nights), and a cached copy would show a night as free or
sold out for as long as it lived after a hold, a cancellation or a price change.
"""

from __future__ import annotations

import datetime as dt

import asyncpg

from app.utils.logger import get_logger

log = get_logger(__name__)

# Generate this many days ahead from "today".
DEFAULT_HORIZON_DAYS = 730


async def ensure_inventory(
    conn: asyncpg.Connection,
    unit_type_id: str,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> int:
    """Create missing inventory_day rows up to horizon_days from today.

    Idempotent: existing rows are never touched (uses ON CONFLICT DO NOTHING),
    so it is safe to run repeatedly as a maintenance job.

    Returns the number of inserted rows.
    """
    today = dt.date.today()
    # generate_series is inclusive; subtract a day so the horizon is exact.
    end = today + dt.timedelta(days=horizon_days - 1)

    # Generate the date series in SQL, join against what already exists.
    count = await conn.fetchval(
        """
        WITH d AS (
            SELECT generate_series($1::date, $2::date, '1 day')::date AS date
        ),
        ins AS (
            INSERT INTO inventory_day (unit_type_id, date, available)
            SELECT $3, d.date, ut.total_units
            FROM d
            CROSS JOIN unit_type ut
            WHERE ut.id = $3
              AND NOT EXISTS (
                  SELECT 1 FROM inventory_day i
                  WHERE i.unit_type_id = $3 AND i.date = d.date
              )
            ON CONFLICT (unit_type_id, date) DO NOTHING
            RETURNING 1
        )
        SELECT count(*) FROM ins
        """,
        today,
        end,
        unit_type_id,
    )
    log.info("inventory-ensured", unit_type_id=unit_type_id, inserted=count)
    return count or 0


async def close_range(
    conn: asyncpg.Connection,
    unit_type_id: str,
    date_from: dt.date,
    date_to: dt.date,
    closed: bool = True,
) -> int:
    """Close (stop-sell) a date range. Overwrites existing rows.

    Auto-creates missing inventory rows so partners can close dates even
    before the generator has run.
    """
    if date_to <= date_from:
        raise ValueError("date_to must be after date_from")

    result = await conn.fetch(
        """
        WITH d AS (
            SELECT generate_series($1::date, ($2::date - interval '1 day')::date, '1 day')::date AS date
        ),
        ins AS (
            INSERT INTO inventory_day (unit_type_id, date, available, closed)
            SELECT $3, d.date, ut.total_units, $4
            FROM d CROSS JOIN unit_type ut
            WHERE ut.id = $3
            ON CONFLICT (unit_type_id, date) DO UPDATE
                SET closed = EXCLUDED.closed,
                    -- closing by hand claims the day: an import-closed day the
                    -- partner also stop-sells must survive the feed dropping it
                    closed_source = CASE WHEN EXCLUDED.closed THEN 'manual'
                                         ELSE inventory_day.closed_source END
            RETURNING date
        )
        SELECT date FROM ins
        """,
        date_from,
        date_to,
        unit_type_id,
        closed,
    )
    # A stop-sell change is exactly what a channel syncs availability for.
    from app.modules.outbox import service as outbox_service

    property_id = await outbox_service.property_of_unit_type(conn, unit_type_id)
    if property_id is not None:
        await outbox_service.emit(
            conn,
            aggregate="inventory",
            aggregate_id=unit_type_id,
            event_type=outbox_service.INVENTORY_AVAILABILITY_CHANGED,
            payload={
                "unit_type_id": unit_type_id,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "closed": closed,
            },
            property_id=property_id,
        )
    log.info(
        "inventory-closed",
        unit_type_id=unit_type_id,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        closed=closed,
        affected=len(result),
    )
    return len(result)


async def get_availability(
    conn: asyncpg.Connection,
    unit_type_id: str,
    date_from: dt.date,
    date_to: dt.date,
) -> list[dict]:
    """Per-night availability for the catalog/calendar.

    Covers the half-open interval [date_from, date_to): a checkout on
    date_to is not a stay. Missing days are treated as unavailable.
    """
    if date_to <= date_from:
        raise ValueError("date_to must be after date_from")

    # The guest's total is the sum of these prices, so the availability read
    # has to carry one. price_day lives in the active rate plan; a day without
    # an explicit price falls back to the unit type's base price, exactly as
    # the partner calendar and the channel read-API do it.
    priced = await conn.fetch(
        """
        SELECT i.date,
               i.available,
               i.hold,
               i.sold,
               i.closed,
               (i.available - i.hold - i.sold) AS free,
               COALESCE(pd.price::float8, ut.base_price::float8) AS price
        FROM inventory_day i
        JOIN unit_type ut ON ut.id = i.unit_type_id
        LEFT JOIN rate_plan rp
               ON rp.unit_type_id = ut.id AND rp.active
        LEFT JOIN price_day pd
               ON pd.rate_plan_id = rp.id AND pd.date = i.date
        WHERE i.unit_type_id = $1
          AND i.date >= $2
          AND i.date <  $3
        ORDER BY i.date
        """,
        unit_type_id,
        date_from,
        date_to,
    )
    return [dict(r) for r in priced]
