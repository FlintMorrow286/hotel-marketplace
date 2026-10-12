"""iCal export tests: feed tokens, ownership, and calendar rendering."""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service as booking_service
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.modules.sync import ical_export

TODAY = dt.date.today()


async def _seed(conn: asyncpg.Connection, email: str) -> dict:
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await property_service.create_property(
        conn,
        partner_id,
        PropertyCreate(
            name="Test",
            property_type="apartment",
            city="Koktebel",
            timezone="Europe/Simferopol",
        ),
    )
    row = await conn.fetchrow(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Квартира', 2, 1, 3000) RETURNING id::text",
        prop.id,
    )
    assert row is not None
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series("
        "  CURRENT_DATE, (CURRENT_DATE + interval '89 days')::date, '1 day'"
        ") AS d(date)",
        row["id"],
    )
    return {"unit_type_id": row["id"], "partner_id": str(partner_id)}


def _guest() -> dict:
    return {
        "guest_name": "Иван Гость",
        "guest_email": f"g{uuid.uuid4().hex[:6]}@example.com",
        "guest_phone": "+79991234567",
    }


async def _hold(
    conn: asyncpg.Connection, ut: str, checkin: dt.date, nights: int = 2, origin: str = "web"
) -> str:
    b = await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=nights),
        idempotency_key=f"k-{uuid.uuid4()}",
        origin=origin,
        **_guest(),
    )
    return b["id"]


# ---------------------------------------------------------------- feed tokens


@pytest.mark.asyncio
async def test_feed_created_and_found_by_token(db_conn) -> None:
    seed = await _seed(db_conn, "i1@example.com")

    feed = await ical_export.create_or_rotate_feed(
        db_conn, seed["unit_type_id"], seed["partner_id"]
    )

    assert feed["token"]
    found = await ical_export.get_feed_by_token(db_conn, feed["token"])
    assert found is not None
    assert found["unit_type_id"] == seed["unit_type_id"]
    assert found["enabled"] is True


@pytest.mark.asyncio
async def test_feed_rotation_invalidates_old_token(db_conn) -> None:
    seed = await _seed(db_conn, "i2@example.com")

    first = await ical_export.create_or_rotate_feed(
        db_conn, seed["unit_type_id"], seed["partner_id"]
    )
    second = await ical_export.create_or_rotate_feed(
        db_conn, seed["unit_type_id"], seed["partner_id"]
    )

    assert first["token"] != second["token"]
    assert await ical_export.get_feed_by_token(db_conn, first["token"]) is None
    assert await ical_export.get_feed_by_token(db_conn, second["token"]) is not None


@pytest.mark.asyncio
async def test_feed_rejected_for_foreign_unit_type(db_conn) -> None:
    """A partner must not create a feed for another partner's unit type."""
    mine = await _seed(db_conn, "i3@example.com")
    other = await _seed(db_conn, "i4@example.com")

    with pytest.raises(ValueError):
        await ical_export.create_or_rotate_feed(db_conn, other["unit_type_id"], mine["partner_id"])


@pytest.mark.asyncio
async def test_unknown_token_not_found(db_conn) -> None:
    assert await ical_export.get_feed_by_token(db_conn, "does-not-exist") is None


# ---------------------------------------------------------------- calendar body


@pytest.mark.asyncio
async def test_calendar_marks_confirmed_and_held_stays(db_conn) -> None:
    seed = await _seed(db_conn, "i5@example.com")
    held = await _hold(db_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=10))
    paid = await _hold(db_conn, seed["unit_type_id"], TODAY + dt.timedelta(days=40))
    await db_conn.execute("UPDATE booking SET status = 'confirmed' WHERE id = $1", paid)

    body = await ical_export.build_calendar(db_conn, seed["unit_type_id"])

    assert body is not None
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.rstrip().endswith("END:VCALENDAR")
    assert f"UID:booking:{held}" in body
    assert f"UID:booking:{paid}" in body
    assert "STATUS:TENTATIVE" in body  # the hold
    assert "STATUS:CONFIRMED" in body  # the confirmed stay
    # DTEND is exclusive: a 2-night stay starting at +10 ends at +12
    start = (TODAY + dt.timedelta(days=10)).strftime("%Y%m%d")
    end = (TODAY + dt.timedelta(days=12)).strftime("%Y%m%d")
    assert f"DTSTART;VALUE=DATE:{start}" in body
    assert f"DTEND;VALUE=DATE:{end}" in body


@pytest.mark.asyncio
async def test_calendar_exports_closed_ranges_as_events(db_conn) -> None:
    seed = await _seed(db_conn, "i6@example.com")

    await db_conn.execute(
        "UPDATE inventory_day SET closed = true "
        "WHERE unit_type_id = $1 AND date >= $2 AND date < $3",
        seed["unit_type_id"],
        TODAY + dt.timedelta(days=5),
        TODAY + dt.timedelta(days=8),  # 3 closed nights: +5..+7
    )

    body = await ical_export.build_calendar(db_conn, seed["unit_type_id"])

    assert body is not None
    start = (TODAY + dt.timedelta(days=5)).strftime("%Y%m%d")
    end = (TODAY + dt.timedelta(days=8)).strftime("%Y%m%d")
    assert f"UID:closed:{seed['unit_type_id']}:{TODAY + dt.timedelta(days=5)}" in body
    assert f"DTSTART;VALUE=DATE:{start}" in body
    assert f"DTEND;VALUE=DATE:{end}" in body


@pytest.mark.asyncio
async def test_calendar_empty_when_everything_free(db_conn) -> None:
    """Free dates emit no events - channels read silence as availability."""
    seed = await _seed(db_conn, "i7@example.com")

    body = await ical_export.build_calendar(db_conn, seed["unit_type_id"])

    assert body is not None
    assert "BEGIN:VEVENT" not in body


@pytest.mark.asyncio
async def test_calendar_skips_past_stays(db_conn) -> None:
    """Past bookings are history, not availability - keep the feed clean."""
    seed = await _seed(db_conn, "i8@example.com")
    # _seed only generates forward; the past stay needs inventory too.
    await db_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series("
        "  (CURRENT_DATE - interval '7 days')::date, CURRENT_DATE, '1 day'"
        ") AS d(date) ON CONFLICT DO NOTHING",
        seed["unit_type_id"],
    )
    # a stay that ended yesterday (a web guest can no longer hold one: it came from a channel)
    await _hold(
        db_conn, seed["unit_type_id"], TODAY - dt.timedelta(days=3), nights=2, origin="channel"
    )

    body = await ical_export.build_calendar(db_conn, seed["unit_type_id"])

    assert body is not None
    assert "BEGIN:VEVENT" not in body


@pytest.mark.asyncio
async def test_calendar_none_for_unknown_unit_type(db_conn) -> None:
    assert await ical_export.build_calendar(db_conn, str(uuid.uuid4())) is None
