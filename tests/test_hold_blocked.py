"""Staff blocked a property: guests with an old link cannot book it, the partner's channel still can."""

from __future__ import annotations

import datetime as dt

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.booking import service as booking_service
from tests.test_booking import TODAY, _guest, _seed_unit


async def _set_status(conn, ut: str, status: str) -> None:
    await conn.execute(
        "UPDATE property SET status = $2 "
        "WHERE id = (SELECT property_id FROM unit_type WHERE id = $1)",
        ut,
        status,
    )


async def _hold(conn, ut: str, origin: str = "web") -> dict:
    return await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=1),
        origin=origin,
        **_guest(),
    )


@pytest.mark.asyncio
async def test_a_blocked_property_takes_no_web_booking(db_conn) -> None:
    ut = await _seed_unit(db_conn, "blk-1@example.com")
    await _set_status(db_conn, ut, "blocked")

    with pytest.raises(booking_service.NotAvailable, match="not available for booking"):
        await _hold(db_conn, ut)

    held = await db_conn.fetchval(
        "SELECT coalesce(sum(hold), 0) FROM inventory_day WHERE unit_type_id = $1", ut
    )
    assert held == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "pending_moderation", "published"])
async def test_every_other_status_still_books(db_conn, status) -> None:
    """Drafts are not in the catalog and cannot be reached by a guest; they stay bookable by id."""
    ut = await _seed_unit(db_conn, f"blk-{status}@example.com")
    await _set_status(db_conn, ut, status)

    assert (await _hold(db_conn, ut))["status"] == "hold"


@pytest.mark.asyncio
async def test_a_channel_booking_is_recorded_even_on_a_blocked_property(db_conn) -> None:
    """The OTA already sold it; refusing would only lose the record."""
    ut = await _seed_unit(db_conn, "blk-ch@example.com")
    await _set_status(db_conn, ut, "blocked")

    assert (await _hold(db_conn, ut, origin="channel"))["status"] == "hold"


@pytest.mark.asyncio
async def test_the_api_answers_409(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "blk-api@example.com")
    await _set_status(committed_conn, ut, "blocked")
    guest = _guest()

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/bookings/hold",
            json={
                "unit_type_id": ut,
                "checkin": TODAY.isoformat(),
                "checkout": (TODAY + dt.timedelta(days=1)).isoformat(),
                "guest": {
                    "name": guest["guest_name"],
                    "email": guest["guest_email"],
                    "phone": guest["guest_phone"],
                },
            },
        )

    assert response.status_code == 409, response.text
    assert "not available for booking" in response.json()["detail"]
