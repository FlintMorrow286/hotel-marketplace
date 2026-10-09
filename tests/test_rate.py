"""Slice 5 tests: rate plans, prices and the partner calendar grid."""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.modules.rate import calendar, service

TODAY = dt.date.today()


async def _seed(conn: asyncpg.Connection, email: str, base_price: float = 3000) -> dict:
    """Partner + property + unit_type + inventory. Returns ids."""
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
        "SELECT $1, d.date, 1 FROM generate_series($2::date, ($2::date + 29)::date, '1 day') AS d(date)",
        row["id"],
        TODAY,
    )
    return {"unit_type_id": row["id"], "partner_id": str(partner_id), "property_id": prop.id}


# ---------------------------------------------------------------- rate plans


@pytest.mark.asyncio
async def test_create_and_list_rate_plan(db_conn) -> None:
    seed = await _seed(db_conn, "r1@example.com")
    rp = await service.create_rate_plan(db_conn, seed["unit_type_id"], "Гибкий")

    assert rp["name"] == "Гибкий"
    assert rp["active"] is True

    plans = await service.list_rate_plans(db_conn, seed["unit_type_id"])
    assert len(plans) == 1


@pytest.mark.asyncio
async def test_second_active_rate_plan_is_rejected(db_conn) -> None:
    """Two active plans priced every night twice and made the room unbookable."""
    seed = await _seed(db_conn, "r-one-active@example.com")
    await service.create_rate_plan(db_conn, seed["unit_type_id"], "Основной")

    with pytest.raises(service.ActivePlanExists):
        await service.create_rate_plan(db_conn, seed["unit_type_id"], "Второй")

    # The rejected insert leaves the transaction usable and nothing behind.
    plans = await service.list_rate_plans(db_conn, seed["unit_type_id"])
    assert [p["name"] for p in plans] == ["Основной"]


@pytest.mark.asyncio
async def test_inactive_rate_plan_does_not_block_a_new_one(db_conn) -> None:
    seed = await _seed(db_conn, "r-inactive@example.com")
    old = await service.create_rate_plan(db_conn, seed["unit_type_id"], "Старый")
    await db_conn.execute("UPDATE rate_plan SET active = false WHERE id = $1", old["id"])

    new = await service.create_rate_plan(db_conn, seed["unit_type_id"], "Новый")

    plans = await service.list_rate_plans(db_conn, seed["unit_type_id"])
    assert [p["id"] for p in plans if p["active"]] == [new["id"]]


@pytest.mark.asyncio
async def test_second_rate_plan_over_http_is_409(committed_conn) -> None:
    """The cabinet's "create plan" fired twice (two tabs) answers 409, not a broken room."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    seed = await _seed(committed_conn, "r-http@example.com")
    token = create_access_token(seed["partner_id"], scope="partner")
    headers = {"Authorization": f"Bearer {token}"}
    body = {"unit_type_id": seed["unit_type_id"], "name": "Основной тариф"}

    with TestClient(create_app()) as client:
        first = client.post("/v1/partner/rate-plans", json=body, headers=headers)
        second = client.post("/v1/partner/rate-plans", json=body, headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert len(await service.list_rate_plans(committed_conn, seed["unit_type_id"])) == 1


# ---------------------------------------------------------------- prices


@pytest.mark.asyncio
async def test_set_prices_over_range(db_conn) -> None:
    seed = await _seed(db_conn, "r2@example.com")
    rp = await service.create_rate_plan(db_conn, seed["unit_type_id"], "Сезон")

    await service.set_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=5), price=5000)

    prices = await service.get_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=5))
    assert len(prices) == 5
    assert all(p["price"] == 5000 for p in prices)


@pytest.mark.asyncio
async def test_price_falls_back_to_base(db_conn) -> None:
    """Without explicit price rows, the unit's base price applies."""
    seed = await _seed(db_conn, "r3@example.com", base_price=2500)
    rp = await service.create_rate_plan(db_conn, seed["unit_type_id"], "База")

    prices = await service.get_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=3))
    assert len(prices) == 3
    assert all(p["price"] == 2500 for p in prices)


