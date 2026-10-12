"""Transaction helpers for the booking core.

Inventory changes run SERIALIZABLE (see app/db/pool.py). When two transactions
conflict, Postgres aborts one of them with a serialization failure. That is an
expected outcome, not a fault: the loser runs again on a fresh snapshot, where
it sees the winner's rows and either succeeds or fails with its own business
error (no free inventory -> 409).

Money operations add a second rule: a booking is charged or refunded by one
request at a time, so a double click or a client retry never reaches the
payment provider twice.

Holds add a third: they queue per unit type (`unit_queue`, `lock_unit`). Holds
on one unit type fight over the same inventory rows, and under SERIALIZABLE the
losers fail and run again; a storm of 100 guests on one room outlasted the
retries for about one in six. A queue costs a few milliseconds per hold and
takes the fight away.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.utils.logger import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 5
_CONFLICTS = (asyncpg.SerializationError, asyncpg.DeadlockDetectedError)


async def serializable[T](
    conn: asyncpg.Connection,
    body: Callable[[], Awaitable[T]],
    *,
    attempts: int = MAX_ATTEMPTS,
) -> T:
    """Run `body` in a SERIALIZABLE transaction, running it again on a conflict.

    `body` may be executed several times, so it must do database work only:
    no provider calls, no notifications.
    """
    for attempt in range(1, attempts + 1):
        try:
            async with conn.transaction(isolation="serializable"):
                return await body()
        except _CONFLICTS:
            if attempt == attempts:
                raise
            log.info("serializable-retry", attempt=attempt)
            await asyncio.sleep(random.uniform(0, 0.05) * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


async def try_lock(conn: asyncpg.Connection, scope: str, booking_id: str) -> bool:
    """Take the advisory lock for `scope` on a booking; False if another request holds it.

    Session-level, so it spans the provider call that sits between the
    transactions. Release it with `unlock` (the pool also drops it on release).
    """
    return bool(
        await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", f"{scope}:{booking_id}"
        )
    )


async def unlock(conn: asyncpg.Connection, scope: str, booking_id: str) -> None:
    try:
        await conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))", f"{scope}:{booking_id}"
        )
    except Exception as exc:  # the lock dies with the session anyway
        log.warning("booking-unlock-failed", scope=scope, booking_id=booking_id, error=str(exc))


QUEUE_WAIT_SEC = 10.0
_QUEUE_POLL_SEC = 0.005


class QueueTimeout(Exception):
    """A turn on a unit type did not come up in time."""


class _Gate:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_gates: dict[str, _Gate] = {}


def _canonical_unit_key(unit_type_id: str) -> str:
    try:
        return str(uuid.UUID(unit_type_id))
    except ValueError:
        return unit_type_id.strip().casefold()


@asynccontextmanager
async def _local_turn(key: str, wait_sec: float) -> AsyncIterator[None]:
    """One request at a time per key inside this process, without holding a connection."""
    key = _canonical_unit_key(key)
    gate = _gates.setdefault(key, _Gate())
    gate.users += 1
    try:
        try:
            async with asyncio.timeout(wait_sec):
                await gate.lock.acquire()
        except TimeoutError as exc:
            raise QueueTimeout(key) from exc
        try:
            yield
        finally:
            gate.lock.release()
    finally:
        gate.users -= 1
        if gate.users == 0 and _gates.get(key) is gate:
            del _gates[key]


@asynccontextmanager
async def lock_unit(
    conn: asyncpg.Connection, unit_type_id: str, *, wait_sec: float = QUEUE_WAIT_SEC
) -> AsyncIterator[None]:
    """Take the turn for a unit type across every worker, on this connection.

    A session-level advisory lock taken before the transaction starts, on purpose:
    a SERIALIZABLE transaction fixes its snapshot at its first statement, so a lock
    waited for inside it would be won on a stale snapshot and the winner's rows would
    still conflict. Polled rather than blocked on, so the wait has a bound.
    """
    key = _canonical_unit_key(unit_type_id)
    deadline = time.monotonic() + wait_sec
    while not await try_lock(conn, "hold", key):
        if time.monotonic() >= deadline:
            raise QueueTimeout(key)
        await asyncio.sleep(_QUEUE_POLL_SEC)
    try:
        yield
    finally:
        await unlock(conn, "hold", key)


@asynccontextmanager
async def unit_queue(
    pool: asyncpg.Pool, unit_type_id: str, *, wait_sec: float = QUEUE_WAIT_SEC
) -> AsyncIterator[asyncpg.Connection]:
    """A connection for taking a hold on `unit_type_id`, one hold at a time per unit type.

    The turn inside this process is awaited before a connection is taken, so a
    queue of waiting guests never drains the pool; the advisory lock then orders the
    workers. Nothing slow belongs inside: no provider calls, no notifications.
    """
    key = _canonical_unit_key(unit_type_id)
    deadline = time.monotonic() + wait_sec
    async with _local_turn(key, wait_sec):
        try:
            conn = await pool.acquire(timeout=max(0.1, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise QueueTimeout(key) from exc
        try:
            remaining = max(0.0, deadline - time.monotonic())
            async with lock_unit(conn, key, wait_sec=remaining):
                yield conn
        finally:
            await pool.release(conn)


def install_conflict_handler(app: FastAPI) -> None:
    """Answer 503 + Retry-After when a conflict outlasts the retries, or a queue the wait, not a 500."""

    async def _on_conflict(request: Request, exc: Exception) -> JSONResponse:
        # The 500 this replaces logged a traceback; keep a trace of the 503. The route
        # template, not the path: a booking id in the path is a credential.
        log.warning(
            "conflict-answered-503",
            method=request.method,
            route=getattr(request.scope.get("route"), "path", None),
            error=str(exc),
        )
        detail = (
            "too many bookings for this room right now, please retry"
            if isinstance(exc, QueueTimeout)
            else "conflicting update, please retry"
        )
        return JSONResponse(
            status_code=503, content={"detail": detail}, headers={"Retry-After": "1"}
        )

    for exc_type in (*_CONFLICTS, QueueTimeout):
        app.add_exception_handler(exc_type, _on_conflict)
