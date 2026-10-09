"""Public inputs that were accepted but meant nothing, or meant too much.

`guests=0` is not a party, `%` and `_` in the search box acted as LIKE
wildcards, a stay that does not run forward answered "dates not available"
instead of "bad request", and the availability read took a ten-year range.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
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
    with TestClient(create_app()) as client:
        statuses = {
            "guests=0": client.get("/v1/properties", params={"guests": 0}).status_code,
            "guests=-3": client.get("/v1/properties", params={"guests": -3}).status_code,
            "guests=2": client.get("/v1/properties", params={"guests": 2}).status_code,
            "q too long": client.get("/v1/properties", params={"q": "a" * 101}).status_code,
            "q ok": client.get("/v1/properties", params={"q": "a" * 100}).status_code,
        }

    assert statuses == {
        "guests=0": 422,
        "guests=-3": 422,
        "guests=2": 200,
        "q too long": 422,
        "q ok": 200,
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
