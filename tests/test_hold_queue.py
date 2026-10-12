"""Holds on one unit type queue; holds on different unit types do not wait for each other."""

from __future__ import annotations

import asyncio
import time

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.db import tx
from app.db.tx import QueueTimeout, lock_unit, unit_queue
from app.main import create_app
from tests.test_concurrency import _hold_body, _race, _seed_unit


@pytest_asyncio.fixture
async def pool(_test_db: str):
    pool = await asyncpg.create_pool(dsn=_test_db, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


class _Probe:
    """Counts how many tasks are inside the critical section at once."""

    def __init__(self) -> None:
        self.inside = 0
        self.peak = 0

    async def visit(self, pool, unit_type_id: str, hold_sec: float = 0.05) -> None:
        async with unit_queue(pool, unit_type_id):
            self.inside += 1
            self.peak = max(self.peak, self.inside)
            await asyncio.sleep(hold_sec)
            self.inside -= 1


@pytest.mark.asyncio
async def test_holds_on_one_unit_type_take_turns(pool) -> None:
    probe = _Probe()

    await asyncio.gather(*(probe.visit(pool, "unit-a") for _ in range(6)))

    assert probe.peak == 1
    assert tx._gates == {}, "a finished queue leaves nothing behind"


@pytest.mark.asyncio
async def test_different_unit_types_do_not_wait_for_each_other(pool) -> None:
    probe = _Probe()
    started = time.perf_counter()

    await asyncio.gather(probe.visit(pool, "unit-a", 0.2), probe.visit(pool, "unit-b", 0.2))

    assert probe.peak == 2
    assert time.perf_counter() - started < 0.35


@pytest.mark.asyncio
async def test_waiting_guests_do_not_hold_connections(pool) -> None:
    """A queue of 6 on a pool of 4 must use one connection, not drain the pool."""
    in_use: list[int] = []

    async def visit() -> None:
        async with unit_queue(pool, "unit-a"):
            in_use.append(pool.get_size() - pool.get_idle_size())
            await asyncio.sleep(0.03)

    await asyncio.gather(*(visit() for _ in range(6)))

    assert max(in_use) == 1


@pytest.mark.asyncio
async def test_another_worker_holding_the_turn_makes_us_wait(pool) -> None:
    """The advisory lock orders workers: it is what a second process would hold."""
    other = await pool.acquire()
    try:
        assert await tx.try_lock(other, "hold", "unit-a")
        entered_at: list[float] = []

        async def visit() -> None:
            async with unit_queue(pool, "unit-a", wait_sec=3):
                entered_at.append(time.monotonic())

        task = asyncio.create_task(visit())
        await asyncio.sleep(0.15)
        assert not entered_at, "must wait while another worker holds the turn"
        released_at = time.monotonic()
        await tx.unlock(other, "hold", "unit-a")
        await task
    finally:
        await pool.release(other)

    assert entered_at[0] >= released_at


@pytest.mark.asyncio
async def test_the_wait_for_another_worker_is_bounded(pool) -> None:
    other = await pool.acquire()
    try:
        assert await tx.try_lock(other, "hold", "unit-a")
        started = time.monotonic()
        with pytest.raises(QueueTimeout):
            async with unit_queue(pool, "unit-a", wait_sec=0.2):
                pytest.fail("must not enter")
        assert time.monotonic() - started < 1
    finally:
        await tx.unlock(other, "hold", "unit-a")
        await pool.release(other)


@pytest.mark.asyncio
async def test_the_wait_for_a_turn_in_this_process_is_bounded(pool) -> None:
    holder_in = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with unit_queue(pool, "unit-a"):
            holder_in.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await holder_in.wait()
    with pytest.raises(QueueTimeout):
        async with unit_queue(pool, "unit-a", wait_sec=0.1):
            pytest.fail("must not enter")
    release.set()
    await task

    assert tx._gates == {}


@pytest.mark.asyncio
async def test_a_failed_hold_gives_the_turn_and_the_connection_back(pool) -> None:
    with pytest.raises(RuntimeError):
        async with unit_queue(pool, "unit-a"):
            raise RuntimeError("the hold blew up")

    async with unit_queue(pool, "unit-a", wait_sec=1) as conn:
        assert await conn.fetchval("SELECT 1") == 1
    assert pool.get_size() - pool.get_idle_size() == 0


@pytest.mark.asyncio
async def test_lock_unit_is_released_for_the_next_taker(pool) -> None:
    async with pool.acquire() as first, pool.acquire() as second:
        async with lock_unit(first, "unit-a"):
            assert not await tx.try_lock(second, "hold", "unit-a")
        assert await tx.try_lock(second, "hold", "unit-a")
        await tx.unlock(second, "hold", "unit-a")


def test_a_queue_timeout_answers_503_with_retry_after() -> None:
    app = FastAPI()
    tx.install_conflict_handler(app)

    @app.get("/busy")
    async def busy() -> None:
        raise QueueTimeout("unit-a")

    response = TestClient(app).get("/busy")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "too many bookings" in response.json()["detail"]


# ---------------------------------------------------------------- through the API


@pytest.mark.asyncio
async def test_a_storm_on_shared_rows_turns_nobody_away(committed_conn) -> None:
    """60 guests, 60 units, the same two nights: every one of them gets a hold."""
    n = 60
    ut = await _seed_unit(committed_conn, "queue-storm@example.com", total_units=n)

    with TestClient(create_app()) as client:
        responses = _race(n, lambda i: client.post("/v1/bookings/hold", json=_hold_body(ut, i)))

    assert sorted({r.status_code for r in responses}) == [201], [
        (r.status_code, r.text) for r in responses if r.status_code != 201
    ][:3]
    day = await committed_conn.fetchrow(
        "SELECT hold, sold, available FROM inventory_day WHERE unit_type_id = $1 "
        "ORDER BY date OFFSET 10 LIMIT 1",
        ut,
    )
    assert (day["hold"], day["sold"], day["available"]) == (n, 0, n)


@pytest.mark.asyncio
async def test_the_last_unit_goes_to_exactly_one_of_many(committed_conn) -> None:
    n = 40
    ut = await _seed_unit(committed_conn, "queue-last@example.com", total_units=1)

    with TestClient(create_app()) as client:
        responses = _race(n, lambda i: client.post("/v1/bookings/hold", json=_hold_body(ut, i)))

    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * (n - 1)
