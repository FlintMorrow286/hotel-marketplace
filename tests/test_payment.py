"""Slice 4 tests: payment stub + instant confirm + refund."""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from app.config.settings import settings
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service as booking_service
from app.modules.payment import service
from app.modules.payment.provider import FailProvider, get_payment_provider
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.utils.tz import today_at

# The property's date, not the server's: see TODAY in test_booking.py.
TODAY = today_at("Europe/Simferopol")


async def _seed_unit(conn: asyncpg.Connection, email: str, total_units: int = 1) -> str:
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await property_service.create_property(
        conn,
        partner_id,
        PropertyCreate(
            name="Test", property_type="apartment", city="Yalta", timezone="Europe/Simferopol"
        ),
    )
    row = await conn.fetchrow(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Квартира', 2, $2, 3000) RETURNING id::text",
        prop.id,
        total_units,
    )
    assert row is not None
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, $2 FROM generate_series($3::date, ($3::date + 29)::date, '1 day') AS d(date)",
        row["id"],
        total_units,
        TODAY,
    )
    return row["id"]


def _guest() -> dict:
    return {
        "guest_name": "Иван Гость",
        "guest_email": f"g{uuid.uuid4().hex[:6]}@example.com",
        "guest_phone": "+79991234567",
    }


async def _hold(conn: asyncpg.Connection, ut: str, nights: int = 2) -> str:
    b = await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=nights),
        idempotency_key=f"k-{uuid.uuid4()}",
        **_guest(),
    )
    return b["id"]


# ---------------------------------------------------------------- provider selection


def test_stub_provider_by_default() -> None:
    assert settings.payment_mode == "stub"
    provider = get_payment_provider()
    assert provider.name == "stub"


def test_fail_provider_selected() -> None:
    original = settings.payment_mode
    settings.payment_mode = "fail"
    try:
        assert isinstance(get_payment_provider(), FailProvider)
    finally:
        settings.payment_mode = original


# ---------------------------------------------------------------- pay + confirm


@pytest.mark.asyncio
async def test_pay_confirms_booking(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "pay1@example.com")
    booking_id = await _hold(committed_conn, ut)

    held = await committed_conn.fetchval(
        "SELECT hold FROM inventory_day WHERE unit_type_id=$1 AND date=$2", ut, TODAY
    )
    assert held == 1

    result = await service.pay_and_confirm(booking_id, conn=committed_conn)

    assert result["status"] == "confirmed"
    assert result["paid_at"] is not None
    assert result["total_amount"] == 6000

    row = await committed_conn.fetchrow(
        "SELECT hold, sold FROM inventory_day WHERE unit_type_id=$1 AND date=$2", ut, TODAY
    )
    assert row["hold"] == 0
    assert row["sold"] == 1

    pay = await committed_conn.fetchrow(
        "SELECT status, amount::float8, provider FROM payment WHERE booking_id=$1", booking_id
    )
    assert pay["status"] == "succeeded"
    assert pay["amount"] == 6000
    assert pay["provider"] == "stub"


@pytest.mark.asyncio
async def test_pay_non_hold_booking_refused(committed_conn) -> None:
    """Paying an already-confirmed booking must fail."""
    ut = await _seed_unit(committed_conn, "pay2@example.com")
    booking_id = await _hold(committed_conn, ut)
    await service.pay_and_confirm(booking_id, conn=committed_conn)

    with pytest.raises(service.PaymentError):
        await service.pay_and_confirm(booking_id, conn=committed_conn)


@pytest.mark.asyncio
async def test_pay_expired_hold_refused(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "pay3@example.com")
    booking_id = await _hold(committed_conn, ut)
    await committed_conn.execute(
        "UPDATE booking SET hold_expires_at = now() - interval '1 minute' WHERE id = $1",
        booking_id,
    )

    with pytest.raises(service.PaymentError, match="expired"):
        await service.pay_and_confirm(booking_id, conn=committed_conn)


