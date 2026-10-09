"""iCal import - pull an external calendar into our inventory.

Imported blocks become inventory_day.closed = true with closed_source='import'
(stop sell). New bookings on those dates are refused by create_hold, but
existing paid bookings are never cancelled: our DB is the source of truth, and
an external feed does not override real money.

Because closures are tagged, the importer unblocks only what it blocked itself -
a partner's manual stop sell survives import untouched.
"""

from __future__ import annotations

import datetime as dt

import asyncpg
import httpx

from app.config.settings import settings
from app.modules.sync.ical_parser import IcalParseError, expand_dates, parse_calendar
from app.utils.logger import get_logger
from app.utils.netguard import GUARD_HOOKS

log = get_logger(__name__)

IMPORT_SOURCE = "import"
IMPORT_HORIZON_DAYS = 365
FETCH_TIMEOUT_SEC = 15
USER_AGENT = "hotel-marketplace-ical-import/1.0"


async def sync_subscription(conn: asyncpg.Connection, subscription_id: str) -> dict:
    """Fetch the feed and apply it. Network happens outside the DB transaction."""
    sub = await conn.fetchrow(
        """
        SELECT s.id::text, s.url, s.enabled, ut.id::text AS unit_type_id
        FROM ical_subscription s
        JOIN unit_type ut ON ut.id = s.unit_type_id
        WHERE s.id = $1
        """,
        subscription_id,
    )
    if sub is None:
        raise ValueError("subscription not found")
    if not sub["enabled"]:
        return {"status": "skipped", "blocked": 0}

    try:
        text = await _fetch(sub["url"])
        events = parse_calendar(text)
    except (httpx.HTTPError, IcalParseError, ValueError) as exc:
        await _mark(conn, sub["id"], "error", str(exc)[:500], 0)
        log.warning("ical-import-failed", subscription_id=sub["id"], error=str(exc))
        return {"status": "error", "error": str(exc)}

    today = dt.date.today()
    horizon = today + dt.timedelta(days=IMPORT_HORIZON_DAYS)
    blocked = expand_dates(events, today, horizon)

    async with conn.transaction():
        result = await apply_import(conn, sub["unit_type_id"], blocked)

    await _mark(conn, sub["id"], "ok", None, result["blocked"])
    log.info(
        "ical-import-ok",
        subscription_id=sub["id"],
        events=len(events),
        blocked=result["blocked"],
        cleared=result["cleared"],
    )
    return {"status": "ok", **result}


async def apply_import(conn: asyncpg.Connection, unit_type_id: str, blocked: set[dt.date]) -> dict:
    """Apply the blocked date set to inventory as import-sourced stop sell.

    Idempotent: re-applying the same set changes nothing after the first run.
    """
    today = dt.date.today()
    horizon = today + dt.timedelta(days=IMPORT_HORIZON_DAYS)

    # Ensure rows exist for the horizon (a feed may block dates the generator
    # has not reached yet).
    await conn.execute(
        """
        WITH d AS (
            SELECT generate_series($1::date, $2::date, '1 day')::date AS date
        )
        INSERT INTO inventory_day (unit_type_id, date, available, closed, closed_source)
        SELECT $3, d.date, ut.total_units, false, 'manual'
        FROM d CROSS JOIN unit_type ut
        WHERE ut.id = $3
          AND NOT EXISTS (SELECT 1 FROM inventory_day i
                          WHERE i.unit_type_id = $3 AND i.date = d.date)
        """,
        today,
        horizon,
        unit_type_id,
    )

    blocked_list = sorted(blocked)
    if blocked_list:
        await conn.executemany(
            """
            UPDATE inventory_day
            SET closed = true, closed_source = $3
            WHERE unit_type_id = $1 AND date = $2
            """,
            [(unit_type_id, d, IMPORT_SOURCE) for d in blocked_list],
        )
    # Clear import-closed dates that left the feed. Manual closures
    # (closed_source = 'manual') are deliberately not touched.
    cleared_rows = await conn.fetch(
        """
        WITH leaving AS (
            SELECT date FROM inventory_day
            WHERE unit_type_id = $1
              AND closed_source = $3
              AND closed
              AND NOT (date = ANY($2::date[]))
              AND date >= CURRENT_DATE
        )
        UPDATE inventory_day
        SET closed = false, closed_source = 'manual'
        WHERE unit_type_id = $1
          AND closed_source = $3
          AND date IN (SELECT date FROM leaving)
        RETURNING 1
        """,
        unit_type_id,
        blocked_list,
        IMPORT_SOURCE,
    )

    from app.modules.outbox import service as outbox_service

    if blocked_list or cleared_rows:
        property_id = await outbox_service.property_of_unit_type(conn, unit_type_id)
        if property_id is not None:
            await outbox_service.emit(
                conn,
                aggregate="inventory",
                aggregate_id=unit_type_id,
                event_type=outbox_service.INVENTORY_AVAILABILITY_CHANGED,
                payload={
                    "unit_type_id": unit_type_id,
                    "blocked_count": len(blocked_list),
                    "cleared_count": len(cleared_rows),
                    "source": IMPORT_SOURCE,
                },
                property_id=property_id,
            )

    return {"blocked": len(blocked_list), "cleared": len(cleared_rows)}


async def list_due(conn: asyncpg.Connection, limit: int = 25) -> list[str]:
    """Claim subscriptions due for a sync.

    Claims by advancing last_synced_at in the same statement, so a second
    worker's SKIP LOCKED sees them as taken. No lock is held across the network
    fetch that follows - if that fetch dies, the row retries next interval.
    """
    rows = await conn.fetch(
        """
        WITH due AS (
            SELECT id FROM ical_subscription
            WHERE enabled
              AND (last_synced_at IS NULL
                   OR last_synced_at < now() - ($1::int || ' seconds')::interval)
            ORDER BY last_synced_at NULLS FIRST
            LIMIT $2
            FOR UPDATE SKIP LOCKED
        )
        UPDATE ical_subscription
        SET last_synced_at = now()
        WHERE id IN (SELECT id FROM due)
        RETURNING id::text
        """,
        settings.ical_sync_interval_sec,
        limit,
    )
    return [r["id"] for r in rows]


async def _fetch(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_SEC,
        follow_redirects=True,
        max_redirects=3,
        event_hooks=GUARD_HOOKS,
    ) as client:
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return response.text


async def _mark(
    conn: asyncpg.Connection,
    subscription_id: str,
    status: str,
    error: str | None,
    blocked: int,
) -> None:
    await conn.execute(
        """
        UPDATE ical_subscription
        SET last_synced_at = now(),
            last_status = $2,
            last_error = $3,
            last_blocked = $4
        WHERE id = $1
        """,
        subscription_id,
        status,
        error,
        blocked,
    )
