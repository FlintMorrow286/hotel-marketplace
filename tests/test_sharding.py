"""Sharding: one queue becomes outbox_shard_count slices the workers split.

The contract this slice guards:
  1. A property's events all land in one shard, so a worker delivers them in
     order and a partner's burst cannot sit in another partner's claim order.
  2. Workers claim disjoint sets — the queue parallelises without double
     delivery.
  3. The shardless claim still sees everything (the reclaim path and the
     tests do not care which shard a row is on).
  4. A booking still jumps the backlog of its own shard.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app.config.settings import settings
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.outbox import service as outbox_service
from app.modules.outbox import worker
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate


async def _property(conn: asyncpg.Connection, email: str) -> str:
    """One property, nothing else — the claim tests do not need inventory."""
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await property_service.create_property(
        conn,
        partner_id,
        PropertyCreate(
            name="Test",
            property_type="apartment",
            city="Koktebel",
            timezone="Europe/Simferopol",
        ),
    )
    return str(prop.id)


async def _shard_of(conn: asyncpg.Connection, key: str) -> int:
    """The same expression emit() uses, asked of the same database.

    hashtext is signed, and emit() takes abs() of it, so match that exactly.
    """
    return await conn.fetchval(
        "SELECT abs(hashtext(coalesce($1, ''))) % $2",
        key,
        settings.outbox_shard_count,
    )


@pytest_asyncio.fixture
async def clean_outbox(committed_conn: asyncpg.Connection) -> AsyncIterator[None]:
    """A per-test outbox, like the backlog tests get: these count exactly what
    they themselves emit, and one leftover row would skew a shard count."""
    await committed_conn.execute("DELETE FROM outbox_shed_counter; DELETE FROM outbox_event;")
    yield


async def _due(conn: asyncpg.Connection) -> None:
    await conn.execute("UPDATE outbox_event SET next_attempt_at = now() WHERE status = 'pending'")


# ---------------------------------------------------------------- placement


@pytest.mark.asyncio
async def test_emit_assigns_the_shard_by_property(committed_conn, clean_outbox) -> None:
    """The shard comes from the property, computed in SQL."""
    prop = await _property(committed_conn, "sh1@example.com")
    await outbox_service.emit(
        committed_conn,
        aggregate="property",
        aggregate_id=prop,
        event_type=outbox_service.PROPERTY_STATUS_CHANGED,
        payload={},
        property_id=prop,
    )
    shard = await committed_conn.fetchval(
        "SELECT shard FROM outbox_event WHERE property_id IS NOT NULL LIMIT 1"
    )
    assert shard == await _shard_of(committed_conn, prop)


@pytest.mark.asyncio
async def test_a_property_stays_in_one_shard(committed_conn, clean_outbox) -> None:
    """Every event about one property lands in the same shard.

    That is what keeps a property's delivery order intact with sharded
    workers: one shard, one worker, one claim order.
    """
    prop = await _property(committed_conn, "sh2@example.com")
    for i in range(4):
        await outbox_service.emit(
            committed_conn,
            aggregate="rate_plan",
            aggregate_id=f"rp-{i}",
            event_type=outbox_service.RATE_PRICES_CHANGED,
            payload={"i": i},
            property_id=prop,
        )
    shards = {
        r["shard"]
        for r in await committed_conn.fetch(
            "SELECT shard FROM outbox_event WHERE property_id IS NOT NULL"
        )
    }
    assert shards == {await _shard_of(committed_conn, prop)}


@pytest.mark.asyncio
async def test_properties_spread_over_the_shards(committed_conn) -> None:
    """The shard function actually fans out — one hot shard would defeat it.

    The ids are fixed: the shard is a pure function of the id, and 20 random ones landed in
    fewer than half of 16 shards about once in 1800 runs.
    """
    keys = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"property/{i}")) for i in range(20)]
    shards = {await _shard_of(committed_conn, key) for key in keys}
    # 20 properties over 16 shards: a spread of at least half the shards means
    # the function is not funnelling everything into a couple of buckets.
    assert len(shards) >= settings.outbox_shard_count // 2


# ---------------------------------------------------------------- ownership


def test_shard_groups_partition_the_queue() -> None:
    """Every shard belongs to exactly one worker."""
    groups = worker.shard_groups()
    assert len(groups) == settings.outbox_workers
    seen: set[int] = set()
    for group in groups:
        assert group, "a worker must own at least one shard"
        assert not (seen & set(group)), "a shard is owned twice"
        seen |= set(group)
    assert seen == set(range(settings.outbox_shard_count))


def test_shard_groups_follow_a_smaller_worker_count(monkeypatch) -> None:
    """Fewer workers still cover every shard, just wider each."""
    monkeypatch.setattr(settings, "outbox_workers", 2)
    groups = worker.shard_groups()
    assert len(groups) == 2
    assert {s for group in groups for s in group} == set(range(settings.outbox_shard_count))


@pytest.mark.asyncio
async def test_workers_claim_disjoint_shards(committed_conn, clean_outbox) -> None:
    """Two workers never deliver the same event, and together they take all."""
    n = 24
    for i in range(n):
        prop = await _property(committed_conn, f"sh4-{i}@example.com")
        await outbox_service.emit(
            committed_conn,
            aggregate="property",
            aggregate_id=prop,
            event_type=outbox_service.PROPERTY_STATUS_CHANGED,
            payload={},
            property_id=prop,
        )
    await _due(committed_conn)

    claimed: dict[str, int] = {}
    for worker_no, group in enumerate(worker.shard_groups()):
        for event in await outbox_service.claim_due(committed_conn, 50, group):
            # A row claimed twice would mean a double delivery.
            assert event["id"] not in claimed
            claimed[event["id"]] = worker_no

    assert len(claimed) == n, "every event belongs to some worker's slice"
    assert len(set(claimed.values())) > 1, "the load actually splits across workers"


# ---------------------------------------------------------------- behaviour


@pytest.mark.asyncio
async def test_a_booking_jumps_the_backlog_of_its_shard(committed_conn, clean_outbox) -> None:
    """Priority still rules inside one shard: a booking leaves first."""
    prop = await _property(committed_conn, "sh5@example.com")
    shard = await _shard_of(committed_conn, prop)
    for i in range(5):
        await outbox_service.emit(
            committed_conn,
            aggregate="inventory",
            aggregate_id=f"ut-{i}",
            event_type=outbox_service.INVENTORY_AVAILABILITY_CHANGED,
            payload={"i": i},
            property_id=prop,
        )
    booking_id = str(uuid.uuid4())
    await outbox_service.emit(
        committed_conn,
        aggregate="booking",
        aggregate_id=booking_id,
        event_type=outbox_service.BOOKING_CONFIRMED,
        payload={},
        property_id=prop,
    )
    await _due(committed_conn)

    claimed = await outbox_service.claim_due(committed_conn, 1, [shard])
    assert len(claimed) == 1
    assert claimed[0]["aggregate"] == "booking"
    assert claimed[0]["aggregate_id"] == booking_id


@pytest.mark.asyncio
async def test_a_shardless_claim_still_sees_everything(committed_conn, clean_outbox) -> None:
    """Reclaim and tests claim without a slice: nothing is hidden from them."""
    props = [await _property(committed_conn, f"sh6-{i}@example.com") for i in range(8)]
    for prop in props:
        await outbox_service.emit(
            committed_conn,
            aggregate="property",
            aggregate_id=prop,
            event_type=outbox_service.PROPERTY_STATUS_CHANGED,
            payload={},
            property_id=prop,
        )
    await _due(committed_conn)

    # Nothing is claimed yet; the shardless sweep takes the whole queue.
    assert len(await outbox_service.claim_due(committed_conn, 50)) == 8


@pytest.mark.asyncio
async def test_metrics_expose_the_fan_out(committed_conn, clean_outbox) -> None:
    """The strip shows how much parallelism a depth number is spread across."""
    metrics = await outbox_service.queue_metrics(committed_conn)
    assert metrics["shard_count"] == settings.outbox_shard_count
    assert metrics["workers"] == settings.outbox_workers
