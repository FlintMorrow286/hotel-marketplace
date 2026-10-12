"""A web guest cannot hold a stay that has already begun, judged on the property's calendar."""

from __future__ import annotations

import datetime as dt

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.booking import service as booking_service
from app.utils.tz import today_at
from tests.test_booking import TODAY, _guest, _seed_unit

AHEAD = "Pacific/Kiritimati"  # UTC+14
BEHIND = "Pacific/Pago_Pago"  # UTC-11: 25 hours behind AHEAD, so their dates always differ


async def _unit_in_zone(conn, email: str, tz: str) -> str:
    """A unit type whose property sits in `tz`, with inventory reaching back a few days."""
    ut = await _seed_unit(conn, email)
    await conn.execute(
        "UPDATE property SET timezone = $2 "
        "WHERE id = (SELECT property_id FROM unit_type WHERE id = $1)",
        ut,
        tz,
    )
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series($2::date, $3::date, '1 day') AS d(date) "
        "ON CONFLICT (unit_type_id, date) DO NOTHING",
        ut,
        TODAY - dt.timedelta(days=4),
        TODAY - dt.timedelta(days=1),
    )
    return ut


async def _hold(conn, ut: str, checkin: dt.date, origin: str = "web") -> dict:
    return await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=1),
        origin=origin,
        **_guest(),
    )


def test_the_two_zones_never_share_a_date() -> None:
    assert today_at(AHEAD) > today_at(BEHIND)


@pytest.mark.asyncio
async def test_yesterday_at_the_property_is_refused(db_conn) -> None:
    ut = await _unit_in_zone(db_conn, "past-1@example.com", BEHIND)

    with pytest.raises(booking_service.NotAvailable, match="in the past"):
        await _hold(db_conn, ut, today_at(BEHIND) - dt.timedelta(days=1))


@pytest.mark.asyncio
async def test_today_at_the_property_is_allowed_even_when_the_server_is_a_day_behind(
    db_conn,
) -> None:
    """Where it is already tomorrow, tomorrow's guests are today's: the server's date is not the rule."""
    ut = await _unit_in_zone(db_conn, "past-2@example.com", AHEAD)
    await db_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series($2::date, $3::date, '1 day') AS d(date) "
        "ON CONFLICT (unit_type_id, date) DO NOTHING",
        ut,
        today_at(AHEAD),
        today_at(AHEAD) + dt.timedelta(days=2),
    )

    hold = await _hold(db_conn, ut, today_at(AHEAD))

    assert hold["status"] == "hold"


@pytest.mark.asyncio
async def test_a_date_that_is_past_in_one_zone_is_open_in_another(db_conn) -> None:
    behind_ut = await _unit_in_zone(db_conn, "past-3a@example.com", BEHIND)
    ahead_ut = await _unit_in_zone(db_conn, "past-3b@example.com", AHEAD)
    same_date = today_at(BEHIND)  # today there, yesterday (at least) at the other property

    assert (await _hold(db_conn, behind_ut, same_date))["status"] == "hold"
    with pytest.raises(booking_service.NotAvailable, match="in the past"):
        await _hold(db_conn, ahead_ut, same_date)


@pytest.mark.asyncio
async def test_a_channel_may_push_a_stay_that_is_under_way(db_conn) -> None:
    """An OTA reports what it already sold; the night is gone from inventory either way."""
    ut = await _unit_in_zone(db_conn, "past-4@example.com", BEHIND)

    hold = await _hold(db_conn, ut, today_at(BEHIND) - dt.timedelta(days=1), origin="channel")

    assert hold["status"] == "hold"


@pytest.mark.asyncio
async def test_the_api_answers_409_and_touches_no_inventory(committed_conn) -> None:
    ut = await _unit_in_zone(committed_conn, "past-5@example.com", BEHIND)
    yesterday = today_at(BEHIND) - dt.timedelta(days=1)
    guest = _guest()

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/bookings/hold",
            json={
                "unit_type_id": ut,
                "checkin": yesterday.isoformat(),
                "checkout": today_at(BEHIND).isoformat(),
                "guest": {
                    "name": guest["guest_name"],
                    "email": guest["guest_email"],
                    "phone": guest["guest_phone"],
                },
            },
        )

    assert response.status_code == 409, response.text
    assert "check-in date is in the past" in response.json()["detail"]
    held = await committed_conn.fetchval(
        "SELECT coalesce(sum(hold), 0) FROM inventory_day WHERE unit_type_id = $1", ut
    )
    assert held == 0
