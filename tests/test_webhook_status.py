"""A subscription's own status follows every delivery attempt, a retry included.

The partner's cabinet reads webhook_subscription.last_status and last_error. A receiver that
answered 500 once and 204 on the retry used to keep the red "error: HTTP 500" until some later
event happened to succeed on its first attempt: only the first attempt per event updated it.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.outbox import service as outbox_service
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from tests._subs import subscribe


async def _event_and_subscription(conn: asyncpg.Connection, email: str) -> tuple[str, str]:
    partner_id = str(
        await auth_service.register_partner(
            conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
        )
    )
    prop = await property_service.create_property(
        conn, partner_id, PropertyCreate(name="Test", property_type="apartment", city="Koktebel")
    )
    await outbox_service.emit(
        conn,
        aggregate="property",
        aggregate_id=str(prop.id),
        event_type=outbox_service.PROPERTY_STATUS_CHANGED,
        payload={},
        property_id=str(prop.id),
    )
    event_id = await conn.fetchval(
        "SELECT id::text FROM outbox_event WHERE property_id = $1", str(prop.id)
    )
    sub_id = await subscribe(conn, partner_id, "http://localhost:9/hooks", "status-secret-1")
    return event_id, sub_id


async def _status(conn: asyncpg.Connection, sub_id: str) -> tuple[str | None, str | None]:
    row = await conn.fetchrow(
        "SELECT last_status, last_error FROM webhook_subscription WHERE id = $1", sub_id
    )
    assert row is not None
    return row["last_status"], row["last_error"]


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_clears_the_failed_status(committed_conn) -> None:
    event_id, sub_id = await _event_and_subscription(committed_conn, "ws1@example.com")

    await outbox_service.record_delivery(committed_conn, event_id, sub_id, False, 500, "HTTP 500")
    assert await _status(committed_conn, sub_id) == ("failed", "HTTP 500")
    first_at = await committed_conn.fetchval(
        "SELECT last_delivery_at FROM webhook_subscription WHERE id = $1", sub_id
    )

    await outbox_service.record_delivery(committed_conn, event_id, sub_id, True, 204, None)

    assert await _status(committed_conn, sub_id) == ("success", None)
    second_at = await committed_conn.fetchval(
        "SELECT last_delivery_at FROM webhook_subscription WHERE id = $1", sub_id
    )
    assert second_at > first_at
    # The delivery log still holds one row for the pair, now the successful one.
    rows = await committed_conn.fetch(
        "SELECT status, status_code FROM webhook_delivery WHERE event_id = $1 AND subscription_id = $2",
        event_id,
        sub_id,
    )
    assert [(r["status"], r["status_code"]) for r in rows] == [("success", 204)]


@pytest.mark.asyncio
async def test_a_retry_that_fails_again_shows_the_newest_error(committed_conn) -> None:
    event_id, sub_id = await _event_and_subscription(committed_conn, "ws2@example.com")

    await outbox_service.record_delivery(committed_conn, event_id, sub_id, False, 500, "HTTP 500")
    await outbox_service.record_delivery(committed_conn, event_id, sub_id, False, 502, "HTTP 502")

    assert await _status(committed_conn, sub_id) == ("failed", "HTTP 502")


@pytest.mark.asyncio
async def test_a_first_attempt_still_sets_the_status(committed_conn) -> None:
    event_id, sub_id = await _event_and_subscription(committed_conn, "ws3@example.com")
    assert await _status(committed_conn, sub_id) == (None, None)

    await outbox_service.record_delivery(committed_conn, event_id, sub_id, True, 200, None)

    assert await _status(committed_conn, sub_id) == ("success", None)