@pytest.mark.asyncio
async def test_fail_provider_marks_payment_failed(committed_conn) -> None:
    original = settings.payment_mode
    settings.payment_mode = "fail"
    try:
        ut = await _seed_unit(committed_conn, "pay4@example.com")
        booking_id = await _hold(committed_conn, ut)

        with pytest.raises(service.PaymentError):
            await service.pay_and_confirm(booking_id, conn=committed_conn)

        status_now = await committed_conn.fetchval(
            "SELECT status FROM booking WHERE id=$1", booking_id
        )
        assert status_now == "hold"  # still holdable, nothing sold
    finally:
        settings.payment_mode = original


# ---------------------------------------------------------------- refund


@pytest.mark.asyncio
async def test_refund_returns_inventory(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "pay5@example.com")
    booking_id = await _hold(committed_conn, ut)
    await service.pay_and_confirm(booking_id, conn=committed_conn)

    result = await service.refund_booking(booking_id, conn=committed_conn)

    assert result["status"] == "refunded"
    row = await committed_conn.fetchrow(
        "SELECT hold, sold FROM inventory_day WHERE unit_type_id=$1 AND date=$2", ut, TODAY
    )
    assert row["sold"] == 0
    assert row["hold"] == 0

    refunded = await committed_conn.fetchval(
        "SELECT status FROM payment WHERE booking_id=$1 AND status='refunded'", booking_id
    )
    assert refunded == "refunded"


@pytest.mark.asyncio
async def test_refund_hold_booking_refused(committed_conn) -> None:
    """A not-yet-paid booking cannot be refunded; cancel it instead."""
    ut = await _seed_unit(committed_conn, "pay6@example.com")
    booking_id = await _hold(committed_conn, ut)

    with pytest.raises(service.PaymentError):
        await service.refund_booking(booking_id, conn=committed_conn)


@pytest.mark.asyncio
async def test_late_cancellation_keeps_the_penalty(committed_conn) -> None:
    """Cancelling on the check-in day refunds only the non-penalty share.

    The deadline is 24h before check-in, so a stay starting today is already
    past it. Before this fix the guest got the whole amount back and the
    penalty in app/config/legal.py was dead code.
    """
    from app.config import legal

    ut = await _seed_unit(committed_conn, "pay7@example.com")
    booking_id = await _hold(committed_conn, ut)
    await service.pay_and_confirm(booking_id, conn=committed_conn)

    total = await committed_conn.fetchval(
        "SELECT total_amount::float8 FROM booking WHERE id = $1", booking_id
    )

    result = await service.refund_booking(booking_id, conn=committed_conn)

    expected = round(total * (1 - legal.CANCELLATION_PENALTY_RATE), 2)
    assert result["refunded_amount"] == expected
    assert result["free_cancelled"] is False


@pytest.mark.asyncio
async def test_early_cancellation_is_free(committed_conn) -> None:
    """Cancelling before the deadline refunds everything."""
    from app.modules.booking import service as booking_service

    ut = await _seed_unit(committed_conn, "pay8@example.com")
    # _seed_unit covers 30 days; the stay starts at the edge, so extend the
    # inventory to the checkout night or the hold finds no rows.
    await committed_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series(($2::date + 29)::date,"
        " ($3::date - 1)::date, '1 day') AS d(date)"
        " ON CONFLICT (unit_type_id, date) DO NOTHING",
        ut,
        TODAY,
        TODAY + dt.timedelta(days=32),
    )
    booking = await booking_service.create_hold(
        committed_conn,
        unit_type_id=ut,
        checkin=TODAY + dt.timedelta(days=30),
        checkout=TODAY + dt.timedelta(days=32),
        idempotency_key=f"k-{uuid.uuid4()}",
        **_guest(),
    )
    booking_id = booking["id"]
    await service.pay_and_confirm(booking_id, conn=committed_conn)

    result = await service.refund_booking(booking_id, conn=committed_conn)

    assert result["refunded_amount"] == 6000
    assert result["free_cancelled"] is True
