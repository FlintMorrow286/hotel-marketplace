"""A guest who asks again with the same Idempotency-Key after the booking is over gets a new booking.

The SPA derives the key from the stay (unit type, dates, email), so "book the same dates
again" and "retry the request" look identical to the server. A retry of a LIVE booking must
still replay it; a booking that has ended (expired, cancelled, refunded) must not lock the
guest out of the same dates for ever.
"""

from __future__ import annotations

import datetime as dt

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.booking import access
from app.modules.booking import service as booking_service
from tests._consent import consent
from tests.test_booking import TODAY, _guest, _seed_unit

KEY = {"Idempotency-Key": "hold-same-stay"}


def _body(ut: str, guest: dict) -> dict:
    return {
        "unit_type_id": ut,
        "checkin": TODAY.isoformat(),
        "checkout": (TODAY + dt.timedelta(days=2)).isoformat(),
        "guest": {
            "name": guest["guest_name"],
            "email": guest["guest_email"],
            "phone": guest["guest_phone"],
        },
        "consent": consent(),
    }


def _token(hold: dict) -> dict:
    return {access.HEADER: hold["access_token"]}


async def _inventory(conn, ut: str) -> tuple[int, int]:
    row = await conn.fetchrow(
        "SELECT hold, sold FROM inventory_day WHERE unit_type_id = $1 AND date = $2", ut, TODAY
    )
    return row["hold"], row["sold"]


@pytest.mark.asyncio
async def test_a_live_hold_still_replays(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "key-live@example.com", published=True)
    body = _body(ut, _guest())

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=KEY)
        again = client.post("/v1/bookings/hold", json=body, headers=KEY)

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json()["id"] == first.json()["id"]
    assert await _inventory(committed_conn, ut) == (1, 0)


@pytest.mark.asyncio
async def test_a_cancelled_hold_does_not_block_the_same_stay(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "key-cancel@example.com", published=True)
    body = _body(ut, _guest())

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=KEY).json()
        cancelled = client.post(f"/v1/bookings/{first['id']}/cancel", headers=_token(first))
        second = client.post("/v1/bookings/hold", json=body, headers=KEY)

    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]
    assert second.json()["status"] == "hold"
    assert await _inventory(committed_conn, ut) == (1, 0)


@pytest.mark.asyncio
async def test_an_expired_hold_does_not_block_the_same_stay_before_the_reaper_runs(
    committed_conn,
) -> None:
    ut = await _seed_unit(committed_conn, "key-expired@example.com", published=True)
    body = _body(ut, _guest())

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=KEY).json()
        await committed_conn.execute(
            "UPDATE booking SET hold_expires_at = now() - interval '1 minute' WHERE id = $1",
            first["id"],
        )
        second = client.post("/v1/bookings/hold", json=body, headers=KEY)

    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]
    assert second.json()["status"] == "hold"
    old = await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", first["id"])
    assert old == "cancelled"
    # One live hold, not two: the expired one gave its night back.
    assert await _inventory(committed_conn, ut) == (1, 0)


@pytest.mark.asyncio
async def test_a_refunded_booking_does_not_block_the_same_stay(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "key-refund@example.com", published=True)
    body = _body(ut, _guest())

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=KEY).json()
        assert (
            client.post(f"/v1/bookings/{first['id']}/pay", headers=_token(first)).status_code == 200
        )
        refunded = client.post(f"/v1/bookings/{first['id']}/refund", headers=_token(first))
        second = client.post("/v1/bookings/hold", json=body, headers=KEY)

    assert refunded.status_code == 200 and refunded.json()["status"] == "refunded"
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]
    assert second.json()["status"] == "hold"
    assert await _inventory(committed_conn, ut) == (1, 0)


@pytest.mark.asyncio
async def test_a_confirmed_booking_still_replays(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "key-confirmed@example.com", published=True)
    body = _body(ut, _guest())

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=KEY).json()
        assert (
            client.post(f"/v1/bookings/{first['id']}/pay", headers=_token(first)).status_code == 200
        )
        again = client.post("/v1/bookings/hold", json=body, headers=KEY)

    assert again.status_code == 201
    assert again.json()["id"] == first["id"]
    assert again.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_a_finished_key_reused_for_another_guest_is_not_a_conflict(committed_conn) -> None:
    """While the booking lives the key is bound to its payload; once it is over it is free."""
    ut = await _seed_unit(committed_conn, "key-other@example.com", published=True)
    first_guest, other_guest = _guest(), _guest()

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=_body(ut, first_guest), headers=KEY).json()
        busy = client.post("/v1/bookings/hold", json=_body(ut, other_guest), headers=KEY)
        client.post(f"/v1/bookings/{first['id']}/cancel", headers=_token(first))
        free = client.post("/v1/bookings/hold", json=_body(ut, other_guest), headers=KEY)

    assert busy.status_code == 409
    assert free.status_code == 201, free.text
    assert free.json()["id"] != first["id"]


@pytest.mark.asyncio
async def test_the_retired_key_keeps_the_old_booking_findable(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "key-trace@example.com", published=True)
    body = _body(ut, _guest())

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=KEY).json()
        client.post(f"/v1/bookings/{first['id']}/cancel", headers=_token(first))
        second = client.post("/v1/bookings/hold", json=body, headers=KEY).json()

    keys = {
        r["id"]: r["idempotency_key"]
        for r in await committed_conn.fetch(
            "SELECT id::text, idempotency_key FROM booking WHERE id = ANY($1::uuid[])",
            [first["id"], second["id"]],
        )
    }
    assert keys[second["id"]] == "hold-same-stay"
    assert keys[first["id"]].startswith("hold-same-stay#retired:")


@pytest.mark.asyncio
async def test_a_channel_key_stays_with_its_cancelled_booking(committed_conn) -> None:
    """A channel's key is the OTA's own booking reference: a late retry must not re-create it."""
    ut = await _seed_unit(committed_conn, "key-channel@example.com")
    args = {"unit_type_id": ut, "checkin": TODAY, "checkout": TODAY + dt.timedelta(days=2)}

    first = await booking_service.create_hold(
        committed_conn, idempotency_key="ota-ref-1", origin="channel", **args, **_guest()
    )
    await booking_service.release_hold(committed_conn, first["id"])
    guest = await committed_conn.fetchrow(
        "SELECT guest_name, guest_email, guest_phone FROM booking WHERE id = $1", first["id"]
    )
    again = await booking_service.create_hold(
        committed_conn,
        idempotency_key="ota-ref-1",
        origin="channel",
        guest_name=guest["guest_name"],
        guest_email=guest["guest_email"],
        guest_phone=guest["guest_phone"],
        **args,
    )

    assert again["id"] == first["id"]
    assert again["status"] == "cancelled"
    assert await _inventory(committed_conn, ut) == (0, 0)
