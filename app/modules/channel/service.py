"""Channel write-API.

A channel pushes bookings into us with an API key instead of a JWT. The key
resolves to a partner, and the booking goes through the *same* path a guest
uses: create_hold under SERIALIZABLE row locks, then pay_and_confirm. A channel
can never bypass the double-booking invariant or invent inventory it does not
own.
"""

from __future__ import annotations

import datetime as dt
import secrets

import asyncpg

from app.db.tx import serializable
from app.utils.logger import get_logger
from app.utils.secrets import hash_secret, needs_rehash, verify_secret

log = get_logger(__name__)

KEY_PREFIX = "hm_live_"
KEY_BYTES = 32
# Visible fingerprint in the UI: "hm_live_3f9a2b".
PREFIX_LEN = len(KEY_PREFIX) + 6


class ChannelError(Exception):
    """Base class for expected channel-API failures."""


class KeyRejected(ChannelError):
    """Unknown, disabled or malformed API key."""


class NotOwned(ChannelError):
    """The unit type or booking belongs to another partner."""


class NotAvailable(ChannelError):
    """The booking cannot move the way the caller asked."""


class BadRange(ChannelError):
    """The requested date range is empty or longer than we allow."""


# A channel pulling tariffs cannot ask for an unbounded range. Matches the
# partner calendar's horizon.
MAX_READ_RANGE_DAYS = 92


def _check_read_range(date_from: dt.date, date_to: dt.date) -> None:
    if date_to <= date_from:
        raise BadRange("date_to must be after date_from")
    if (date_to - date_from).days > MAX_READ_RANGE_DAYS:
        raise BadRange(f"range cannot exceed {MAX_READ_RANGE_DAYS} days")


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _hash(raw_key: str) -> str:
    return hash_secret(raw_key)


def generate_key() -> str:
    """A new plaintext key. Only ever returned to the partner once."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def is_well_formed(raw_key: str) -> bool:
    return raw_key.startswith(KEY_PREFIX) and len(raw_key) > PREFIX_LEN


async def create_key(conn: asyncpg.Connection, partner_id: str, label: str) -> dict:
    """Create a key. The plaintext is in the response and nowhere else."""
    raw = generate_key()
    row = await conn.fetchrow(
        """
        INSERT INTO api_key (partner_id, label, key_hash, key_prefix)
        VALUES ($1, $2, $3, $4)
        RETURNING id::text, label, key_prefix, enabled, last_used_at, created_at
        """,
        partner_id,
        label,
        _hash(raw),
        raw[:PREFIX_LEN],
    )
    assert row is not None
    log.info("api-key-created", partner_id=partner_id, label=label)
    return {**dict(row), "key": raw}


async def list_keys(conn: asyncpg.Connection, partner_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT id::text, label, key_prefix, enabled, last_used_at, created_at
        FROM api_key
        WHERE partner_id = $1
        ORDER BY created_at DESC
        """,
        partner_id,
    )
    return [dict(r) for r in rows]


async def revoke_key(conn: asyncpg.Connection, partner_id: str, key_id: str) -> bool:
    """Revoke. Ownership is checked in the same statement, never trust the id."""
    result = await conn.execute(
        "DELETE FROM api_key WHERE id = $1 AND partner_id = $2",
        key_id,
        partner_id,
    )
    return result.endswith(" 1")


async def resolve_key(conn: asyncpg.Connection, raw_key: str) -> dict:
    """Map a presented key to its partner. Raises KeyRejected if unusable.

    argon2 salts every hash, so a presented key cannot be turned into something
    to look up by equality. The visible prefix narrows the candidates to one
    partner's handful of keys, and each is verified properly.

    last_used_at is stamped on every use so the partner can spot a stale or
    leaked key; a failed lookup is not recorded.
    """
    if not is_well_formed(raw_key):
        raise KeyRejected("malformed api key")

    rows = await conn.fetch(
        """
        SELECT k.id::text, k.key_hash, k.partner_id::text, k.label, k.enabled, p.status
        FROM api_key k
        JOIN partner p ON p.id = k.partner_id
        WHERE k.key_prefix = $1
        """,
        raw_key[:PREFIX_LEN],
    )
    row = next((r for r in rows if verify_secret(raw_key, r["key_hash"])), None)
    if row is None or not row["enabled"]:
        raise KeyRejected("api key not found")
    if row["status"] != "active":
        raise KeyRejected("partner account is not active")

    if needs_rehash(row["key_hash"]):
        await conn.execute(
            "UPDATE api_key SET key_hash = $2 WHERE id = $1",
            row["id"],
            hash_secret(raw_key),
        )
        log.info("api-key-rehashed", key_id=row["id"])

    await conn.execute("UPDATE api_key SET last_used_at = now() WHERE id = $1", row["id"])
    return dict(row)


