"""release_hold when another transaction reached the booking first.

The reaper runs READ COMMITTED, and nothing locked the nights before release_hold read the booking's status. A
release that overlapped a confirmation, or another release of the same booking, acted on a status that was no longer
true: it took a second hold off the nights, which belonged to other guests' holds, and set a booking that had just been
paid to `cancelled` with its charge unreturned. The seeded chaos round failed once in about seventy runs under load
(`inventory (a)`, `money (c)`, "hold vanished"); these tests make the same interleaving happen on purpose: the release
reads the booking, the other transaction finishes and commits, and only then does the release go on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import asyncpg
import pytest

from app.config.settings import settings
from app.modules.booking import service as booking_service
from app.modules.outbox import service as outbox_service
from tests.test_booking import TODAY, _guest, _seed_unit

CHECKIN = TODAY + dt.timedelta(days=3)
CHECKOUT = TODAY + dt.timedelta(days=5)


class _PauseAfterFirstRead:
    """A connection that stops once, right after its first fetchrow, until `go` is set."""

    def __init__(self, conn: asyncpg.Connection, reached: asyncio.Event, go: asyncio.Event):
        self._conn = conn
        self._reached = reached
        self._go = go
        self._paused = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchrow(self, query: str, *args):
        row = await self._conn.fetchrow(query, *args)
        if not self._paused:
            self._paused = True
            self._reached.set()
            await self._go.wait()
        return row


async def _two_holds(conn: asyncpg.Connection, email: str) -> tuple[str, str, str]:
    """A room type with two units and two holds on the same two nights."""
    unit_type_id = await _seed_unit(conn, email, total_units=2)
    holds = []
    for _ in range(2):
        booking = await booking_service.create_hold(
            conn,
            unit_type_id=unit_type_id,
            checkin=CHECKIN,
            checkout=CHECKOUT,
            idempotency_key=f"k-{uuid.uuid4()}",
            **_guest(),
        )
        holds.append(booking["id"])
    return unit_type_id, holds[0], holds[1]


async def _release_while_another_transaction_finishes(
    first: str, other_transaction, release_after: bool = True
) -> None:
    """Start releasing `first`, let `other_transaction(conn)` run and commit, then let the release go on."""
    reached, go = asyncio.Event(), asyncio.Event()
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=2, max_size=2)
    try:
        async with pool.acquire() as releasing, pool.acquire() as other:

            async def release() -> None:
                async with releasing.transaction():
                    await booking_service.release_hold(
                        _PauseAfterFirstRead(releasing, reached, go), first
                    )

            task = asyncio.create_task(release())
            await asyncio.wait_for(reached.wait(), 10)
            async with other.transaction():
                await other_transaction(other)
            go.set()
            await asyncio.wait_for(task, 10)
    finally:
        go.set()
        await pool.close()


async def _holds_on_the_nights(
    conn: asyncpg.Connection, unit_type_id: str
) -> list[tuple[int, int]]:
    rows = await conn.fetch(
        "SELECT hold, sold FROM inventory_day WHERE unit_type_id = $1 AND date >= $2 AND date < $3"
        " ORDER BY date",
        unit_type_id,
        CHECKIN,
        CHECKOUT,
    )
    return [(row["hold"], row["sold"]) for row in rows]


async def _cancelled_events(conn: asyncpg.Connection, booking_id: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM outbox_event WHERE aggregate_id = $1 AND event_type = $2",
        booking_id,
        outbox_service.BOOKING_CANCELLED,
    )


@pytest.mark.asyncio
async def test_a_second_release_of_the_same_hold_takes_nothing_more(committed_conn) -> None:
    unit_type_id, first, second = await _two_holds(committed_conn, "release-twice@example.com")

    await _release_while_another_transaction_finishes(
        first, lambda conn: booking_service.release_hold(conn, first)
    )

    assert await _holds_on_the_nights(committed_conn, unit_type_id) == [(1, 0), (1, 0)], (
        "the second release took the other guest's hold off the nights"
    )
    statuses = {
        row["id"]: row["status"]
        for row in await committed_conn.fetch(
            "SELECT id::text, status FROM booking WHERE id = ANY($1::uuid[])", [first, second]
        )
    }
    assert statuses == {first: "cancelled", second: "hold"}
    assert await _cancelled_events(committed_conn, first) == 1


@pytest.mark.asyncio
async def test_a_release_does_not_cancel_a_hold_that_was_confirmed_meanwhile(
    committed_conn,
) -> None:
    unit_type_id, first, _ = await _two_holds(committed_conn, "release-confirmed@example.com")

    async def confirm(conn: asyncpg.Connection) -> None:
        # What _confirm in app/modules/payment/service.py does to the nights and to the booking.
        await conn.execute(
            "UPDATE inventory_day SET hold = hold - 1, sold = sold + 1"
            " WHERE unit_type_id = $1 AND date >= $2 AND date < $3",
            unit_type_id,
            CHECKIN,
            CHECKOUT,
        )
        await conn.execute(
            "UPDATE booking SET status = 'confirmed', paid_at = now(), hold_expires_at = NULL"
            " WHERE id = $1 AND status = 'hold'",
            first,
        )

    await _release_while_another_transaction_finishes(first, confirm)

    assert (
        await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", first)
        == "confirmed"
    ), "a paid booking was turned into a cancelled one"
    assert await _holds_on_the_nights(committed_conn, unit_type_id) == [(1, 1), (1, 1)]
    assert await _cancelled_events(committed_conn, first) == 0


@pytest.mark.asyncio
async def test_a_release_queues_behind_a_confirmation_instead_of_deadlocking_with_it(
    committed_conn,
) -> None:
    """The confirmation locks the nights and then updates the booking. A release that took the booking first
    and the nights second would wait for the confirmation while the confirmation waits for it: Postgres ends
    that after deadlock_timeout by failing one of the two."""
    unit_type_id, first, _ = await _two_holds(committed_conn, "release-queues@example.com")
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=2, max_size=2)
    try:
        async with pool.acquire() as confirming, pool.acquire() as releasing:
            confirmation = confirming.transaction()
            await confirmation.start()
            await confirming.execute(
                "UPDATE inventory_day SET hold = hold - 1, sold = sold + 1"
                " WHERE unit_type_id = $1 AND date >= $2 AND date < $3",
                unit_type_id,
                CHECKIN,
                CHECKOUT,
            )

            async def release() -> None:
                async with releasing.transaction():
                    await booking_service.release_hold(releasing, first)

            task = asyncio.create_task(release())
            pid = releasing.get_server_pid()
            for _ in range(100):
                if await committed_conn.fetchval(
                    "SELECT coalesce(wait_event_type = 'Lock', false) FROM pg_stat_activity"
                    " WHERE pid = $1",
                    pid,
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("the release never waited for the confirmation")

            await confirming.execute(
                "UPDATE booking SET status = 'confirmed', paid_at = now(), hold_expires_at = NULL"
                " WHERE id = $1 AND status = 'hold'",
                first,
            )
            await confirmation.commit()
            await asyncio.wait_for(task, 10)
    finally:
        await pool.close()

    assert (
        await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", first)
        == "confirmed"
    )
    assert await _holds_on_the_nights(committed_conn, unit_type_id) == [(1, 1), (1, 1)]


@pytest.mark.asyncio
async def test_the_cancelled_event_says_whether_the_hold_expired(committed_conn) -> None:
    unit_type_id, held, failed = await _two_holds(committed_conn, "release-flag@example.com")
    await committed_conn.execute("UPDATE booking SET status = 'failed' WHERE id = $1", failed)

    await booking_service.release_hold(committed_conn, held)
    await booking_service.release_hold(committed_conn, failed)

    async def expired(booking_id: str) -> str:
        return await committed_conn.fetchval(
            "SELECT payload->>'expired' FROM outbox_event WHERE aggregate_id = $1 AND event_type = $2",
            booking_id,
            outbox_service.BOOKING_CANCELLED,
        )

    assert await expired(held) == "true"
    assert await expired(failed) == "false"
    assert await _holds_on_the_nights(committed_conn, unit_type_id) == [(0, 0), (0, 0)]