@pytest.mark.asyncio
async def test_set_prices_overwrites_existing(db_conn) -> None:
    seed = await _seed(db_conn, "r4@example.com")
    rp = await service.create_rate_plan(db_conn, seed["unit_type_id"], "Сезон")

    await service.set_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=3), price=5000)
    await service.set_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=3), price=7000)

    prices = await service.get_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=3))
    assert all(p["price"] == 7000 for p in prices)


@pytest.mark.asyncio
async def test_set_prices_rejects_bad_range(db_conn) -> None:
    seed = await _seed(db_conn, "r5@example.com")
    rp = await service.create_rate_plan(db_conn, seed["unit_type_id"], "Сезон")

    with pytest.raises(ValueError):
        await service.set_prices(db_conn, rp["id"], TODAY, TODAY, price=1000)
    with pytest.raises(ValueError):
        await service.set_prices(db_conn, rp["id"], TODAY, TODAY + dt.timedelta(days=2), price=-100)


# ---------------------------------------------------------------- calendar


@pytest.mark.asyncio
async def test_calendar_shape_and_free(db_conn) -> None:
    seed = await _seed(db_conn, "r6@example.com")
    grid = await calendar.get_calendar(
        db_conn, seed["partner_id"], TODAY, TODAY + dt.timedelta(days=4)
    )

    assert grid["date_from"] == TODAY.isoformat()
    assert len(grid["units"]) == 1

    unit = grid["units"][0]
    assert unit["unit_type_id"] == seed["unit_type_id"]
    assert unit["total_units"] == 1
    assert len(unit["days"]) == 4

    day = unit["days"][0]
    assert set(day.keys()) == {
        "date",
        "available",
        "hold",
        "sold",
        "free",
        "closed",
        "price",
        "min_stay",
    }
    assert day["free"] == 1
    assert day["closed"] is False
    assert day["price"] == 3000


@pytest.mark.asyncio
async def test_calendar_reflects_hold(db_conn) -> None:
    """A held night shows hold=1, free=0."""
    from app.modules.booking import service as booking_service

    seed = await _seed(db_conn, "r7@example.com")
    await booking_service.create_hold(
        db_conn,
        unit_type_id=seed["unit_type_id"],
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        guest_name="Иван",
        guest_email=f"g{uuid.uuid4().hex[:6]}@example.com",
        guest_phone="+79991234567",
        idempotency_key=f"k-{uuid.uuid4()}",
    )

    grid = await calendar.get_calendar(
        db_conn, seed["partner_id"], TODAY, TODAY + dt.timedelta(days=4)
    )
    days = grid["units"][0]["days"]

    assert days[0]["hold"] == 1 and days[0]["free"] == 0
    assert days[1]["hold"] == 1 and days[1]["free"] == 0
    assert days[2]["hold"] == 0 and days[2]["free"] == 1


@pytest.mark.asyncio
async def test_calendar_respects_partner_scope(db_conn) -> None:
    """A partner must not see another partner's units in the grid."""
    seed_a = await _seed(db_conn, "a@r8.example.com")
    seed_b = await _seed(db_conn, "b@r8.example.com")

    grid_a = await calendar.get_calendar(
        db_conn, seed_a["partner_id"], TODAY, TODAY + dt.timedelta(days=2)
    )
    grid_b = await calendar.get_calendar(
        db_conn, seed_b["partner_id"], TODAY, TODAY + dt.timedelta(days=2)
    )

    assert [u["unit_type_id"] for u in grid_a["units"]] == [seed_a["unit_type_id"]]
    assert [u["unit_type_id"] for u in grid_b["units"]] == [seed_b["unit_type_id"]]


@pytest.mark.asyncio
async def test_calendar_rejects_long_range(db_conn) -> None:
    seed = await _seed(db_conn, "r9@example.com")
    with pytest.raises(ValueError, match="92"):
        await calendar.get_calendar(
            db_conn, seed["partner_id"], TODAY, TODAY + dt.timedelta(days=200)
        )


