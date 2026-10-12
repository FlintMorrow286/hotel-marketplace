"""The public API serves what the catalog shows: a guest cannot read or book what is not published.

A draft, or a property waiting for moderation, keeps valid unit type ids that only its partner knows.
An id that leaks (a pasted dashboard URL, a log line) must not open inventory the catalog hides, and
whatever is later decided about partners publishing themselves, the booking gate stays aligned with
the catalog. A channel push and the partner's own reads are not the public API and are unchanged.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.booking import service as booking_service
from app.modules.inventory import service as inventory_service
from tests.test_booking import TODAY, _guest, _seed_unit

HIDDEN = ["draft", "pending_moderation", "blocked"]


async def _set_status(conn, ut: str, status: str) -> None:
    await conn.execute(
        "UPDATE property SET status = $2 "
        "WHERE id = (SELECT property_id FROM unit_type WHERE id = $1)",
        ut,
        status,
    )


def _hold_body(ut: str) -> dict:
    guest = _guest()
    return {
        "unit_type_id": ut,
        "checkin": TODAY.isoformat(),
        "checkout": (TODAY + dt.timedelta(days=1)).isoformat(),
        "guest": {
            "name": guest["guest_name"],
            "email": guest["guest_email"],
            "phone": guest["guest_phone"],
        },
    }


def _availability(client: TestClient, ut: str):
    return client.get(
        "/v1/availability",
        params={
            "unit_type_id": ut,
            "date_from": TODAY.isoformat(),
            "date_to": (TODAY + dt.timedelta(days=3)).isoformat(),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", HIDDEN)
async def test_a_guest_cannot_hold_what_the_catalog_hides(committed_conn, status) -> None:
    ut = await _seed_unit(committed_conn, f"pub-hold-{status}@example.com")
    await _set_status(committed_conn, ut, status)

    with TestClient(create_app()) as client:
        response = client.post("/v1/bookings/hold", json=_hold_body(ut))

    assert response.status_code == 409, response.text
    assert "not available for booking" in response.json()["detail"]
    held = await committed_conn.fetchval(
        "SELECT coalesce(sum(hold), 0) FROM inventory_day WHERE unit_type_id = $1", ut
    )
    assert held == 0


@pytest.mark.asyncio
async def test_a_guest_can_hold_a_published_property(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "pub-hold-ok@example.com")
    await _set_status(committed_conn, ut, "published")

    with TestClient(create_app()) as client:
        response = client.post("/v1/bookings/hold", json=_hold_body(ut))

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", HIDDEN)
async def test_availability_of_what_the_catalog_hides_looks_like_an_unknown_unit(
    committed_conn, status
) -> None:
    ut = await _seed_unit(committed_conn, f"pub-av-{status}@example.com")
    await _set_status(committed_conn, ut, status)

    with TestClient(create_app()) as client:
        hidden = _availability(client, ut)
        unknown = _availability(client, str(uuid.uuid4()))

    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["days"] == []
    assert hidden.json()["days"] == unknown.json()["days"]
    assert "price" not in hidden.text


@pytest.mark.asyncio
async def test_availability_of_a_published_property_is_served(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "pub-av-ok@example.com")
    await _set_status(committed_conn, ut, "published")

    with TestClient(create_app()) as client:
        response = _availability(client, ut)

    days = response.json()["days"]
    assert response.status_code == 200 and len(days) == 3
    assert {d["price"] for d in days} == {3000.0}


@pytest.mark.asyncio
async def test_the_service_serves_everything_unless_asked_for_the_public_view(db_conn) -> None:
    """The partner's calendar, the channel and the tests read and book drafts by id."""
    ut = await _seed_unit(db_conn, "pub-svc@example.com")
    to = TODAY + dt.timedelta(days=2)

    assert len(await inventory_service.get_availability(db_conn, ut, TODAY, to)) == 2
    assert (
        await inventory_service.get_availability(db_conn, ut, TODAY, to, published_only=True)
    ) == []

    with pytest.raises(booking_service.NotAvailable, match="not available for booking"):
        await booking_service.create_hold(
            db_conn,
            unit_type_id=ut,
            checkin=TODAY,
            checkout=TODAY + dt.timedelta(days=1),
            published_only=True,
            **_guest(),
        )
    held = await booking_service.create_hold(
        db_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=1),
        **_guest(),
    )
    assert held["status"] == "hold"
