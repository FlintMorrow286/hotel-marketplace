"""Booking service — the core of the platform.

Concurrency model: every hold is created inside a SERIALIZABLE transaction
that locks the affected `inventory_day` rows with `SELECT ... FOR UPDATE`,
ordered by (unit_type_id, date) to avoid deadlocks on overlapping ranges.
Two concurrent holds on the last room can never both succeed.

Idempotency: a client-supplied key makes retries safe — a replay returns the
original booking instead of creating a duplicate.
"""

from __future__ import annotations

import datetime as dt
import secrets

import asyncpg

from app.config.settings import settings
from app.modules.rate import service as rate_service
from app.utils.logger import get_logger
from app.utils.tz import today_at

log = get_logger(__name__)

BOOKING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _make_code() -> str:
    """Guest-facing code, e.g. BK-7K2Q9F."""
    body = "".join(secrets.choice(BOOKING_CODE_ALPHABET) for _ in range(6))
    return f"BK-{body}"


async def _load_or_lock_inventory(
    conn: asyncpg.Connection,
    unit_type_id: str,
    checkin: dt.date,
    checkout: dt.date,
) -> list[asyncpg.Record]:
    """Lock the inventory rows for the stay, ordered to avoid deadlocks.

    Returns the locked rows. Caller must be inside a transaction.
    """
    return await conn.fetch(
        """
        SELECT unit_type_id, date, available, hold, sold, closed
        FROM inventory_day
        WHERE unit_type_id = $1
          AND date >= $2
          AND date <  $3
        ORDER BY unit_type_id, date
        FOR UPDATE
        """,
        unit_type_id,
        checkin,
        checkout,
    )


