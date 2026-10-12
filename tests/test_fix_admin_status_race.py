from __future__ import annotations

import asyncio

import asyncpg
import pytest

from app.config.settings import settings
from app.modules.admin import service as admin_service
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate


class _StatusReadBarrier:
    def __init__(self, conn: asyncpg.Connection, selected: asyncio.Event, arrivals: list[int]):
        self._conn = conn
        self._selected = selected
        self._arrivals = arrivals
        self._waited = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query: str, *args):
        result = await self._conn.fetchval(query, *args)
        if not self._waited and query.lstrip().startswith("SELECT status FROM property"):
            self._waited = True
            self._arrivals[0] += 1
            if self._arrivals[0] == 2:
                self._selected.set()
            await self._selected.wait()
        return result


@pytest.mark.asyncio
async def test_admin_status_update_conflicts_after_a_stale_read(committed_conn) -> None:
    partner_id = await auth_service.register_partner(
        committed_conn,
        PartnerRegisterRequest(
            email="fix-status-race@example.com", password="secret123", name="Tester"
        ),
    )
    prop = await property_service.create_property(
        committed_conn, partner_id, PropertyCreate(name="Race property", property_type="hotel")
    )
    await committed_conn.execute(
        "UPDATE property SET status = 'pending_moderation' WHERE id = $1", prop.id
    )

    selected = asyncio.Event()
    arrivals = [0]
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=2, max_size=2)
    try:
        async with pool.acquire() as conn_a, pool.acquire() as conn_b:
            results = await asyncio.gather(
                admin_service.set_property_status(
                    _StatusReadBarrier(conn_a, selected, arrivals), prop.id, "published"
                ),
                admin_service.set_property_status(
                    _StatusReadBarrier(conn_b, selected, arrivals), prop.id, "draft"
                ),
                return_exceptions=True,
            )
    finally:
        await pool.close()

    succeeded = [result for result in results if isinstance(result, dict)]
    conflicted = [result for result in results if isinstance(result, ValueError)]
    assert len(succeeded) == 1
    assert len(conflicted) == 1
    assert (
        await committed_conn.fetchval("SELECT status FROM property WHERE id = $1", prop.id)
        == succeeded[0]["status"]
    )
