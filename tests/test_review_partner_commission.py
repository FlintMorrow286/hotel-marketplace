import datetime as dt
import uuid

import asyncpg
import pytest

from app.config import legal
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service as booking_service
from app.modules.payment import service as payment_service
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.utils.tz import today_at


async def _create_booking(conn: asyncpg.Connection, unit_type_id: str, checkin: dt.date) -> dict:
    return await booking_service.create_hold(
        conn,
        unit_type_id=unit_type_id,
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=2),
        idempotency_key=f"review-{uuid.uuid4()}",
        guest_name="Review Guest",
        guest_email=f"{uuid.uuid4().hex}@example.com",
        guest_phone="+79991234567",
    )


@pytest.mark.asyncio
async def test_partner_listing_omits_void_commission_but_keeps_accrued_amount(
    committed_conn: asyncpg.Connection, monkeypatch
) -> None:
    monkeypatch.setattr(legal, "COMMISSION_ON_PENALTY", True)
    today = today_at("Europe/Simferopol")  # the property's date, not the server's
    partner_id = await auth_service.register_partner(
        committed_conn,
        PartnerRegisterRequest(
            email=f"review-{uuid.uuid4().hex}@example.com",
            password="secret123",
            name="Review Partner",
        ),
    )
    prop = await property_service.create_property(
        committed_conn,
        partner_id,
        PropertyCreate(
            name="Review Property",
            property_type="house",
            city="Koktebel",
            timezone="Europe/Simferopol",
        ),
    )
    unit = await committed_conn.fetchrow(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Standard', 2, 1, 3000) RETURNING id::text",
        prop.id,
    )
    assert unit is not None
    unit_type_id = unit["id"]
    await committed_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series($2::date, ($2::date + 49)::date, '1 day') AS d(date)",
        unit_type_id,
        today,
    )

    free = await _create_booking(committed_conn, unit_type_id, today + dt.timedelta(days=10))
    late = await _create_booking(committed_conn, unit_type_id, today)
    confirmed = await _create_booking(committed_conn, unit_type_id, today + dt.timedelta(days=30))
    original_rows = await committed_conn.fetch(
        "SELECT id::text, total_amount::float8 AS total, commission_amt::float8 AS commission "
        "FROM booking WHERE id = ANY($1::uuid[])",
        [free["id"], late["id"], confirmed["id"]],
    )
    originals = {row["id"]: row for row in original_rows}

    for booking in (free, late, confirmed):
        await payment_service.pay_and_confirm(booking["id"], conn=committed_conn)
    await payment_service.refund_booking(free["id"], conn=committed_conn)
    await payment_service.refund_booking(late["id"], conn=committed_conn)

    stored_rows = await committed_conn.fetch(
        "SELECT id::text, commission_status, commission_amt::float8 AS commission "
        "FROM booking WHERE id = ANY($1::uuid[])",
        [free["id"], late["id"], confirmed["id"]],
    )
    stored = {row["id"]: row for row in stored_rows}
    late_commission = round(
        originals[late["id"]]["commission"] * legal.CANCELLATION_PENALTY_RATE, 2
    )

    assert stored[free["id"]]["commission_status"] == "void"
    assert stored[free["id"]]["commission"] == pytest.approx(originals[free["id"]]["commission"])
    assert stored[late["id"]]["commission_status"] == "accrued"
    assert stored[late["id"]]["commission"] == pytest.approx(late_commission)

    listed = await booking_service.list_partner_bookings(committed_conn, str(partner_id))
    by_id = {row["id"]: row for row in listed}

    assert by_id[free["id"]]["commission_amt"] == 0
    assert by_id[late["id"]]["commission_amt"] == pytest.approx(late_commission)
    assert by_id[confirmed["id"]]["commission_amt"] == pytest.approx(
        originals[confirmed["id"]]["commission"]
    )