async def _night_prices(
    conn: asyncpg.Connection,
    unit_type_id: str,
    checkin: dt.date,
    checkout: dt.date,
) -> list[tuple[dt.date, float]]:
    """Per-night prices for the stay, the way availability prices them.

    The active rate plan's `price_day` wins; a night without a row falls back
    to the unit type's base price. Read after the inventory rows are locked,
    so the price comes from the same transaction as the availability check.
    """
    rows = await conn.fetch(
        """
        SELECT i.date,
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
        checkin,
        checkout,
    )
    return [(r["date"], float(r["price"])) for r in rows]


class BookingError(Exception):
    """Base class for expected booking failures."""


class NotAvailable(BookingError):
    """No free inventory on the requested dates."""


class Conflict(BookingError):
    """Idempotency-key collision with a different payload."""


_OVER = ("cancelled", "failed", "refunded", "conflict", "no_show")


async def _retire_if_over(conn: asyncpg.Connection, existing: asyncpg.Record) -> bool:
    """Free the idempotency key of a web booking that has ended.

    A booking is over when it was cancelled, refunded or failed, or when its hold has
    run out and only the reaper's next sweep is missing (it is released here, exactly as
    the reaper would). The key moves to `<key>#retired:<id>`: unique, and the old booking
    stays traceable.
    """
    if existing["hold_lapsed"]:
        await release_hold(conn, existing["id"])
    elif existing["status"] not in _OVER:
        return False
    await conn.execute(
        "UPDATE booking SET idempotency_key = idempotency_key || '#retired:' || id::text "
        "WHERE id = $1",
        existing["id"],
    )
    log.info("idempotency-key-retired", booking_id=existing["id"], code=existing["code"])
    return True


async def create_hold(
    conn: asyncpg.Connection,
    *,
    unit_type_id: str,
    checkin: dt.date,
    checkout: dt.date,
    guest_name: str,
    guest_email: str,
    guest_phone: str,
    idempotency_key: str | None = None,
    origin: str = "web",
    published_only: bool = False,
    docs_version: str | None = None,
) -> dict:
    """Reserve inventory for the stay and return the new hold booking.

    Raises NotAvailable when there is no free inventory, Conflict when the
    idempotency key is reused with different request data. `published_only` is
    the public API's view: a property the catalog does not show takes no booking.
    `docs_version` is the version of the public documents the guest accepted; it is
    stored with the moment of acceptance. A channel booking has none.

    The caller is responsible for the transaction; tests wrap this in a
    rolled-back transaction, the route wraps it in a committed one.
    """
    nights = (checkout - checkin).days
    if nights < 1:
        raise NotAvailable("checkout must be after checkin")

    # ---- idempotency: replay the exact same request if the key exists
    if idempotency_key:
        existing = await conn.fetchrow(
            """
            SELECT id::text, code, status, total_amount, hold_expires_at,
                   checkin_date, checkout_date, unit_type_id::text,
                   guest_name, guest_email, guest_phone,
                   (status = 'hold' AND hold_expires_at IS NOT NULL
                    AND hold_expires_at <= now()) AS hold_lapsed
            FROM booking WHERE idempotency_key = $1
            """,
            idempotency_key,
        )
        # The web client derives the key from the stay, so "book these dates again" looks
        # like a retry. Only a retry of a booking that is still alive may replay it.
        # A channel's key is the OTA's own booking reference and keeps its first answer.
        if existing is not None and origin == "web" and await _retire_if_over(conn, existing):
            existing = None
        if existing is not None:
            # The whole request must match, guest included: without the guest
            # in `same`, two different people on the same dates would replay
            # one booking instead of getting their own.
            same = (
                existing["unit_type_id"] == unit_type_id
                and existing["checkin_date"] == checkin
                and existing["checkout_date"] == checkout
                and existing["guest_name"] == guest_name
                and existing["guest_email"] == guest_email
                and existing["guest_phone"] == guest_phone
            )
            if not same:
                raise Conflict("idempotency key used with different payload")
            log.info("hold-idempotent-replay", code=existing["code"])
            return dict(existing)

    # ---- unit type + price
    ut = await conn.fetchrow(
        """
        SELECT ut.id::text, ut.property_id::text, ut.base_price, ut.total_units, p.timezone,
               p.status AS property_status
        FROM unit_type ut
        JOIN property p ON p.id = ut.property_id
        WHERE ut.id = $1
        """,
        unit_type_id,
    )
    if ut is None:
        raise NotAvailable("unit type not found")
    # Inventory rows for past nights stay in the table, so without this a guest could hold
    # and pay for a stay that has already happened. "Today" is the property's: a guest may
    # book the night that is still today there. A channel pushes what an OTA already sold,
    # which can be a stay already under way, so only the web path is held to it.
    if origin == "web" and checkin < today_at(ut["timezone"]):
        raise NotAvailable("check-in date is in the past")
    # Staff blocked this property: the catalog no longer shows it, but its unit type ids are
    # still known to guests with an old link, and the block has to stop the money too.
    if origin == "web" and ut["property_status"] == "blocked":
        raise NotAvailable("property is not available for booking")
    # A draft or a property awaiting moderation keeps valid unit type ids too; the public API
    # takes bookings only where the catalog shows the property.
    if published_only and ut["property_status"] != "published":
        raise NotAvailable("property is not available for booking")

    # ---- lock inventory rows, ordered by (unit_type_id, date)
    locked = await _load_or_lock_inventory(conn, unit_type_id, checkin, checkout)
    if len(locked) != nights:
        raise NotAvailable("inventory not generated for all nights of the stay")

    # ---- check availability on every night
    for row in locked:
        if row["closed"]:
            raise NotAvailable(f"dates closed (stop sell) at {row['date']}")
        free = row["available"] - row["hold"] - row["sold"]
        if free < 1:
            raise NotAvailable(f"no free inventory at {row['date']}")

    # ---- price: the per-night price the availability read already showed the
    # guest. A night with no explicit price row falls back to base_price, the
    # same way get_availability and the channel read-API price it, so the
    # catalog, the checkout and the charge can never disagree.
    priced = await _night_prices(conn, unit_type_id, checkin, checkout)
    night_prices = [price for _, price in priced]
    total = round(sum(night_prices), 2)
    # The rate plan's commission share wins; without one the platform default
    # applies. Snapshotted here so a later change to the plan never reprices a
    # booking that is already confirmed.
    _plan_id, commission_rate = await rate_service.get_effective_rate(conn, unit_type_id)
    commission = round(total * commission_rate, 2)

    hold_expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=settings.hold_ttl_min)
    code = _make_code()

    row = await conn.fetchrow(
        """
        INSERT INTO booking
            (code, idempotency_key, property_id, unit_type_id,
             guest_name, guest_email, guest_phone,
             checkin_date, checkout_date, status, origin,
             total_amount, commission_rate, commission_amt, hold_expires_at,
             docs_version, docs_accepted_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'hold', $14,
                $10, $11, $12, $13, $15, $16)
        RETURNING id::text, code, status, total_amount, hold_expires_at
        """,
        code,
        idempotency_key,
        ut["property_id"],
        unit_type_id,
        guest_name,
        guest_email,
        guest_phone,
        checkin,
        checkout,
        total,
        commission_rate,
        commission,
        hold_expires_at,
        origin,
        docs_version,
        dt.datetime.now(dt.UTC) if docs_version else None,
    )
    assert row is not None

    # ---- decrement availability (hold bucket) for each night
    await conn.executemany(
        """
        UPDATE inventory_day SET hold = hold + 1
        WHERE unit_type_id = $1 AND date = $2
        """,
        [(unit_type_id, r["date"]) for r in locked],
    )

    await conn.executemany(
        """
        INSERT INTO booking_line (booking_id, date, price)
        VALUES ($1, $2, $3)
        """,
        [(row["id"], date, price) for date, price in priced],
    )

    log.info(
        "hold-created",
        booking_id=row["id"],
        code=code,
        nights=nights,
        total=total,
        expires_at=hold_expires_at.isoformat(),
    )
    return dict(row)


async def release_hold(conn: asyncpg.Connection, booking_id: str) -> None:
    """Return hold inventory to the pool and mark the booking cancelled.

    Safe against a second release or a confirmation of the same booking: the nights are locked before the
    status is read, in the order `_confirm` and `create_hold` lock them, so the later of two transactions
    sees what the earlier one left. The reaper runs READ COMMITTED, where a status read first is stale.
    """
    booking = await conn.fetchrow(
        """
        SELECT b.unit_type_id::text, b.property_id::text, b.code,
               b.checkin_date, b.checkout_date
        FROM booking b WHERE b.id = $1
        """,
        booking_id,
    )
    if booking is None:
        return

    await conn.fetch(
        """
        SELECT 1 FROM inventory_day
        WHERE unit_type_id = $1 AND date >= $2 AND date < $3
        ORDER BY date
        FOR UPDATE
        """,
        booking["unit_type_id"],
        booking["checkin_date"],
        booking["checkout_date"],
    )
    status = await conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    if status not in ("hold", "failed"):
        return
    claimed = await conn.fetchval(
        """
        UPDATE booking SET status = 'cancelled', cancelled_at = now()
        WHERE id = $1 AND status IN ('hold', 'failed')
        RETURNING true
        """,
        booking_id,
    )
    if not claimed:
        return

    await conn.executemany(
        """
        UPDATE inventory_day SET hold = GREATEST(hold - 1, 0)
        WHERE unit_type_id = $1
          AND date >= $2 AND date < $3
          AND hold > 0
        """,
        [(booking["unit_type_id"], booking["checkin_date"], booking["checkout_date"])],
    )

    from app.modules.outbox import service as outbox_service

    await outbox_service.emit(
        conn,
        aggregate="booking",
        aggregate_id=booking_id,
        event_type=outbox_service.BOOKING_CANCELLED,
        payload={
            "booking_id": booking_id,
            "code": booking["code"],
            "unit_type_id": booking["unit_type_id"],
            "checkin_date": booking["checkin_date"].isoformat(),
            "checkout_date": booking["checkout_date"].isoformat(),
            "expired": status == "hold",
        },
        property_id=booking["property_id"],
    )
    log.info("hold-released", booking_id=booking_id)


async def expire_holds(conn: asyncpg.Connection) -> int:
    """Reaper: cancel holds whose TTL has passed. Returns count released."""
    rows = await conn.fetch(
        """
        SELECT id::text FROM booking
        WHERE status = 'hold' AND hold_expires_at < now()
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 200
        """
    )
    released = 0
    for r in rows:
        async with conn.transaction():
            await release_hold(conn, r["id"])
        released += 1
    if released:
        log.info("reaper-expired-holds", count=released)
    return released


async def list_partner_bookings(
    conn: asyncpg.Connection,
    partner_id: str,
    status: str | None = None,
) -> list[asyncpg.Record]:
    """Every booking on the partner's own inventory, newest first.

    Scoped by a join on property, not by RLS: the route passes the partner's id
    from the token, and the join is the same ownership check the partner's
    other endpoints already make. A booking is included once even if the
    partner owns several properties — there is one row per booking, and the
    property name disambiguates which object it belongs to.
    """
    base = """
        SELECT b.id::text, b.code, b.status, b.total_amount::float8,
               b.commission_rate::float8,
               CASE WHEN b.commission_status = 'void' THEN 0
                    ELSE b.commission_amt::float8 END AS commission_amt,
               b.origin, b.source_channel,
               b.guest_name, b.guest_email, b.guest_phone,
               b.property_id::text, p.name AS property_name,
               b.unit_type_id::text, ut.name AS unit_type_name,
               b.checkin_date, b.checkout_date, b.created_at
        FROM booking b
        JOIN property p ON p.id = b.property_id
        JOIN unit_type ut ON ut.id = b.unit_type_id
        WHERE p.partner_id = $1
    """
    if status is None:
        return await conn.fetch(base + " ORDER BY b.created_at DESC", partner_id)
    return await conn.fetch(
        base + " AND b.status = $2 ORDER BY b.created_at DESC",
        partner_id,
        status,
    )
