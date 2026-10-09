"""Transaction helpers for the booking core.

Inventory changes run SERIALIZABLE (see app/db/pool.py). When two transactions
conflict, Postgres aborts one of them with a serialization failure. That is an
expected outcome, not a fault: the loser runs again on a fresh snapshot, where
it sees the winner's rows and either succeeds or fails with its own business
error (no free inventory -> 409).

Money operations add a second rule: a booking is charged or refunded by one
request at a time, so a double click or a client retry never reaches the
payment provider twice.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

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


def install_conflict_handler(app: FastAPI) -> None:
    """Answer 503 + Retry-After when a conflict outlasts the retries, not a 500."""

    async def _on_conflict(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "conflicting update, please retry"},
            headers={"Retry-After": "1"},
        )

    for exc_type in _CONFLICTS:
        app.add_exception_handler(exc_type, _on_conflict)
