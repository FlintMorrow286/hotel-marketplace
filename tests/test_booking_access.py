"""The booking access token: the booking id alone cannot pay, cancel or refund."""

from __future__ import annotations

import datetime as dt

import pytest
from starlette.testclient import TestClient

from app.config.settings import settings
from app.main import create_app
from app.modules.booking import access
from tests.test_booking import TODAY, _guest, _seed_unit

ACTIONS = ("pay", "cancel", "refund")


def _hold(client: TestClient, ut: str, guest: dict) -> dict:
    response = client.post(
        "/v1/bookings/hold",
        json={
            "unit_type_id": ut,
            "checkin": TODAY.isoformat(),
            "checkout": (TODAY + dt.timedelta(days=2)).isoformat(),
            "guest": {
                "name": guest["guest_name"],
                "email": guest["guest_email"],
                "phone": guest["guest_phone"],
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_the_token_is_bound_to_the_booking_and_to_the_secret(monkeypatch) -> None:
    token = access.issue("booking-a")

    assert access.verify("booking-a", token)
    assert not access.verify("booking-b", token)
    assert not access.verify("booking-a", token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert not access.verify("booking-a", "")
    assert not access.verify("booking-a", "tökén")  # non-ASCII must not crash the comparison
    monkeypatch.setattr(settings, "jwt_secret", "another-secret-of-at-least-32-characters")
    assert not access.verify("booking-a", token)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ACTIONS)
async def test_an_action_without_the_token_is_refused(committed_conn, action) -> None:
    ut = await _seed_unit(committed_conn, f"acc-none-{action}@example.com")

    with TestClient(create_app()) as client:
        hold = _hold(client, ut, _guest())
        response = client.post(f"/v1/bookings/{hold['id']}/{action}")
        status_after = client.get(f"/v1/bookings/{hold['id']}").json()["status"]

    assert response.status_code == 422, response.text
    assert status_after == "hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ACTIONS)
async def test_the_token_of_another_booking_does_not_open_this_one(
    committed_conn, action
) -> None:
    ut = await _seed_unit(committed_conn, f"acc-other-{action}@example.com", total_units=2)

    with TestClient(create_app()) as client:
        mine = _hold(client, ut, _guest())
        theirs = _hold(client, ut, _guest())
        response = client.post(
            f"/v1/bookings/{mine['id']}/{action}", headers={access.HEADER: theirs["access_token"]}
        )
        status_after = client.get(f"/v1/bookings/{mine['id']}").json()["status"]

    assert response.status_code == 404, response.text
    assert status_after == "hold"


@pytest.mark.asyncio
async def test_an_unknown_id_and_a_wrong_token_look_the_same(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acc-same@example.com")

    with TestClient(create_app()) as client:
        hold = _hold(client, ut, _guest())
        wrong = client.post(f"/v1/bookings/{hold['id']}/pay", headers={access.HEADER: "nope"})
        unknown = client.post(
            "/v1/bookings/00000000-0000-0000-0000-000000000000/pay",
            headers={access.HEADER: hold["access_token"]},
        )
        malformed = client.post("/v1/bookings/not-a-uuid/pay", headers={access.HEADER: "nope"})

    assert (wrong.status_code, unknown.status_code, malformed.status_code) == (404, 404, 404)
    assert wrong.json() == unknown.json() == malformed.json()


@pytest.mark.asyncio
async def test_the_hold_token_pays_and_the_lookup_token_cancels(committed_conn) -> None:
    """A guest who closed the tab finds the booking by code and email and gets a token back."""
    ut = await _seed_unit(committed_conn, "acc-flow@example.com")
    guest = _guest()

    with TestClient(create_app()) as client:
        hold = _hold(client, ut, guest)
        paid = client.post(
            f"/v1/bookings/{hold['id']}/pay", headers={access.HEADER: hold["access_token"]}
        )
        found = client.post(
            "/v1/bookings/lookup", json={"code": hold["code"], "email": guest["guest_email"]}
        ).json()
        refunded = client.post(
            f"/v1/bookings/{found['id']}/refund", headers={access.HEADER: found["access_token"]}
        )

    assert paid.status_code == 200, paid.text
    assert found["access_token"] == hold["access_token"]
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["status"] == "refunded"


@pytest.mark.asyncio
async def test_a_replayed_hold_returns_the_same_token(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acc-replay@example.com")
    guest = _guest()
    body = {
        "unit_type_id": ut,
        "checkin": TODAY.isoformat(),
        "checkout": (TODAY + dt.timedelta(days=2)).isoformat(),
        "guest": {
            "name": guest["guest_name"],
            "email": guest["guest_email"],
            "phone": guest["guest_phone"],
        },
    }

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers={"Idempotency-Key": "acc-1"})
        again = client.post("/v1/bookings/hold", json=body, headers={"Idempotency-Key": "acc-1"})

    assert first.json()["access_token"] == again.json()["access_token"]
