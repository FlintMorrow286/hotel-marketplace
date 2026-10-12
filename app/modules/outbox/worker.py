"""Outbox delivery loop.

Same shape as the reaper and the iCal poller: a tolerant forever loop that
claims a batch, does the slow network work outside any transaction, and never
lets one bad iteration kill the next one.

Delivery states:
  pending      -> waiting for a slot
  delivering   -> claimed, HTTP in flight
  published    -> every subscriber answered 2xx (or there were none)
  failed       -> max_attempts reached; dead-lettered for manual retry
"""

from __future__ import annotations

import asyncio
import contextlib

import asyncpg

from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.outbox import deliver, service
from app.utils import ratelimit
from app.utils.logger import get_logger
from app.utils.netguard import redact_url

log = get_logger(__name__)

# Rows sitting in 'delivering' longer than this are from a dead worker.
STALE_SEC = 300
BATCH = 50


def shards_for_worker(worker: int, workers: int, shard_count: int) -> list[int]:
    """The shards one delivery loop owns.

    Worker k gets every shard whose number is k mod workers — a disjoint
    spread over the queue, and a contiguous restart keeps each shard with one
    owner. With more shards than workers (the normal case) every worker holds
    several; with fewer, shards repeat across workers and SKIP LOCKED keeps
    even that configuration correct.
    """
    return [s for s in range(shard_count) if s % workers == worker]


def shard_groups() -> list[list[int]]:
    """One shard list per delivery worker, in worker order."""
    workers = settings.outbox_workers
    return [shards_for_worker(k, workers, settings.outbox_shard_count) for k in range(workers)]


async def outbox_loop(shards: list[int] | None = None) -> None:
    """Forever: reclaim orphans, then deliver what is due in this slice.

    `shards` pins this loop to a subset of the queue; None keeps the old
    single-queue behaviour (used by tests, which do not start the loops).
    """
    while True:
        try:
            pool = get_pool()
            conn = await pool.acquire()
            try:
                await service.reclaim_stale(conn, STALE_SEC)
                await _run_batch(conn, shards)
            finally:
                await pool.release(conn)
        except Exception as exc:
            log.error("outbox-loop-error", error=str(exc), shards=shards)
        await asyncio.sleep(settings.outbox_poll_interval_sec)


async def _run_batch(conn: asyncpg.Connection, shards: list[int] | None) -> None:
    events = await service.claim_due(conn, BATCH, shards)
    if not events:
        return

    for event in events:
        with contextlib.suppress(Exception):
            await _deliver_one(conn, event)


async def _deliver_one(conn: asyncpg.Connection, event: dict) -> None:
    subs = await service.subscribers_for(conn, event["id"], event["event_type"])

    # Nobody is listening (or everyone already got it): the event is done.
    if not subs:
        await service.mark_published(conn, event["id"])
        return

    errors: list[str] = []
    failures_for_log: list[dict[str, str]] = []
    throttled = 0
    for sub in subs:
        # Never push a subscription faster than its rate, even when our queue
        # is bursting: a partner's hook that we DDoS is a partner we lose.
        allowed, retry_after = await ratelimit.acquire(
            f"rl:deliver:{sub['id']}", settings.webhook_rate_per_sec, 1
        )
        if not allowed:
            throttled += 1
            log.debug("webhook-throttled", subscription_id=sub["id"], retry_after=retry_after)
            continue

        ok, status_code, error = await deliver.deliver(sub["url"], sub["secret"], event)
        await service.record_delivery(conn, event["id"], sub["id"], ok, status_code, error)
        if not ok:
            errors.append(f"{sub['url']}: {error}")
            failures_for_log.append(
                {
                    "subscription_id": str(sub["id"]),
                    "url": redact_url(sub["url"]),
                    "error": error or "delivery failed",
                }
            )

    if not errors and not throttled:
        await service.mark_published(conn, event["id"])
        log.info("outbox-published", event_id=event["id"], subs=len(subs))
    elif errors:
        await service.mark_retry(conn, event["id"], event["attempts"] + 1, "; ".join(errors))
        log.warning(
            "outbox-delivery-failed",
            event_id=event["id"],
            attempts=event["attempts"] + 1,
            failures=failures_for_log,
        )
    else:
        # Every subscriber was throttled, none failed: hand the claim back and
        # let the next cycle try. No attempt is spent on a rate limit.
        await service.release_claim(conn, event["id"])
        log.info("outbox-throttled", event_id=event["id"], subs=len(subs))