async def unit_type_owned_by(conn: asyncpg.Connection, unit_type_id: str, partner_id: str) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT 1 FROM unit_type ut
            JOIN property p ON p.id = ut.property_id
            WHERE ut.id = $1 AND p.partner_id = $2
            """,
            unit_type_id,
            partner_id,
        )
    )


async def get_channel_rates(
    conn: asyncpg.Connection,
    partner_id: str,
    unit_type_id: str,
    date_from: dt.date,
    date_to: dt.date,
) -> dict:
    """Per-night tariffs a channel pulls to sync its own pricing.

    Same key that pushes bookings, same ownership rule: another partner's unit
    type is NotOwned (a 404 for the channel, never a leak). Prices fall back to
    unit_type.base_price where the partner set no explicit price_day row — the
    same fallback the partner cabinet's calendar uses.

    [date_from, date_to) is half-open: the checkout night is not priced.
    """
    if not await unit_type_owned_by(conn, unit_type_id, partner_id):
        raise NotOwned("unit type not found")
    _check_read_range(date_from, date_to)

    rows = await conn.fetch(
        """
        SELECT d.date,
               p.currency,
               COALESCE(pd.price::float8, ut.base_price::float8) AS price,
               COALESCE(pd.min_stay, 1)                          AS min_stay,
               COALESCE(pd.max_stay, 0)                          AS max_stay,
               COALESCE(pd.cta, true)                            AS cta,
               COALESCE(pd.ctd, true)                            AS ctd,
               COALESCE(pd.stop_sell, false)                     AS stop_sell
        FROM (SELECT generate_series($2::date, ($3::date - interval '1 day')::date, '1 day')::date AS date) d
        JOIN unit_type ut ON ut.id = $1
        JOIN property  p  ON p.id = ut.property_id
        LEFT JOIN LATERAL (
            SELECT id FROM rate_plan
            WHERE unit_type_id = ut.id AND active
            ORDER BY created_at
            LIMIT 1
        ) rp ON true
        LEFT JOIN price_day pd ON pd.rate_plan_id = rp.id AND pd.date = d.date
        ORDER BY d.date
        """,
        unit_type_id,
        date_from,
        date_to,
    )
    return {
        "unit_type_id": unit_type_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "currency": rows[0]["currency"] if rows else "RUB",
        "days": [
            {
                "date": _iso(r["date"]),
                "price": r["price"],
                "min_stay": r["min_stay"],
                "max_stay": r["max_stay"] or None,
                "cta": r["cta"],
                "ctd": r["ctd"],
                "stop_sell": r["stop_sell"],
            }
            for r in rows
        ],
    }


async def get_channel_availability(
    conn: asyncpg.Connection,
    partner_id: str,
    unit_type_id: str,
    date_from: dt.date,
    date_to: dt.date,
) -> dict:
    """Per-night availability a channel pulls to sync its inventory.

    free = available - hold - sold, floored at 0. A closed date (stop sell,
    incl. an iCal-imported closure) is flagged separately: it may still report
    free units, but it is not bookable — a channel must not sell it, the same
    rule that rejects a channel booking on a closed date.

    [date_from, date_to) is half-open: the checkout night is not a stay. Days
    the inventory generator has not reached yet are reported as free=0.
    """
    if not await unit_type_owned_by(conn, unit_type_id, partner_id):
        raise NotOwned("unit type not found")
    _check_read_range(date_from, date_to)

    rows = await conn.fetch(
        """
        SELECT d.date,
               ut.total_units,
               COALESCE(i.available, 0)  AS available,
               COALESCE(i.hold, 0)       AS hold,
               COALESCE(i.sold, 0)       AS sold,
               COALESCE(i.closed, false) AS closed,
               COALESCE(pd.stop_sell, false) AS stop_sell
        FROM (SELECT generate_series($2::date, ($3::date - interval '1 day')::date, '1 day')::date AS date) d
        JOIN unit_type ut ON ut.id = $1
        LEFT JOIN inventory_day i ON i.unit_type_id = ut.id AND i.date = d.date
        LEFT JOIN LATERAL (
            SELECT id FROM rate_plan
            WHERE unit_type_id = ut.id AND active
            ORDER BY created_at
            LIMIT 1
        ) rp ON true
        LEFT JOIN price_day pd ON pd.rate_plan_id = rp.id AND pd.date = d.date
        ORDER BY d.date
        """,
        unit_type_id,
        date_from,
        date_to,
    )
    days = []
    for r in rows:
        closed = bool(r["closed"]) or bool(r["stop_sell"])
        free = max(r["available"] - r["hold"] - r["sold"], 0)
        days.append(
            {
                "date": _iso(r["date"]),
                "available": r["available"],
                "hold": r["hold"],
                "sold": r["sold"],
                "free": free,
                "closed": closed,
                "bookable": 0 if closed else free,
            }
        )
    return {
        "unit_type_id": unit_type_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "total_units": rows[0]["total_units"] if rows else 0,
        "days": days,
    }


async def booking_owned_by(conn: asyncpg.Connection, booking_id: str, partner_id: str) -> bool:
    """A channel may only see bookings on its own inventory."""
    return bool(
        await conn.fetchval(
            """
            SELECT 1 FROM booking b
            JOIN property p ON p.id = b.property_id
            WHERE b.id = $1 AND p.partner_id = $2 AND b.origin = 'channel'
            """,
            booking_id,
            partner_id,
        )
    )


def idempotency_namespace(partner_id: str, client_key: str) -> str:
    """Channel idempotency keys live in their own space.

    A channel's keys never collide with web idempotency keys or with another
    channel's, so a replay is always the same booking.
    """
    return f"channel:{partner_id}:{client_key}"


async def create_channel_booking(
    conn: asyncpg.Connection,
    *,
    partner_id: str,
    unit_type_id: str,
    checkin: dt.date,
    checkout: dt.date,
    guest_name: str,
    guest_email: str,
    guest_phone: str,
    client_key: str,
) -> dict:
    """The channel write path: the same route a guest takes, origin 'channel'.

    Hold under the SERIALIZABLE inventory locks, then confirm through the
    payment provider. A replay of the same idempotency key replays the original
    booking and is still a success.

    Raises NotOwned / NotAvailable / Conflict / PaymentError.
    """
    from app.modules.booking import service as booking_service
    from app.modules.payment import service as payment_service

    if not await unit_type_owned_by(conn, unit_type_id, partner_id):
        raise NotOwned("unit type not found")

    idem = idempotency_namespace(partner_id, client_key)

    # 1. Hold with the same row locks a guest hold takes.
    hold = await serializable(
        conn,
        lambda: booking_service.create_hold(
            conn,
            unit_type_id=unit_type_id,
            checkin=checkin,
            checkout=checkout,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            idempotency_key=idem,
            origin="channel",
        ),
    )

    # A replay of an already-confirmed booking is a success, not an error:
    # the channel retried after we already confirmed.
    if hold["status"] == "confirmed":
        return booking_out(hold)

    # 2. The channel vouches for the payment; confirm exactly like the web
    #    checkout does. The hold bucket becomes sold.
    confirmed = await payment_service.pay_and_confirm(hold["id"], conn=conn)
    log.info(
        "channel-booking-created",
        booking_id=confirmed["id"],
        code=confirmed["code"],
        partner_id=partner_id,
    )
    return confirmed


async def cancel_channel_booking(
    conn: asyncpg.Connection, partner_id: str, booking_id: str
) -> dict:
    """Cancel a channel booking: a hold is released, a confirmed one refunded.

    Only channel-origin bookings on this partner's inventory are reachable.
    """
    from app.modules.booking import service as booking_service
    from app.modules.payment import service as payment_service

    if not await booking_owned_by(conn, booking_id, partner_id):
        raise NotOwned("booking not found")

    status_now = await conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    if status_now is None:
        raise NotOwned("booking not found")

    if status_now == "confirmed":
        return await payment_service.refund_booking(booking_id, conn=conn)
    if status_now == "hold":
        await serializable(conn, lambda: booking_service.release_hold(conn, booking_id))
        return {"id": booking_id, "status": "cancelled"}
    raise NotAvailable(f"cannot cancel booking with status={status_now}")


def booking_out(row) -> dict:
    """Shape a booking row the way the channel API returns it."""
    return {
        "id": row["id"],
        "code": row["code"],
        "status": row["status"],
        "total_amount": float(row["total_amount"]),
        "checkin_date": row["checkin_date"].isoformat(),
        "checkout_date": row["checkout_date"].isoformat(),
    }