@pytest.mark.asyncio
async def test_calendar_marks_ungenerated_day_as_closed(db_conn) -> None:
    """A day the inventory generator never covered is not free-with-zero.

    The chessboard used to draw such days as "свободно" with 0 rooms: the
    LEFT JOIN yields NULL available and the COALESCE turns it into a free cell
    the partner cannot honour. A missing inventory_day means the room is not
    for sale that day — same rule the guest-facing catalog applies — so the
    grid must report it closed.
    """
    seed = await _seed(db_conn, "r10@example.com")
    # _seed covers 30 days from TODAY; ask for a day well past that window.
    grid = await calendar.get_calendar(
        db_conn,
        seed["partner_id"],
        TODAY + dt.timedelta(days=60),
        TODAY + dt.timedelta(days=62),
    )

    unit = grid["units"][0]
    assert len(unit["days"]) == 2
    for day in unit["days"]:
        assert day["closed"] is True
        assert day["free"] == 0


# ------------------------------------------------------- commission on a plan


@pytest.mark.asyncio
async def test_rate_plan_commission_override(db_conn) -> None:
    """A rate plan with a commission rate beats the platform default."""
    from app.config import legal

    seed = await _seed(db_conn, "r11@example.com")
    await service.create_rate_plan(db_conn, seed["unit_type_id"], "Летний")
    plan_id = await db_conn.fetchval(
        "SELECT id::text FROM rate_plan WHERE unit_type_id = $1", seed["unit_type_id"]
    )
    updated = await service.update_rate_plan(db_conn, plan_id, 0.20)
    assert updated["commission_rate"] == 0.20

    # get_effective_rate returns the override, not the 12% default.
    found, rate = await service.get_effective_rate(db_conn, seed["unit_type_id"])
    assert found == plan_id
    assert rate == 0.20
    assert rate != legal.COMMISSION_DEFAULT_RATE


@pytest.mark.asyncio
async def test_rate_plan_commission_null_falls_back(db_conn) -> None:
    """A plan without a rate falls back to the platform default."""
    from app.config import legal

    seed = await _seed(db_conn, "r12@example.com")
    await service.create_rate_plan(db_conn, seed["unit_type_id"], "Гибкий")

    found, rate = await service.get_effective_rate(db_conn, seed["unit_type_id"])
    assert rate == legal.COMMISSION_DEFAULT_RATE
    assert found is not None


@pytest.mark.asyncio
async def test_no_rate_plan_uses_default(db_conn) -> None:
    """No rate plan at all: the default still applies, and the source is None."""
    from app.config import legal

    seed = await _seed(db_conn, "r13@example.com")
    found, rate = await service.get_effective_rate(db_conn, seed["unit_type_id"])
    assert rate == legal.COMMISSION_DEFAULT_RATE
    assert found is None


@pytest.mark.asyncio
async def test_clearing_commission_returns_to_default(db_conn) -> None:
    """null clears the override back to the platform default."""
    from app.config import legal

    seed = await _seed(db_conn, "r14@example.com")
    await service.create_rate_plan(db_conn, seed["unit_type_id"], "Летний")
    plan_id = await db_conn.fetchval(
        "SELECT id::text FROM rate_plan WHERE unit_type_id = $1", seed["unit_type_id"]
    )
    await service.update_rate_plan(db_conn, plan_id, 0.25)
    await service.update_rate_plan(db_conn, plan_id, None)

    row = await db_conn.fetchrow("SELECT commission_rate FROM rate_plan WHERE id = $1", plan_id)
    assert row["commission_rate"] is None
    _, rate = await service.get_effective_rate(db_conn, seed["unit_type_id"])
    assert rate == legal.COMMISSION_DEFAULT_RATE


@pytest.mark.asyncio
async def test_commission_rate_out_of_range_rejected(db_conn) -> None:
    """A share is 0..1; anything else is a programming error."""
    seed = await _seed(db_conn, "r15@example.com")
    await service.create_rate_plan(db_conn, seed["unit_type_id"], "Летний")
    plan_id = await db_conn.fetchval(
        "SELECT id::text FROM rate_plan WHERE unit_type_id = $1", seed["unit_type_id"]
    )
    with pytest.raises(ValueError, match="between 0 and 1"):
        await service.update_rate_plan(db_conn, plan_id, 1.5)
