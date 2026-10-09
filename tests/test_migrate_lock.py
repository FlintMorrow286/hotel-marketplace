"""Several instances start at once: each migration is still applied exactly once.

The app runs migrations in its lifespan, so a rolling deploy (or a scaled-out
restart) starts several runners against one database. Without a lock they race
on the tracking table and on the DDL itself. A runner that has to wait says so,
and `--check` neither waits nor writes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from app.config.settings import settings
from app.db import migrate
from app.db.migrate import _list_migrations, run_migrations
from tests.conftest import _admin_dsn, _parts

TAKE_LOCK = "SELECT pg_advisory_lock(hashtextextended('app.db.migrate', 0))"


@pytest_asyncio.fixture
async def scratch_dsn(_test_db) -> AsyncIterator[str]:
    """A fresh, never-migrated database of its own (advisory locks are per database)."""
    split, _ = _parts(settings.test_dsn)
    name = f"hotel_mp_lock_{uuid.uuid4().hex[:8]}"
    dsn = urlunsplit(split._replace(path=f"/{name}"))

    admin = await asyncpg.connect(_admin_dsn(settings.test_dsn))
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
            await conn.execute("CREATE EXTENSION IF NOT EXISTS citext")
        finally:
            await conn.close()
        yield dsn
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


class _Log:
    """Records the runner's log events; `seen(name)` is an Event set once `name` is logged."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._seen: dict[str, asyncio.Event] = {}

    def seen(self, name: str) -> asyncio.Event:
        return self._seen.setdefault(name, asyncio.Event())

    def info(self, event: str, **_: object) -> None:
        self.events.append(event)
        self.seen(event).set()

    warning = info


@pytest.fixture
def runner_log(monkeypatch) -> _Log:
    """The app logger caches itself on first use, so a capture hook installed now would
    miss it; swap the module's logger instead."""
    recorder = _Log()
    monkeypatch.setattr(migrate, "log", recorder)
    return recorder


async def _tracking_table_exists(dsn: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        return bool(await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL"))
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_runs_apply_every_migration_once(scratch_dsn: str) -> None:
    dsn = scratch_dsn
    results = await asyncio.gather(*(run_migrations(dsn=dsn) for _ in range(4)))

    expected = [n for n, _ in await _list_migrations()]
    # Across the four runners every version was applied once and only once.
    assert sorted(v for applied in results for v in applied) == expected

    check = await asyncpg.connect(dsn)
    try:
        recorded = await check.fetch("SELECT version FROM schema_migrations ORDER BY version")
    finally:
        await check.close()
    assert [r["version"] for r in recorded] == expected
    assert await run_migrations(check_only=True, dsn=dsn) == []


@pytest.mark.asyncio
async def test_a_runner_waits_for_the_lock_and_says_so(scratch_dsn: str, runner_log: _Log) -> None:
    dsn = scratch_dsn
    expected = [n for n, _ in await _list_migrations()]

    holder = await asyncpg.connect(dsn)  # another instance, mid-migration
    try:
        await holder.execute(TAKE_LOCK)
        runner = asyncio.create_task(run_migrations(dsn=dsn))

        async with asyncio.timeout(10):
            await runner_log.seen("migration-lock-waiting").wait()
        # It is waiting, not failing and not applying anything behind the holder's back.
        assert not runner.done()
        assert "apply-migration" not in runner_log.events
    finally:
        await holder.close()  # the lock goes with the session

    assert await asyncio.wait_for(runner, 30) == expected
    events = runner_log.events
    assert events.index("migration-lock-acquired") > events.index("migration-lock-waiting")

    # Nobody holds it now: the next run neither waits nor says it did.
    events.clear()
    assert await run_migrations(dsn=dsn) == []
    assert "migration-lock-waiting" not in events


@pytest.mark.asyncio
async def test_check_neither_waits_nor_writes(scratch_dsn: str) -> None:
    dsn = scratch_dsn
    expected = [n for n, _ in await _list_migrations()]

    holder = await asyncpg.connect(dsn)
    try:
        await holder.execute(TAKE_LOCK)
        # A check does not queue behind a migration in progress ...
        pending = await asyncio.wait_for(run_migrations(check_only=True, dsn=dsn), 10)
    finally:
        await holder.close()

    assert pending == expected
    # ... and on a database that was never migrated it does not create the tracking
    # table, which is what raced with the runner holding the lock.
    assert not await _tracking_table_exists(dsn)
