"""Shared pytest fixtures.

The tests run against their own database, not the developer's.

The old suite cleaned the dev database after each test — a `committed_conn`
test wipes every row of every table in `hotel_mp`. That worked, but it made
`pytest` and `seed_demo.py` mutually exclusive: every test run destroyed the
demo content the frontend was developed against, so the DB had to be re-seeded
before the app was usable again.

The suite now creates and migrates `hotel_mp_test` once per session and drops
it again on the way out. The dev database is never touched, so `pytest` and
`seed_demo.py` no longer fight over the same rows.

Two DB fixtures:

- `db_conn`: connection inside a transaction that is always rolled back.
  Used by unit tests that must leave nothing behind.

- `committed_conn`: connection in autocommit mode with explicit cleanup.
  Used by tests whose code path opens its own transactions (payment service),
  which cannot nest inside an outer rollback.
"""

from __future__ import annotations

import os

# Signals "the suite is running" to the app itself. The app's connection pool
# follows `settings.pool_dsn`, which routes to the throwaway test database only
# when this is set — so a `TestClient` booted inside a test reads and writes
# the same rows the test's connection does. Conftest is imported before any
# test module touches `create_app`, so the flag is always in place first.
os.environ["PYTEST_RUNNING"] = "1"

from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from app.config.settings import settings

# The DSN the whole suite connects to. `settings.test_dsn` derives it from
# DATABASE_URL: the `_test` sibling unless PYTEST_DISABLE_TEST_DB=1 says the
# caller already pointed the suite at a throwaway database.
TEST_DSN = settings.test_dsn


@pytest.fixture(scope="session", autouse=True)
def _relaxed_public_rate_limits():
    """The suite logs in and looks bookings up from one client address.

    Only tests/test_public_ratelimit.py tightens these, with an address of its own.
    """
    names = ("login_limit_per_min", "register_limit_per_min", "lookup_limit_per_min")
    saved = {name: getattr(settings, name) for name in names}
    for name in names:
        setattr(settings, name, 100_000)
    yield
    for name, value in saved.items():
        setattr(settings, name, value)


_CLEANUP = (
    "DELETE FROM outbox_shed_counter; DELETE FROM webhook_delivery; "
    "DELETE FROM outbox_event; "
    "DELETE FROM webhook_subscription; "
    "DELETE FROM api_key; "
    "DELETE FROM ical_subscription; "
    "DELETE FROM booking_line; "
    "DELETE FROM payment; "
    "DELETE FROM booking; "
    "DELETE FROM inventory_day; "
    "DELETE FROM price_day; "
    "DELETE FROM rate_plan; "
    "DELETE FROM unit_type; "
    "DELETE FROM property; "
    "DELETE FROM partner; "
    "DELETE FROM admin;"
)


def _parts(dsn: str):
    """Split a DSN into server credentials and the database name."""
    split = urlsplit(dsn)
    return split, split.path.lstrip("/")


def _admin_dsn(dsn: str) -> str:
    """The same server and credentials, pointed at the maintenance database.

    A template database cannot be created while another connection is sitting
    on the target database, so the setup connects to `postgres` first. The test
    role needs CREATEDB (it owns both databases); it does not need SUPERUSER,
    and pgcrypto/citext are trusted extensions.
    """
    split, _ = _parts(dsn)
    return urlunsplit(split._replace(path="/postgres"))


async def _create_test_db(admin: asyncpg.Connection, dsn: str) -> None:
    """Create a fresh, fully migrated database owned by the test session."""
    from app.db.migrate import run_migrations

    _, name = _parts(dsn)

    exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
    if exists:
        # A previous run crashed before dropping it; start from scratch anyway,
        # so a half-seeded database can never make a run look green.
        await admin.execute(f'DROP DATABASE "{name}"')

    # template0 carries no objects from a prior run: the schema only, not the
    # data or the migrations table. Encoding/locale follow the server default,
    # as the dev database does.
    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')

    # pgcrypto and citext are trusted, so the test role may create them.
    app = await asyncpg.connect(dsn=dsn)
    try:
        await app.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await app.execute("CREATE EXTENSION IF NOT EXISTS citext")
    finally:
        await app.close()

    # Migrations run through the same runner the app uses, so the suite can
    # never diverge from the real schema.
    await run_migrations(dsn=dsn)


async def _drop_test_db(admin: asyncpg.Connection, dsn: str) -> None:
    _, name = _parts(dsn)
    exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
    if not exists:
        return
    # `db_conn` fixtures roll back and close their pools per test, so by
    # teardown nothing should be left connected. FORCE is not used: a lingering
    # connection means a fixture leaked, and the error says exactly that
    # instead of silently killing a session mid-query.
    await admin.execute(f'DROP DATABASE "{name}"')


@pytest_asyncio.fixture(scope="session")
async def _test_db() -> AsyncIterator[str]:
    """Create and migrate the test database once, drop it when the session ends.

    Yields the DSN the per-test fixtures connect to.
    """
    if TEST_DSN == settings.database_url:
        # PYTEST_DISABLE_TEST_DB=1: the caller pointed the suite at a database
        # they are happy to have wiped. Keep the old behaviour, but say so.
        import warnings

        warnings.warn(
            "PYTEST_DISABLE_TEST_DB is set — tests are writing to DATABASE_URL "
            f"({TEST_DSN}) and the cleanup DELETEs every row in it.",
            stacklevel=1,
        )
        yield TEST_DSN
        return

    admin_pool = await asyncpg.create_pool(dsn=_admin_dsn(TEST_DSN), min_size=1, max_size=2)
    try:
        async with admin_pool.acquire() as admin:
            await _create_test_db(admin, TEST_DSN)
        yield TEST_DSN
        async with admin_pool.acquire() as admin:
            await _drop_test_db(admin, TEST_DSN)
    finally:
        await admin_pool.close()


@pytest_asyncio.fixture
async def db_conn(_test_db: str) -> AsyncIterator[asyncpg.Connection]:
    """Per-test connection, always rolled back."""
    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=5)
    conn = await pool.acquire()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await pool.release(conn)
        await pool.close()


@pytest_asyncio.fixture
async def committed_conn(_test_db: str) -> AsyncIterator[asyncpg.Connection]:
    """Per-test autocommit connection with explicit cleanup afterwards.

    For tests where the code under test manages its own transactions.
    """
    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=5)
    conn = await pool.acquire()
    try:
        yield conn
    finally:
        await conn.execute(_CLEANUP)
        await pool.release(conn)
        await pool.close()
