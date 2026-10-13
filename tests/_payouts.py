"""Fixtures for the payout tests.

Bookings are inserted with exact money, so every expected amount can be written down by hand,
instead of being read back from the code under test. `refund_late` and `refund_free` write what
`payment.service.refund_booking` writes for a late and a free cancellation; the tests that must
prove the two agree go through the real refund instead.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import asyncpg

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.utils.tz import today_at

CENT = Decimal("0.01")
ADMIN = "admin-under-test"


async def partner_with_property(
    conn: asyncpg.Connection,
    email: str,
    *,
    tz: str = "Europe/Simferopol",
    currency: str = "RUB",
    name: str = "Мария",
) -> dict:
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name=name)
    )
    seed = {"partner_id": str(partner_id)}
    seed.update(await add_property(conn, seed, tz=tz, currency=currency))
    return seed


async def add_property(
    conn: asyncpg.Connection, seed: dict, *, tz: str, currency: str = "RUB", name: str = "Дом"
) -> dict:
    """Another property of the same partner, with a unit type and a month of inventory."""
    prop = await property_service.create_property(
        conn,
        seed["partner_id"],
        PropertyCreate(
            name=name, property_type="apartment", city="Ялта", timezone=tz, currency=currency
        ),
    )
    unit_type_id = await conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Номер', 2, 5, 3000) RETURNING id::text",
        prop.id,
    )
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 5 FROM generate_series($2::date, ($2::date + 29)::date, '1 day') "
        "AS d(date)",
        unit_type_id,
        today_at(tz),
    )
    return {"property_id": str(prop.id), "unit_type_id": unit_type_id, "tz": tz}


async def booking(
    conn: asyncpg.Connection,
    p: dict,
    *,
    total: str = "10000.00",
    rate: str = "0.12",
    commission: str | None = None,
    status: str = "confirmed",
    origin: str = "web",
    checkout_in: int = -3,
    nights: int = 2,
    commission_status: str | None = None,
    kept: str | None = None,
    paid: bool = True,
) -> str:
    """A booking whose checkout is `checkout_in` days from today at the property."""
    amount = Decimal(total)
    fee = Decimal(commission) if commission is not None else (amount * Decimal(rate)).quantize(CENT)
    checkout = today_at(p["tz"]) + dt.timedelta(days=checkout_in)
    checkin = checkout - dt.timedelta(days=nights)
    if commission_status is None:
        commission_status = {"confirmed": "accrued", "paid": "accrued", "refunded": "void"}.get(
            status, "none"
        )
    return await conn.fetchval(
        """
        INSERT INTO booking
            (code, property_id, unit_type_id, guest_name, guest_email, guest_phone,
             checkin_date, checkout_date, status, origin, total_amount, commission_rate,
             commission_amt, commission_status, kept_amount, paid_at)
        VALUES ($1, $2, $3, 'Гость', 'guest@example.com', '+79990000000', $4, $5, $6, $7, $8, $9,
                $10, $11, $12, CASE WHEN $13 THEN now() ELSE NULL END)
        RETURNING id::text
        """,
        "BK-" + uuid.uuid4().hex[:8].upper(),
        p["property_id"],
        p["unit_type_id"],
        checkin,
        checkout,
        status,
        origin,
        amount,
        Decimal(rate),
        fee,
        commission_status,
        Decimal(kept) if kept is not None else None,
        paid,
    )


async def refund_late(conn: asyncpg.Connection, booking_id: str, kept: str) -> None:
    """What a late cancellation leaves: the partner keeps `kept`, the commission shrinks with it."""
    await conn.execute(
        """
        UPDATE booking
        SET status = 'refunded', kept_amount = $2::numeric, commission_status = 'accrued',
            commission_amt = round(commission_amt * $2::numeric / total_amount, 2)
        WHERE id = $1
        """,
        booking_id,
        Decimal(kept),
    )


async def refund_free(conn: asyncpg.Connection, booking_id: str) -> None:
    await conn.execute(
        "UPDATE booking SET status = 'refunded', kept_amount = 0, commission_status = 'void' "
        "WHERE id = $1",
        booking_id,
    )


async def lines_of(conn: asyncpg.Connection, payout_id: str) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT l.booking_id::text, l.kind, l.gross_amount, l.commission_amount, l.net_amount
            FROM payout_line l JOIN booking b ON b.id = l.booking_id
            WHERE l.payout_id = $1 ORDER BY b.code
            """,
            payout_id,
        )
    )


async def payout_row(conn: asyncpg.Connection, payout_id: str) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT *, id::text AS sid FROM payout WHERE id = $1", payout_id)
    assert row is not None
    return row


async def payout_count(conn: asyncpg.Connection, *statuses: str) -> int:
    if statuses:
        return await conn.fetchval(
            "SELECT count(*) FROM payout WHERE status = ANY($1::text[])", list(statuses)
        )
    return await conn.fetchval("SELECT count(*) FROM payout")
