"""A web guest accepts the offer and the personal data consent before a hold is made.

docs/LEGAL.md §3. The hold request carries two ticked boxes and the version of the documents the
guest was shown. A request without them, or with a version that is no longer current, is refused
before it touches inventory; an accepted hold stores the version and the moment on the booking.
"""

from __future__ import annotations

import datetime as dt

import asyncpg
import pytest
from starlette.testclient import TestClient

from app.config import legal
from app.main import create_app
from app.modules.booking import service as booking_service
from tests._consent import consent
from tests.test_booking import TODAY, _guest, _seed_unit

STALE = {**consent(), "docs_version": "1999.01-old"}


def _body(ut: str, **overrides) -> dict:
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
        "consent": consent(),
    }
    body.update(overrides)
    return body


async def _nothing_was_taken(conn: asyncpg.Connection, ut: str) -> None:
    """No booking exists and no night is held: the refusal came before inventory."""
    assert await conn.fetchval("SELECT count(*) FROM booking") == 0
    assert (
        await conn.fetchval("SELECT sum(hold) FROM inventory_day WHERE unit_type_id = $1", ut) == 0
    )


@pytest.mark.asyncio
async def test_legal_endpoint_reports_the_current_version(committed_conn) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/v1/legal")

    assert response.status_code == 200
    assert response.json() == {"docs_version": legal.LEGAL_DOCS_VERSION}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ticked",
    [
        pytest.param({**consent(), "offer": False}, id="offer-unticked"),
        pytest.param({**consent(), "personal_data": False}, id="personal-data-unticked"),
        pytest.param({**consent(), "offer": False, "personal_data": False}, id="both-unticked"),
        pytest.param({**consent(), "docs_version": ""}, id="empty-version"),
        pytest.param({"offer": True, "personal_data": True}, id="no-version"),
    ],
)
async def test_hold_without_both_consents_is_422(committed_conn, ticked: dict) -> None:
    ut = await _seed_unit(committed_conn, "consent-422@example.com", published=True)

    with TestClient(create_app()) as client:
        response = client.post("/v1/bookings/hold", json=_body(ut, consent=ticked))

    assert response.status_code == 422, response.text
    await _nothing_was_taken(committed_conn, ut)


@pytest.mark.asyncio
async def test_hold_without_the_consent_block_is_422(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "consent-missing@example.com", published=True)
    body = _body(ut)
    del body["consent"]

    with TestClient(create_app()) as client:
        response = client.post("/v1/bookings/hold", json=body)

    assert response.status_code == 422, response.text
    await _nothing_was_taken(committed_conn, ut)


@pytest.mark.asyncio
async def test_a_version_that_is_not_current_is_409(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "consent-stale@example.com", published=True)

    with TestClient(create_app()) as client:
        response = client.post("/v1/bookings/hold", json=_body(ut, consent=STALE))

    assert response.status_code == 409, response.text
    assert "legal documents changed" in response.json()["detail"]
    await _nothing_was_taken(committed_conn, ut)


@pytest.mark.asyncio
async def test_the_version_is_compared_with_the_constant_at_request_time(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "consent-bump@example.com", 2, published=True)
    previous = {**consent()}
    monkeypatch.setattr(legal, "LEGAL_DOCS_VERSION", "2099.01")

    with TestClient(create_app()) as client:
        old = client.post("/v1/bookings/hold", json=_body(ut, consent=previous))
        new = client.post("/v1/bookings/hold", json=_body(ut))
        info = client.get("/v1/legal")

    assert (old.status_code, new.status_code) == (409, 201), (old.text, new.text)
    assert info.json() == {"docs_version": "2099.01"}


@pytest.mark.asyncio
async def test_an_accepted_hold_stores_the_version_and_the_moment(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "consent-ok@example.com", published=True)
    before = dt.datetime.now(dt.UTC)

    with TestClient(create_app()) as client:
        response = client.post("/v1/bookings/hold", json=_body(ut))
    after = dt.datetime.now(dt.UTC)

    assert response.status_code == 201, response.text
    row = await committed_conn.fetchrow(
        "SELECT docs_version, docs_accepted_at FROM booking WHERE id = $1",
        response.json()["id"],
    )
    assert row["docs_version"] == legal.LEGAL_DOCS_VERSION
    assert before <= row["docs_accepted_at"] <= after


@pytest.mark.asyncio
async def test_a_replayed_key_keeps_the_first_acceptance(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "consent-replay@example.com", published=True)
    body = _body(ut)
    key = {"Idempotency-Key": "consent-replay"}

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=body, headers=key)
        stamped = await committed_conn.fetchval("SELECT docs_accepted_at FROM booking")
        again = client.post("/v1/bookings/hold", json=body, headers=key)

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json()["id"] == first.json()["id"]
    rows = await committed_conn.fetch("SELECT docs_accepted_at FROM booking")
    assert [r["docs_accepted_at"] for r in rows] == [stamped]


@pytest.mark.asyncio
async def test_a_channel_booking_carries_no_consent(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "consent-channel@example.com")
    guest = _guest()

    async with committed_conn.transaction(isolation="serializable"):
        hold = await booking_service.create_hold(
            committed_conn,
            unit_type_id=ut,
            checkin=TODAY,
            checkout=TODAY + dt.timedelta(days=1),
            guest_name=guest["guest_name"],
            guest_email=guest["guest_email"],
            guest_phone=guest["guest_phone"],
            origin="channel",
        )

    row = await committed_conn.fetchrow(
        "SELECT docs_version, docs_accepted_at FROM booking WHERE id = $1", hold["id"]
    )
    assert (row["docs_version"], row["docs_accepted_at"]) == (None, None)


@pytest.mark.asyncio
async def test_the_version_and_the_moment_are_written_together(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "consent-pair@example.com")
    guest = _guest()
    async with committed_conn.transaction(isolation="serializable"):
        hold = await booking_service.create_hold(
            committed_conn,
            unit_type_id=ut,
            checkin=TODAY,
            checkout=TODAY + dt.timedelta(days=1),
            guest_name=guest["guest_name"],
            guest_email=guest["guest_email"],
            guest_phone=guest["guest_phone"],
        )

    with pytest.raises(asyncpg.CheckViolationError):
        await committed_conn.execute(
            "UPDATE booking SET docs_version = $2 WHERE id = $1",
            hold["id"],
            legal.LEGAL_DOCS_VERSION,
        )
