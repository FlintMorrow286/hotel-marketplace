"""Slice 3 tests: booking hold + idempotency + reaper.

The concurrency test is the most important in the whole project: 20 parallel
holds on a single room must produce exactly one success.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import asyncpg
import pytest

from app.config.settings import settings
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service
from app.modules.booking.service import Conflict, NotAvailable
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate

TODAY = dt.date.today()


async def _seed_unit(conn: asyncpg.Connection, email: str, total_units: int = 1) -> str:
    """Create partner + property + unit_type + 30 days of inventory."""
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


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_hold_success(db_conn) -> None:
    ut = await _seed_unit(db_conn, "b1@example.com")
    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=3), **_guest()
    )

    assert hold["status"] == "hold"
    assert hold["code"].startswith("BK-")
    assert hold["total_amount"] == 9000  # 3 nights x 3000

    free = await db_conn.fetchval(
        "SELECT available - hold - sold FROM inventory_day WHERE unit_type_id=$1 AND date=$2",
        ut,
        TODAY,
    )
    assert free == 0
    assert await db_conn.fetchval("SELECT count(*) FROM booking_line") == 3


@pytest.mark.asyncio
async def test_hold_then_cancel_returns_inventory(db_conn) -> None:
    ut = await _seed_unit(db_conn, "b2@example.com")
    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=2), **_guest()
    )

    await service.release_hold(db_conn, hold["id"])

    free = await db_conn.fetchval(
        "SELECT available - hold - sold FROM inventory_day WHERE unit_type_id=$1 AND date=$2",
        ut,
        TODAY,
    )
    assert free == 1
    status_now = await db_conn.fetchval("SELECT status FROM booking WHERE id=$1", hold["id"])
    assert status_now == "cancelled"


# ---------------------------------------------------------------- double booking


@pytest.mark.asyncio
async def test_overlapping_range_rejected(db_conn) -> None:
    """A second hold overlapping the first must fail on a single room."""
    ut = await _seed_unit(db_conn, "b3@example.com")
    await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=5), **_guest()
    )

    with pytest.raises(NotAvailable):
        await service.create_hold(
            db_conn,
            unit_type_id=ut,
            checkin=TODAY + dt.timedelta(days=3),
            checkout=TODAY + dt.timedelta(days=7),
            **_guest(),
        )


@pytest.mark.asyncio
async def test_closed_dates_rejected(db_conn) -> None:
    ut = await _seed_unit(db_conn, "b4@example.com")
    await db_conn.execute(
        "UPDATE inventory_day SET closed = true WHERE unit_type_id=$1 AND date >= $2",
        ut,
        TODAY + dt.timedelta(days=5),
    )
    with pytest.raises(NotAvailable):
        await service.create_hold(
            db_conn,
            unit_type_id=ut,
            checkin=TODAY + dt.timedelta(days=5),
            checkout=TODAY + dt.timedelta(days=8),
            **_guest(),
        )


# ---------------------------------------------------------------- idempotency


@pytest.mark.asyncio
async def test_idempotency_replay_returns_same_booking(db_conn) -> None:
    ut = await _seed_unit(db_conn, "b5@example.com")
    key = f"client-{uuid.uuid4()}"
    guest = _guest()

    first = await service.create_hold(
        db_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        idempotency_key=key,
        **guest,
    )
    second = await service.create_hold(
        db_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        idempotency_key=key,
        **guest,
    )

    assert first["id"] == second["id"]
    assert await db_conn.fetchval("SELECT count(*) FROM booking") == 1


@pytest.mark.asyncio
async def test_idempotency_conflict_on_different_payload(db_conn) -> None:
    """Reusing a key with different dates must fail loudly."""
    ut = await _seed_unit(db_conn, "b6@example.com")
    key = f"client-{uuid.uuid4()}"

    await service.create_hold(
        db_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        idempotency_key=key,
        **_guest(),
    )
    with pytest.raises(Conflict):
        await service.create_hold(
            db_conn,
            unit_type_id=ut,
            checkin=TODAY + dt.timedelta(days=10),
            checkout=TODAY + dt.timedelta(days=12),
            idempotency_key=key,
            **_guest(),
        )


@pytest.mark.asyncio
async def test_idempotency_same_key_other_guest_is_conflict(db_conn) -> None:
    """A replay must repeat the guest too: another traveller is not a retry.

    Two different guests on the same dates with a colliding key must be refused
    loudly rather than silently replaying the first booking.
    """
    ut = await _seed_unit(db_conn, "b6b@example.com", total_units=1)
    key = f"client-{uuid.uuid4()}"
    other = {
        "guest_name": "Пётр Другой",
        "guest_email": "other@example.com",
        "guest_phone": "+79990000000",
    }

    await service.create_hold(
        db_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        idempotency_key=key,
        **_guest(),
    )
    with pytest.raises(Conflict):
        await service.create_hold(
            db_conn,
            unit_type_id=ut,
            checkin=TODAY,
            checkout=TODAY + dt.timedelta(days=2),
            idempotency_key=key,
            **other,
        )


async def _lookup_fixture(committed_conn, email: str) -> tuple[dict, dict]:
    """A committed hold and the guest who made it."""
    ut = await _seed_unit(committed_conn, email)
    guest = _guest()
    hold = await service.create_hold(
        committed_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        **guest,
    )
    return hold, guest


@pytest.mark.asyncio
async def test_booking_lookup_roundtrip(committed_conn) -> None:
    """The guest knows the BK-XXXXXX code and the booking's email, never the id.

    A lowercase typed code or email still finds the booking: both are normalised.
    """
    from starlette.testclient import TestClient

    from app.main import create_app

    hold, guest = await _lookup_fixture(committed_conn, "b6c@example.com")

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/bookings/lookup",
            json={"code": hold["code"].lower(), "email": guest["guest_email"].upper()},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == hold["id"]
    assert body["code"] == hold["code"]
    assert len(body["lines"]) == 2


@pytest.mark.asyncio
async def test_booking_lookup_needs_the_booking_email(committed_conn) -> None:
    """The code alone must not hand out the booking id, which cancels and refunds."""
    from starlette.testclient import TestClient

    from app.main import create_app

    hold, _ = await _lookup_fixture(committed_conn, "b6d@example.com")

    with TestClient(create_app()) as client:
        wrong_email = client.post(
            "/v1/bookings/lookup", json={"code": hold["code"], "email": "someone@else.example"}
        )
        no_email = client.post("/v1/bookings/lookup", json={"code": hold["code"]})
        old_route = client.get(f"/v1/bookings/by-code/{hold['code']}")

    assert wrong_email.status_code == 404, wrong_email.text
    assert hold["id"] not in wrong_email.text
    assert no_email.status_code == 422, no_email.text
    assert hold["id"] not in old_route.text


@pytest.mark.asyncio
async def test_booking_lookup_unknown_code_is_404(committed_conn) -> None:
    from starlette.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/bookings/lookup", json={"code": "BK-NOPE00", "email": "nobody@example.com"}
        )

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------- reaper


@pytest.mark.asyncio
async def test_reaper_expires_stale_hold(db_conn) -> None:
    ut = await _seed_unit(db_conn, "b7@example.com")
    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=2), **_guest()
    )
    # force expiry into the past
    await db_conn.execute(
        "UPDATE booking SET hold_expires_at = now() - interval '1 minute' WHERE id = $1",
        hold["id"],
    )

    released = await service.expire_holds(db_conn)
    assert released == 1

    free = await db_conn.fetchval(
        "SELECT available - hold - sold FROM inventory_day WHERE unit_type_id=$1 AND date=$2",
        ut,
        TODAY,
    )
    assert free == 1


# ---------------------------------------------------------------- CONCURRENCY


@pytest.mark.asyncio
async def test_concurrent_holds_single_room(_test_db) -> None:
    """20 parallel requests for the same single room -> exactly one success.

    Uses its own pool (committed seed) because the racing connections must all
    see the same data; cleans everything up afterwards.
    """
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=5, max_size=25)
    try:
        async with pool.acquire() as setup:
            ut = await _seed_unit(setup, "b8@example.com", total_units=1)

        async def attempt(i: int):
            async with pool.acquire() as conn:
                try:
                    async with conn.transaction(isolation="serializable"):
                        try:
                            return await service.create_hold(
                                conn,
                                unit_type_id=ut,
                                checkin=TODAY,
                                checkout=TODAY + dt.timedelta(days=2),
                                idempotency_key=f"race-{uuid.uuid4()}",
                                guest_name=f"Гость {i}",
                                guest_email=f"race{i}@example.com",
                                guest_phone="+79991234567",
                            )
                        except NotAvailable:
                            return None
                except asyncpg.SerializationError:
                    # Lost the race: SERIALIZABLE aborted this transaction
                    # because another concurrent hold touched the same rows.
                    return None

        results = await asyncio.gather(*(attempt(i) for i in range(20)))
        successes = [r for r in results if r is not None]

        assert len(successes) == 1, f"expected exactly 1 success, got {len(successes)}"

        async with pool.acquire() as check:
            holds = await check.fetchval(
                "SELECT count(*) FROM booking WHERE unit_type_id = $1 AND status = 'hold'",
                ut,
            )
            assert holds == 1
    finally:
        async with pool.acquire() as cleanup:
            await cleanup.execute("DELETE FROM booking_line")
            await cleanup.execute("DELETE FROM booking")
            await cleanup.execute("DELETE FROM inventory_day")
            await cleanup.execute("DELETE FROM unit_type")
            await cleanup.execute("DELETE FROM property")
            await cleanup.execute("DELETE FROM partner")
        await pool.close()


# ---------------------------------------------------------------- rate-plan pricing


async def _seed_unit_with_rate_plan(
    conn: asyncpg.Connection, email: str, base_price: float, rate_price: float
) -> str:
    """A unit type with an active rate plan that reprices its whole window.

    The partner sets a seasonal rate over the stay's nights; the hold must
    charge that, not the unit's base price.
    """
    from app.modules.rate import service as rate_service

    ut = await _seed_unit(conn, email)
    await conn.execute("UPDATE unit_type SET base_price = $2 WHERE id = $1", ut, base_price)

    rp = await rate_service.create_rate_plan(conn, ut, "Сезон")
    await rate_service.set_prices(
        conn,
        rp["id"],
        TODAY,
        TODAY + dt.timedelta(days=29),
        price=rate_price,
    )
    return ut


@pytest.mark.asyncio
async def test_hold_charges_the_rate_plan_price(db_conn) -> None:
    """The hold charges what the availability read shows the guest.

    `get_availability` prices a night from the active rate plan and falls back
    to base_price. The hold used to ignore the plan and charge base_price, so
    a guest who saw 9990 ₽ on the checkout was charged 6500 ₽ — and the
    commission, the refund and the partner's report were all computed from the
    wrong number.
    """
    ut = await _seed_unit_with_rate_plan(db_conn, "rp-1@example.com", 6500, 9990)

    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=3), **_guest()
    )

    assert hold["total_amount"] == 9990 * 3

    lines = await db_conn.fetch(
        "SELECT price::float8 FROM booking_line WHERE booking_id = $1 ORDER BY date", hold["id"]
    )
    assert [line["price"] for line in lines] == [9990, 9990, 9990]


@pytest.mark.asyncio
async def test_hold_falls_back_to_base_price_without_price_rows(db_conn) -> None:
    """A rate plan with no rows for the night keeps the base price.

    The partner creates the plan for its cancellation terms but prices only
    part of the season; the unpriced nights fall back, exactly as
    `get_availability` does.
    """
    from app.modules.rate import service as rate_service

    ut = await _seed_unit(db_conn, "rp-2@example.com")
    await db_conn.execute("UPDATE unit_type SET base_price = $2 WHERE id = $1", ut, 4000)
    await rate_service.create_rate_plan(db_conn, ut, "Основной")

    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=2), **_guest()
    )

    assert hold["total_amount"] == 4000 * 2


@pytest.mark.asyncio
async def test_hold_mixes_priced_and_unpriced_nights(db_conn) -> None:
    """A stay straddling the priced and the unpriced part is summed per night.

    Nights with a price row pay it, the rest pay base_price — never a flat
    average and never one or the other.
    """
    from app.modules.rate import service as rate_service

    base = 4000
    seasonal = 9000
    ut = await _seed_unit(db_conn, "rp-3@example.com")
    await db_conn.execute("UPDATE unit_type SET base_price = $2 WHERE id = $1", ut, base)

    rp = await rate_service.create_rate_plan(db_conn, ut, "Сезон")
    # Only the first two nights of the stay are priced; the third falls back.
    await rate_service.set_prices(
        db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=2), price=seasonal
    )

    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=3), **_guest()
    )

    assert hold["total_amount"] == seasonal * 2 + base


# --------------------------------------------------- commission from the plan


@pytest.mark.asyncio
async def test_hold_snapshots_plan_commission(db_conn) -> None:
    """The rate plan's commission share is snapshotted on the booking.

    A later change to the plan must not reprice a booking already confirmed,
    so the rate is read at hold time and written onto the row.
    """
    from app.modules.rate import service as rate_service

    ut = await _seed_unit(db_conn, "b20@example.com")
    await rate_service.create_rate_plan(db_conn, ut, "Летний")
    plan_id = await db_conn.fetchval("SELECT id::text FROM rate_plan WHERE unit_type_id = $1", ut)
    await rate_service.update_rate_plan(db_conn, plan_id, 0.20)

    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=2), **_guest()
    )

    row = await db_conn.fetchrow(
        "SELECT commission_rate::float8, commission_amt::float8 FROM booking WHERE id = $1",
        hold["id"],
    )
    # 2 nights x 3000 = 6000, at 20%.
    assert row["commission_rate"] == 0.20
    assert row["commission_amt"] == 1200


@pytest.mark.asyncio
async def test_hold_without_plan_uses_default_commission(db_conn) -> None:
    """No rate plan: the platform default is snapshotted instead."""
    from app.config import legal

    ut = await _seed_unit(db_conn, "b21@example.com")
    hold = await service.create_hold(
        db_conn, unit_type_id=ut, checkin=TODAY, checkout=TODAY + dt.timedelta(days=2), **_guest()
    )

    rate = await db_conn.fetchval(
        "SELECT commission_rate::float8 FROM booking WHERE id = $1", hold["id"]
    )
    assert rate == legal.COMMISSION_DEFAULT_RATE
