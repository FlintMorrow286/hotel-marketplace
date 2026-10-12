"""Slice 6 tests: cancellation rule + commission reports."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import asyncpg
import jwt
import pytest

from app.config.settings import settings
from app.modules.admin import report
from app.modules.admin import service as admin_service
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service as booking_service
from app.modules.payment import service as payment_service
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.utils.tz import today_at

# The property's date, not the server's: see TODAY in test_booking.py.
TODAY = today_at("Europe/Simferopol")


async def _seed(conn: asyncpg.Connection, email: str, base_price: float = 3000) -> dict:
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
        "VALUES ($1, 'Квартира', 2, 1, $2) RETURNING id::text",
        prop.id,
        base_price,
    )
    assert row is not None
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series($2::date, ($2::date + 59)::date, '1 day') AS d(date)",
        row["id"],
        TODAY,
    )
    return {"unit_type_id": row["id"], "partner_id": str(partner_id)}


def _guest() -> dict:
    return {
        "guest_name": "Иван Гость",
        "guest_email": f"g{uuid.uuid4().hex[:6]}@example.com",
        "guest_phone": "+79991234567",
    }


async def _confirmed(conn: asyncpg.Connection, ut: str, checkin: dt.date, nights: int = 2) -> str:
    b = await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=nights),
        idempotency_key=f"k-{uuid.uuid4()}",
        **_guest(),
    )
    await payment_service.pay_and_confirm(b["id"], conn=conn)
    return b["id"]


# ---------------------------------------------------------------- cancellation rule


@pytest.mark.asyncio
async def test_free_cancellation_before_deadline(committed_conn) -> None:
    """Cancelling well before check-in is free."""
    seed = await _seed(committed_conn, "c1@example.com")
    booking_id = await _confirmed(
        committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=30)
    )

    result = await payment_service.refund_booking(booking_id, conn=committed_conn)

    assert result["status"] == "refunded"
    assert result["free_cancelled"] is True


@pytest.mark.asyncio
async def test_late_cancellation_flagged(committed_conn) -> None:
    """Cancelling on the check-in day is past the free deadline."""
    seed = await _seed(committed_conn, "c2@example.com")
    booking_id = await _confirmed(committed_conn, seed["unit_type_id"], TODAY)

    result = await payment_service.refund_booking(booking_id, conn=committed_conn)

    assert result["status"] == "refunded"
    assert result["free_cancelled"] is False


# ---------------------------------------------------------------- commission


@pytest.mark.asyncio
async def test_commission_accrued_on_confirm(committed_conn) -> None:
    seed = await _seed(committed_conn, "c3@example.com")
    booking_id = await _confirmed(
        committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=10)
    )

    row = await committed_conn.fetchrow(
        "SELECT commission_status, commission_amt::float8, total_amount::float8 "
        "FROM booking WHERE id = $1",
        booking_id,
    )
    assert row["commission_status"] == "accrued"
    # default 12% from legal config
    assert round(row["commission_amt"], 2) == round(row["total_amount"] * 0.12, 2)


@pytest.mark.asyncio
async def test_commission_voided_on_refund(committed_conn) -> None:
    seed = await _seed(committed_conn, "c4@example.com")
    booking_id = await _confirmed(
        committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=10)
    )

    await payment_service.refund_booking(booking_id, conn=committed_conn)

    status = await committed_conn.fetchval(
        "SELECT commission_status FROM booking WHERE id = $1", booking_id
    )
    assert status == "void"


@pytest.mark.asyncio
async def test_commission_report_aggregates_per_partner(committed_conn) -> None:
    seed = await _seed(committed_conn, "c5@example.com")
    await _confirmed(committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=10), nights=3)
    await _confirmed(committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=20), nights=2)

    rep = await report.commission_report(committed_conn)

    assert rep["total"]["bookings"] == 2
    assert rep["total"]["gross"] == 15000  # 5 nights x 3000
    assert round(rep["total"]["commission"], 2) == round(15000 * 0.12, 2)
    assert len(rep["partners"]) == 1
    assert rep["partners"][0]["bookings"] == 2


@pytest.mark.asyncio
async def test_commission_report_excludes_refunded(committed_conn) -> None:
    """A refunded booking must not count towards commission."""
    seed = await _seed(committed_conn, "c6@example.com")
    keep = await _confirmed(
        committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=10), nights=2
    )
    drop = await _confirmed(
        committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=20), nights=2
    )
    await payment_service.refund_booking(drop, conn=committed_conn)

    rep = await report.commission_report(committed_conn)

    assert rep["total"]["bookings"] == 1
    assert rep["total"]["gross"] == 6000
    assert rep["partners"][0]["bookings"] == 1
    # the kept booking is the accrued one
    kept_id = await committed_conn.fetchval(
        "SELECT id::text FROM booking WHERE commission_status = 'accrued'"
    )
    assert kept_id == keep


@pytest.mark.asyncio
async def test_commission_report_date_filter(committed_conn) -> None:
    seed = await _seed(committed_conn, "c7@example.com")
    await _confirmed(committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=10))
    await _confirmed(committed_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=40))

    rep = await report.commission_report(
        committed_conn, TODAY + dt.timedelta(days=5), TODAY + dt.timedelta(days=30)
    )

    assert rep["total"]["bookings"] == 1


# ---------------------------------------------------------------- admin auth + moderation


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _seed_admin(conn: asyncpg.Connection, email: str) -> None:
    await conn.execute(
        "INSERT INTO admin (email, password_hash) VALUES ($1, $2) ON CONFLICT (email) DO NOTHING",
        email,
        _sha("secret123"),
    )


async def _property_with_status(conn: asyncpg.Connection, email: str, status: str) -> str:
    seed = await _seed(conn, email)
    property_id = await conn.fetchval(
        "SELECT id::text FROM property WHERE partner_id = $1", seed["partner_id"]
    )
    assert property_id is not None
    await conn.execute("UPDATE property SET status = $2 WHERE id = $1", property_id, status)
    return property_id


@pytest.mark.asyncio
async def test_admin_login_issues_admin_scoped_token(committed_conn) -> None:
    await _seed_admin(committed_conn, "a1@example.com")

    token = await admin_service.login_admin(committed_conn, "a1@example.com", "secret123")

    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["scope"] == "admin"


@pytest.mark.asyncio
async def test_admin_login_rejects_wrong_password(committed_conn) -> None:
    await _seed_admin(committed_conn, "a2@example.com")

    with pytest.raises(ValueError):
        await admin_service.login_admin(committed_conn, "a2@example.com", "wrongpass")


@pytest.mark.asyncio
async def test_admin_login_rejects_partner_credentials(committed_conn) -> None:
    """A partner account must not authenticate as staff."""
    await _seed(committed_conn, "a3@example.com")  # registers a partner

    with pytest.raises(ValueError):
        await admin_service.login_admin(committed_conn, "a3@example.com", "secret123")


@pytest.mark.asyncio
async def test_moderation_approves_pending_submission(committed_conn) -> None:
    property_id = await _property_with_status(
        committed_conn, "a4@example.com", "pending_moderation"
    )

    row = await admin_service.set_property_status(committed_conn, property_id, "published")

    assert row is not None
    assert row["status"] == "published"
    assert row["partner_email"] == "a4@example.com"


@pytest.mark.asyncio
async def test_moderation_blocks_and_unblocks(committed_conn) -> None:
    property_id = await _property_with_status(committed_conn, "a5@example.com", "published")

    blocked = await admin_service.set_property_status(committed_conn, property_id, "blocked")
    assert blocked["status"] == "blocked"

    restored = await admin_service.set_property_status(committed_conn, property_id, "published")
    assert restored["status"] == "published"


@pytest.mark.asyncio
async def test_moderation_rejects_illegal_transition(committed_conn) -> None:
    """published -> draft would silently drop a live listing; refuse it."""
    property_id = await _property_with_status(committed_conn, "a6@example.com", "published")

    with pytest.raises(ValueError):
        await admin_service.set_property_status(committed_conn, property_id, "draft")


@pytest.mark.asyncio
async def test_moderation_unknown_property_returns_none(committed_conn) -> None:
    assert (
        await admin_service.set_property_status(committed_conn, str(uuid.uuid4()), "published")
        is None
    )


@pytest.mark.asyncio
async def test_admin_property_list_orders_queue_first(committed_conn) -> None:
    """pending_moderation surfaces before published, regardless of age."""
    await _property_with_status(committed_conn, "a7@example.com", "published")
    await _property_with_status(committed_conn, "a8@example.com", "pending_moderation")
    await _property_with_status(committed_conn, "a9@example.com", "draft")

    rows = await admin_service.list_properties_for_admin(committed_conn)

    assert [r["status"] for r in rows] == [
        "pending_moderation",
        "published",
        "draft",
    ]
    assert all("partner_email" in r for r in rows)


@pytest.mark.asyncio
async def test_admin_property_list_filters_by_status(committed_conn) -> None:
    await _property_with_status(committed_conn, "b1@example.com", "published")
    await _property_with_status(committed_conn, "b2@example.com", "blocked")

    rows = await admin_service.list_properties_for_admin(committed_conn, status="blocked")

    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
