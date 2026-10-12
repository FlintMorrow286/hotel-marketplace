from __future__ import annotations

import pytest

from app.config.settings import settings
from scripts.chaos_bookings import run_inproc_round
from scripts.check_invariants import run_self_test


@pytest.mark.asyncio
async def test_invariant_checker_self_test(committed_conn) -> None:
    await run_self_test(settings.pool_dsn)


@pytest.mark.asyncio
async def test_seeded_in_process_chaos_has_no_violations(committed_conn) -> None:
    result = await run_inproc_round(
        seed=358,
        ops=24,
        guests=6,
        dsn=settings.pool_dsn,
    )
    assert result["violations"] == []
