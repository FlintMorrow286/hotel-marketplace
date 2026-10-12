"""A failed outbox write loses the event, never the caller's transaction."""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from app.db.tx import serializable
from app.modules.booking import service as booking_service
from app.modules.outbox import backlog, service
from tests.test_booking import TODAY, _guest, _seed_unit

MISSING_PROPERTY = str(uuid.uuid4())  # the foreign key refuses it: a failure the SERVER reports


async def _outbox_rows(conn, aggregate_id: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM outbox_event WHERE aggregate_id = $1", aggregate_id
    )


@pytest.mark.asyncio
async def test_a_failed_event_leaves_the_callers_transaction_usable(committed_conn) -> None:
    """Before: the swallowed error aborted the transaction and the next statement raised
    InFailedSQLTransactionError (or the COMMIT quietly became a ROLLBACK)."""
    ut = await _seed_unit(committed_conn, "sp-1@example.com")
    marker = f"sp-{uuid.uuid4().hex[:8]}"

    async with committed_conn.transaction():
        await committed_conn.execute("UPDATE unit_type SET name = $2 WHERE id = $1", ut, marker)
        await service.emit(
            committed_conn,
            aggregate="booking",
            aggregate_id=marker,
            event_type=service.BOOKING_CONFIRMED,
            payload={},
            property_id=MISSING_PROPERTY,
        )
        await committed_conn.execute("UPDATE unit_type SET capacity = 7 WHERE id = $1", ut)

    row = await committed_conn.fetchrow("SELECT name, capacity FROM unit_type WHERE id = $1", ut)
    assert (row["name"], row["capacity"]) == (marker, 7)
    assert await _outbox_rows(committed_conn, marker) == 0


@pytest.mark.asyncio
async def test_a_good_event_is_still_written_with_the_transaction(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "sp-2@example.com")
    property_id = await committed_conn.fetchval(
        "SELECT property_id::text FROM unit_type WHERE id = $1", ut
    )
    marker = f"sp-{uuid.uuid4().hex[:8]}"

    async with committed_conn.transaction():
        await service.emit(
            committed_conn,
            aggregate="booking",
            aggregate_id=marker,
            event_type=service.BOOKING_CONFIRMED,
            payload={"a": 1},
            property_id=property_id,
        )

    assert await _outbox_rows(committed_conn, marker) == 1


@pytest.mark.asyncio
async def test_a_lost_booking_event_is_logged_as_an_error(committed_conn, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(service.log, "error", lambda event, **kw: seen.append(("error", event)))
    monkeypatch.setattr(service.log, "warning", lambda event, **kw: seen.append(("warning", event)))

    for aggregate in ("booking", "rate_plan"):
        await service.emit(
            committed_conn,
            aggregate=aggregate,
            aggregate_id="x",
            event_type=service.BOOKING_CONFIRMED,
            payload={},
            property_id=MISSING_PROPERTY,
        )

    assert seen == [("error", "outbox-emit-failed"), ("warning", "outbox-emit-failed")]


@pytest.mark.asyncio
async def test_a_serialization_conflict_reaches_the_retry_loop(committed_conn, monkeypatch) -> None:
    """The conflict is the caller's, not the outbox's: swallowing it hid the signal that makes
    serializable() run the body again."""
    ut = await _seed_unit(committed_conn, "sp-3@example.com")
    property_id = await committed_conn.fetchval(
        "SELECT property_id::text FROM unit_type WHERE id = $1", ut
    )
    marker = f"sp-{uuid.uuid4().hex[:8]}"
    real_admit = backlog.admit
    attempts = {"n": 0}

    async def conflict_once(conn, *, priority, limit):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncpg.SerializationError("could not serialize access")
        return await real_admit(conn, priority=priority, limit=limit)

    monkeypatch.setattr(backlog, "admit", conflict_once)

    async def body() -> None:
        await service.emit(
            committed_conn,
            aggregate="property",
            aggregate_id=marker,
            event_type=service.PROPERTY_STATUS_CHANGED,
            payload={},
            property_id=property_id,
        )

    await serializable(committed_conn, body)

    assert attempts["n"] == 2, "the body must have run again"
    assert await _outbox_rows(committed_conn, marker) == 1


@pytest.mark.asyncio
async def test_a_conflict_outside_a_transaction_is_only_the_events_own(
    committed_conn, monkeypatch
) -> None:
    async def conflict(conn, *, priority, limit):
        raise asyncpg.SerializationError("could not serialize access")

    monkeypatch.setattr(backlog, "admit", conflict)

    await service.emit(  # must not raise: nothing of the caller's is at stake
        committed_conn,
        aggregate="property",
        aggregate_id="x",
        event_type=service.PROPERTY_STATUS_CHANGED,
        payload={},
        property_id=None,
    )


@pytest.mark.asyncio
async def test_cancelling_a_hold_survives_a_failed_event(committed_conn, monkeypatch) -> None:
    """The cancel route's own path: release_hold, then a read in the same transaction."""
    ut = await _seed_unit(committed_conn, "sp-4@example.com")
    hold = await booking_service.create_hold(
        committed_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        **_guest(),
    )
    real_emit = service.emit

    async def emit_with_a_dead_property(conn, **kwargs):
        await real_emit(conn, **{**kwargs, "property_id": MISSING_PROPERTY})

    import app.modules.outbox.service as outbox_module

    monkeypatch.setattr(outbox_module, "emit", emit_with_a_dead_property)

    async def cancel() -> str:
        await booking_service.release_hold(committed_conn, hold["id"])
        return await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", hold["id"])

    assert await serializable(committed_conn, cancel) == "cancelled"
    held = await committed_conn.fetchval(
        "SELECT hold FROM inventory_day WHERE unit_type_id = $1 AND date = $2", ut, TODAY
    )
    assert held == 0
