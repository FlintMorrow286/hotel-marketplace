import asyncio
import uuid

import pytest

from app.db import tx


class _Connection:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    async def fetchval(self, query: str, key: str) -> bool:
        self.keys.append(key)
        return True

    async def execute(self, query: str, key: str) -> str:
        self.keys.append(key)
        return "SELECT 1"


class _Pool:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def acquire(self, **_: float) -> _Connection:
        return _Connection(self.keys)

    async def release(self, conn: _Connection) -> None:
        return None


async def _assert_serialized(first: str, second: str, expected_key: str) -> None:
    pool = _Pool()
    active = 0
    peak = 0

    async def hold(unit_type_id: str) -> None:
        nonlocal active, peak
        async with tx.unit_queue(pool, unit_type_id, wait_sec=1):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(hold(first), hold(second))

    assert peak == 1
    assert pool.keys == [f"hold:{expected_key}"] * 4


@pytest.mark.asyncio
async def test_case_variants_of_uuid_share_one_turn() -> None:
    canonical = str(uuid.uuid4())

    await _assert_serialized(canonical.upper(), canonical, canonical)


@pytest.mark.asyncio
async def test_non_uuid_key_is_canonicalized_and_still_works() -> None:
    await _assert_serialized(" Room-A ", "room-a", "room-a")
