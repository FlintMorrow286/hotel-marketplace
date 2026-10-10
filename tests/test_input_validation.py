"""Public inputs that were accepted but meant nothing, or meant too much.

`guests=0` is not a party, `%` and `_` in the search box acted as LIKE
wildcards, a stay that does not run forward answered "dates not available"
instead of "bad request", the availability read took a ten-year range, an
integer past its column's type or a NUL in text answered 404, and the catalog's
date filter cost a night per day of whatever range it was sent.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.inventory import service as inventory_service
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate, PropertyUpdate

TODAY = dt.date.today()


async def _published(conn, partner_id: str, name: str):
    prop = await property_service.create_property(
        conn, partner_id, PropertyCreate(name=name, property_type="hotel", city="Yalta")
    )
    await property_service.update_property(
        conn, prop.id, partner_id, PropertyUpdate(status="published")
    )
    return prop


async def _published_with_room(conn, email: str) -> str:
    """A published property with one bookable room; returns the room's id."""
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await _published(conn, partner_id, "Alpha")
    unit_id = await conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', 2, 1, 5000) RETURNING id::text",
        prop.id,
    )
    await inventory_service.ensure_inventory(conn, unit_id)
    return unit_id


@pytest.mark.asyncio
async def test_search_text_is_not_a_like_pattern(db_conn) -> None:
    """`%` and `_` find a literal percent sign and underscore, not every property."""
    partner_id = await auth_service.register_partner(
        db_conn,
        PartnerRegisterRequest(email="like@example.com", password="secret123", name="Tester"),
    )
    plain = await _published(db_conn, partner_id, "Alpha")
    odd = await _published(db_conn, partner_id, "50% Beta_2")

    async def found(q: str) -> list[str]:
        return [
            p["id"] for p in (await property_service.list_public_properties(db_conn, q=q))["items"]
        ]

    assert await found("%") == [odd.id]
    assert await found("_") == [odd.id]
    assert await found("\\") == []
    assert await found("alp") == [plain.id]  # case-insensitive substring still works
    assert await found("50%") == [odd.id]


@pytest.mark.asyncio
async def test_catalog_filters_reject_nonsense(committed_conn) -> None:
    """A value the database cannot take (an integer past its column's type, a NUL in text)
    reached it, asyncpg raised DataError and the uuid handler answered a 404 "not found"."""

    def get(client: TestClient, **params: int | str) -> int:
        return client.get("/v1/properties", params=params).status_code

    with TestClient(create_app()) as client:
        statuses = {
            "guests=0": get(client, guests=0),
            "guests=-3": get(client, guests=-3),
            "guests=2": get(client, guests=2),
            "guests=int4 max": get(client, guests=2**31 - 1),
            "guests=int4 max+1": get(client, guests=2**31),
            "offset=int8 max": get(client, offset=2**63 - 1),
            "offset=int8 max+1": get(client, offset=2**63),
            "q too long": get(client, q="a" * 101),
            "q ok": get(client, q="a" * 100),
            "q NUL": get(client, q="a\x00b"),
            "city NUL": get(client, city="a\x00b"),
            "city ok": get(client, city="Коктебель"),
        }

    assert statuses == {
        "guests=0": 422,
        "guests=-3": 422,
        "guests=2": 200,
        "guests=int4 max": 200,
        "guests=int4 max+1": 422,
        "offset=int8 max": 200,
        "offset=int8 max+1": 422,
        "q too long": 422,
        "q ok": 200,
        "q NUL": 422,
        "city NUL": 422,
        "city ok": 200,
    }


@pytest.mark.asyncio
async def test_a_stay_that_does_not_run_forward_is_422(committed_conn) -> None:
    day = (TODAY + dt.timedelta(days=5)).isoformat()
    earlier = (TODAY + dt.timedelta(days=3)).isoformat()

    def hold(client: TestClient, checkin: str, checkout: str):
        return client.post(
            "/v1/bookings/hold",
            json={
                "unit_type_id": str(uuid.uuid4()),
                "checkin": checkin,
                "checkout": checkout,
                "guest": {"name": "Иван", "email": "ivan@example.com", "phone": "+79991234567"},
            },
        )

    with TestClient(create_app()) as client:
        zero_nights = hold(client, day, day)
        reversed_stay = hold(client, day, earlier)

    for response in (zero_nights, reversed_stay):
        assert response.status_code == 422, response.text
        assert "checkout must be after checkin" in response.text


@pytest.mark.asyncio
async def test_availability_range_is_capped(committed_conn) -> None:
    unit = str(uuid.uuid4())

    def read(client: TestClient, days: int):
        return client.get(
            "/v1/availability",
            params={
                "unit_type_id": unit,
                "date_from": TODAY.isoformat(),
                "date_to": (TODAY + dt.timedelta(days=days)).isoformat(),
            },
        )

    with TestClient(create_app()) as client:
        within = read(client, 366)
        too_long = read(client, 367)
        ten_years = read(client, 3650)

    assert within.status_code == 200, within.text
    assert too_long.status_code == 422 and "limited to 366 days" in too_long.text
    assert ten_years.status_code == 422


@pytest.mark.asyncio
async def test_date_filter_answers_at_the_edges(db_conn) -> None:
    """A stay matches only when every night has an open, unsold row; the checkout day
    is not a night."""
    unit_id = await _published_with_room(db_conn, "edges@example.com")
    last = await db_conn.fetchval(
        "SELECT max(date) FROM inventory_day WHERE unit_type_id = $1", unit_id
    )
    day = dt.timedelta(days=1)

    async def matches(first: dt.date, checkout: dt.date) -> bool:
        page = await property_service.list_public_properties(
            db_conn, date_from=first, date_to=checkout
        )
        return page["total"] == 1

    start = TODAY + 20 * day
    assert await matches(start, start + 3 * day)
    # The checkout day may be the one past the last generated night ...
    assert await matches(last - day, last + day)
    # ... but one more night has no row, so the room cannot be booked for it.
    assert not await matches(last - day, last + 2 * day)

    await db_conn.execute(
        "UPDATE inventory_day SET sold = available WHERE unit_type_id = $1 AND date = $2",
        unit_id,
        start + day,
    )
    assert not await matches(start, start + 3 * day)
    assert await matches(start + 2 * day, start + 5 * day)


@pytest.mark.asyncio
async def test_date_filter_costs_the_rows_it_reads_not_the_length_of_the_range(db_conn) -> None:
    """A stay from year 1 to year 9999 used to generate a night per day of it: about two
    seconds of database time per request. asyncpg sends `date.min` and `date.max` as
    -infinity and infinity, and a series from -infinity never ended."""
    await _published_with_room(db_conn, "cost@example.com")

    await db_conn.execute("SET LOCAL statement_timeout = '400ms'")
    for date_from, date_to in (
        (dt.date(1, 1, 2), dt.date(9999, 12, 30)),
        (dt.date.min, dt.date(2030, 1, 1)),
        (TODAY, dt.date.max),
        (dt.date.min, dt.date.max),
    ):
        page = await property_service.list_public_properties(
            db_conn, date_from=date_from, date_to=date_to
        )
        assert page["total"] == 0, (date_from, date_to)
