"""Several instances start at once: each migration is still applied exactly once.

The app runs migrations in its lifespan, so a rolling deploy (or a scaled-out
restart) starts several runners against one database. Without a lock they race
on the tracking table and on the DDL itself.
"""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlunsplit

import asyncpg
import pytest

from app.config.settings import settings
from app.db.migrate import _list_migrations, run_migrations
from tests.conftest import _admin_dsn, _parts


@pytest.mark.asyncio
async def test_concurrent_runs_apply_every_migration_once(_test_db) -> None:
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
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
