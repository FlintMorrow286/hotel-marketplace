"""Outbox: write an event in the same transaction as the change it describes.

The API is deliberately tiny — `emit()` at the call site, nothing else — so
that adding an event to a service is a one-liner inside its transaction. A
background worker (worker.py) owns the delivery half.

Ownership: every event carries the property it is about, so the worker can
match it to a partner's subscription without joining business tables at
delivery time.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import asyncpg

from app.config.settings import settings
from app.utils import secrets as secrets_util
from app.utils.logger import get_logger
from app.utils.secrets import seal as seal_secret

log = get_logger(__name__)

# Event types. A subscription lists the ones it wants, or '*' for all.
BOOKING_CONFIRMED = "booking.confirmed"
BOOKING_CANCELLED = "booking.cancelled"
RATE_PRICES_CHANGED = "rate.prices_changed"
INVENTORY_AVAILABILITY_CHANGED = "inventory.availability_changed"
PROPERTY_STATUS_CHANGED = "property.status_changed"

ALL_EVENT_TYPES = (
    BOOKING_CONFIRMED,
    BOOKING_CANCELLED,
    RATE_PRICES_CHANGED,
    INVENTORY_AVAILABILITY_CHANGED,
    PROPERTY_STATUS_CHANGED,
)


async def emit(
    conn: asyncpg.Connection,
    *,
    aggregate: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    property_id: str,
) -> None:
    """Append an outbox row. Caller must be inside the business transaction.

    A failure here must not break the business operation: the event is
    best-effort, the booking is the source of truth. That also covers the
    backlog limit — a bulk event past the limit is shed on purpose rather than
    queued (see backlog.py), and its drop is counted instead of remembered.
    """
    from app.config.settings import settings
    from app.modules.outbox import backlog

    priority = backlog.PRIORITY_BOOKING if aggregate == "booking" else backlog.PRIORITY_BULK
    limit = settings.outbox_max_pending
    in_transaction = conn.is_in_transaction()
    try:
        # A savepoint. A statement that fails inside the caller's transaction aborts that whole
        # transaction in Postgres, so swallowing the error without one left the caller holding a
        # dead transaction: its next statement failed with InFailedSQLTransactionError (a 500
        # under load, seen once in 4000 random operations on two workers), or its COMMIT quietly
        # became a ROLLBACK. With the savepoint only the event is lost.
        async with conn.transaction():
            if not await backlog.admit(conn, priority=priority, limit=limit):
                await backlog.count_shed(conn, aggregate=aggregate, event_type=event_type)
                log.warning(
                    "outbox-shed",
                    event_type=event_type,
                    aggregate=aggregate,
                    limit=limit,
                )
                return

            await conn.execute(
                """
                INSERT INTO outbox_event
                    (aggregate, aggregate_id, event_type, payload, property_id,
                     priority, shard)
                VALUES ($1, $2, $3, $4, $5, $6,
                        abs(hashtext(coalesce($8, $2))) % $7)
                """,
                aggregate,
                aggregate_id,
                event_type,
                json.dumps(payload, default=str),
                property_id,
                priority,
                settings.outbox_shard_count,
                property_id,
            )
    except (asyncpg.SerializationError, asyncpg.DeadlockDetectedError):
        # Not an outbox fault: the caller's transaction cannot be serialised, and its retry
        # loop (app.db.tx.serializable) has to see that. Outside a transaction the conflict
        # was only the event's own, and is as lost as any other failure here.
        if in_transaction:
            raise
        log.warning("outbox-emit-failed", event_type=event_type, error="conflict")
    except Exception as exc:
        # Never let a logging failure roll back a booking. A lost booking event is the one that
        # matters (the channels are not told), so it is logged as an error.
        record = log.error if aggregate == "booking" else log.warning
        record("outbox-emit-failed", event_type=event_type, aggregate=aggregate, error=str(exc))


async def property_of_unit_type(conn: asyncpg.Connection, unit_type_id: str) -> str | None:
    return await conn.fetchval(
        "SELECT property_id::text FROM unit_type WHERE id = $1", unit_type_id
    )


async def property_of_rate_plan(conn: asyncpg.Connection, rate_plan_id: str) -> str | None:
    return await conn.fetchval(
        "SELECT ut.property_id::text FROM rate_plan rp "
        "JOIN unit_type ut ON ut.id = rp.unit_type_id "
        "WHERE rp.id = $1",
        rate_plan_id,
    )


# ---- worker-facing half ----------------------------------------------------


async def claim_due(
    conn: asyncpg.Connection, limit: int = 50, shards: list[int] | None = None
) -> list[dict]:
    """Claim pending events for delivery.

    Claims by flipping status to 'delivering' in the same statement, exactly
    like the iCal importer's list_due: no lock is held across the network calls
    that follow. If this process dies, the reclaim loop puts the rows back.

    `shards` restricts the claim to a slice of the queue — a delivery worker
    owns a disjoint subset of shards, so the workers parallelise without
    stealing each other's rows (SKIP LOCKED would keep that correct even if
    they overlapped). None claims across the whole table: the reclaim path and
    tests do not care which shard a row is on.
    """
    rows = await conn.fetch(
        """
        WITH due AS (
            SELECT id FROM outbox_event
            WHERE status = 'pending' AND next_attempt_at <= now()
              AND ($2::smallint[] IS NULL OR shard = ANY($2::smallint[]))
            ORDER BY priority, next_attempt_at, happened_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE outbox_event
        SET status = 'delivering', claimed_at = now()
        WHERE id IN (SELECT id FROM due)
        RETURNING id::text, aggregate, aggregate_id, event_type, payload,
                  attempts, max_attempts, property_id::text, happened_at,
                  priority, next_attempt_at
        """,
        limit,
        shards,
    )
    # The UPDATE re-scans the table, so RETURNING comes back in the scan's
    # order, not the CTE's. Claiming is still correct either way (the batch is
    # the same set of rows), but a booking must leave first, so re-apply the
    # queue order here.
    rows.sort(key=lambda r: (r["priority"], r["next_attempt_at"], r["happened_at"]))
    out = []
    for r in rows:
        event = {k: v for k, v in dict(r).items() if k not in ("priority", "next_attempt_at")}
        # asyncpg returns jsonb as text; deliver it as a real object.
        if isinstance(event["payload"], str):
            event["payload"] = json.loads(event["payload"])
        out.append(event)
    return out


async def reclaim_stale(conn: asyncpg.Connection, timeout_sec: int = 300) -> int:
    """Return rows a dead worker left in 'delivering' back to the queue."""
    result = await conn.execute(
        """
        UPDATE outbox_event
        SET status = 'pending', claimed_at = NULL
        WHERE status = 'delivering' AND claimed_at < now() - ($1 || ' seconds')::interval
        """,
        str(timeout_sec),
    )
    n = int(result.split()[-1]) if result.startswith("UPDATE") else 0
    if n:
        log.info("outbox-reclaimed", count=n)
    return n


async def mark_published(conn: asyncpg.Connection, event_id: str) -> None:
    await conn.execute(
        """
        UPDATE outbox_event
        SET status = 'published', published_at = now(), last_error = NULL
        WHERE id = $1
        """,
        event_id,
    )


async def release_claim(conn: asyncpg.Connection, event_id: str) -> None:
    """Give a claimed event back to the queue, without burning an attempt.

    Used when delivery was throttled: nothing failed, the subscriber simply
    may not be pushed yet. The event is due again on the next cycle.
    """
    await conn.execute(
        """
        UPDATE outbox_event
        SET status = 'pending', claimed_at = NULL
        WHERE id = $1 AND status = 'delivering'
        """,
        event_id,
    )


async def queue_metrics(conn: asyncpg.Connection) -> dict:
    """Queue health: depth, dead letters and how fast things actually leave.

    The reconciliation viewer shows the ledger; this is the number a staffer
    glances at to see whether the queue is keeping up.
    """
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM outbox_event WHERE status = 'pending')    AS pending,
            (SELECT count(*) FROM outbox_event WHERE status = 'delivering') AS delivering,
            (SELECT count(*) FROM outbox_event WHERE status = 'failed')     AS failed,
            (SELECT count(*) FROM outbox_event
             WHERE status = 'pending' AND next_attempt_at > now())          AS scheduled,
            (SELECT coalesce(round(
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (published_at - happened_at))
                )::numeric, 1), 0)
             FROM outbox_event
             WHERE status = 'published'
               AND published_at > now() - interval '1 hour')                AS median_latency_sec,
            (SELECT coalesce(extract(EPOCH FROM (now() - min(happened_at)))::int, 0)
             FROM outbox_event WHERE status = 'pending')                    AS oldest_pending_sec,
            (SELECT coalesce(sum(n), 0)::int FROM outbox_shed_counter)      AS shed_total
        """,
    )
    return {
        "pending": row["pending"],
        "delivering": row["delivering"],
        "failed": row["failed"],
        "scheduled_for_retry": row["scheduled"],
        "median_latency_sec": float(row["median_latency_sec"]),
        "oldest_pending_sec": int(row["oldest_pending_sec"]),
        # The configured limits, so the UI draws one line instead of guessing
        # at one: how deep the queue may grow before bulk events are shed, and
        # how old a pending event may be before the strip flags it.
        "depth_limit": settings.outbox_max_pending,
        "lag_alert_sec": settings.outbox_lag_alert_sec,
        "shed_total": int(row["shed_total"]),
        # The queue's fan-out, so the strip shows how much parallelism a depth
        # number is spread across — one depth behind four workers is not the
        # same emergency as one behind one.
        "shard_count": settings.outbox_shard_count,
        "workers": settings.outbox_workers,
    }


async def mark_retry(conn: asyncpg.Connection, event_id: str, attempts: int, error: str) -> None:
    """Schedule the next attempt with exponential backoff, or dead-letter."""
    row = await conn.fetchrow(
        """
        UPDATE outbox_event
        SET attempts = $2,
            status = CASE WHEN $2 >= max_attempts THEN 'failed' ELSE 'pending' END,
            next_attempt_at = CASE
                WHEN $2 >= max_attempts THEN next_attempt_at
                ELSE now() + (least($2, 8) ^ 2 || ' seconds')::interval
            END,
            last_error = left($3, 500)
        WHERE id = $1
        RETURNING status
        """,
        event_id,
        attempts,
        error,
    )
    if row is not None and row["status"] == "failed":
        log.warning("outbox-dead-lettered", event_id=event_id, attempts=attempts)


async def record_delivery(
    conn: asyncpg.Connection,
    event_id: str,
    subscription_id: str,
    ok: bool,
    status_code: int | None,
    error: str | None,
) -> None:
    """Upsert the per-subscription delivery log.

    The ledger is partitioned by delivered_at and its uniqueness is enforced
    per partition, so ON CONFLICT on (event_id, subscription_id) is not
    available: the pair has no index the planner can infer a conflict from.
    Update-then-insert is safe because the claim is SKIP LOCKED — one worker
    owns an event end to end, so the pair is only ever written from one place —
    and the per-partition unique index still raises if a duplicate is ever
    attempted, rather than silently keeping two rows.
    """
    delivery_status = "success" if ok else "failed"
    result = await conn.execute(
        """
        UPDATE webhook_delivery
        SET status = $3, status_code = $4, error = left($5, 500), delivered_at = now()
        WHERE event_id = $1 AND subscription_id = $2
        """,
        event_id,
        subscription_id,
        delivery_status,
        status_code,
        error,
    )
    if result != "UPDATE 0":
        return
    await conn.execute(
        """
        INSERT INTO webhook_delivery (event_id, subscription_id, status, status_code, error)
        VALUES ($1, $2, $3, $4, left($5, 500))
        """,
        event_id,
        subscription_id,
        delivery_status,
        status_code,
        error,
    )
    await conn.execute(
        """
        UPDATE webhook_subscription
        SET last_delivery_at = now(),
            last_status = $2,
            last_error = CASE WHEN $2 = 'success' THEN NULL ELSE left($3, 500) END
        WHERE id = $1
        """,
        subscription_id,
        "success" if ok else "failed",
        error,
    )


async def subscribers_for(conn: asyncpg.Connection, event_id: str, event_type: str) -> list[dict]:
    """Subscriptions that still need this event.

    Joins the event's property to its owner's subscriptions, keeps the ones
    subscribed to this event type, and drops any that already answered 2xx —
    so a retry after a partial failure only reaches what it did not reach.
    """
    rows = await conn.fetch(
        """
        SELECT s.id::text, s.url, s.secret_sealed
        FROM outbox_event e
        JOIN property p ON p.id = e.property_id
        JOIN webhook_subscription s ON s.partner_id = p.partner_id
        WHERE e.id = $1
          AND s.enabled
          AND ('*' = ANY (s.event_types) OR $2 = ANY (s.event_types))
          AND NOT EXISTS (
              SELECT 1 FROM webhook_delivery d
              WHERE d.event_id = e.id AND d.subscription_id = s.id AND d.status = 'success'
          )
        """,
        event_id,
        event_type,
    )
    out = []
    for r in rows:
        sub = dict(r)
        secret = await _read_secret(conn, sub.pop("secret_sealed"), sub["id"])
        if secret is None:
            # Sealed storage cannot be read and the plaintext is gone: the
            # subscription cannot be delivered to, which is not the event's
            # fault, so it is left out rather than failing the batch.
            log.warning("subscription-secret-unreadable", subscription_id=sub["id"])
            continue
        sub["secret"] = secret
        out.append(sub)
    return out


_LEGACY_PREFIX = "LEGACY:"


async def _read_secret(conn: asyncpg.Connection, stored: str | None, sub_id: str) -> str | None:
    """Read a subscription secret, sealing a legacy plaintext on the way.

    The plaintext is only ever held here, on the path that already needed it to
    sign a delivery — so this is the cheapest correct place to migrate a row.
    """
    if stored is None:
        return None
    if not stored.startswith(_LEGACY_PREFIX):
        try:
            return secrets_util.unseal(stored)
        except RuntimeError:
            # Tampered token, or the seal key rotated and this secret is gone.
            return None
    plaintext = stored[len(_LEGACY_PREFIX) :]
    try:
        sealed = seal_secret(plaintext)
    except RuntimeError:
        # No seal key configured: deliver what we can, and let the caller
        # upgrade the row once configuration is fixed.
        return plaintext
    await conn.execute(
        "UPDATE webhook_subscription SET secret_sealed = $2 WHERE id = $1",
        sub_id,
        sealed,
    )
    log.info("webhook-secret-sealed", subscription_id=sub_id)
    return plaintext


# ---- reconciliation --------------------------------------------------------


# A booking's delivery state, as the staff sees it. The order matters: it is
# also the order the summary counts bookings in.
NO_EVENT = "no_event"
NO_LISTENER = "no_listener"
FAILED = "failed"
QUEUED = "queued"
DELIVERED = "delivered"
UNDELIVERED = "undelivered"
PARTIAL = "partial"

_BOOKING_EVENT_STATES = (DELIVERED, QUEUED, FAILED, UNDELIVERED, PARTIAL, NO_LISTENER)


def _delivery_status(row: dict) -> str:
    """Classify one booking against its latest outbox event.

    A subscription counts as "expected" only if it is enabled now, wants this
    event type, and already existed when the event fired — a hook created later
    cannot be blamed for missing an older event. The delivered_ok counter uses
    the same set, so the two numbers are always comparable.
    """
    if row["event_id"] is None:
        return NO_EVENT
    if row["expected_subscribers"] == 0:
        # Nobody is listening. This is not an error: the partner has no hook,
        # or disabled it. The event may still be queued; if a hook appears, it
        # will go out on the next cycle.
        return NO_LISTENER
    if row["event_status"] == "failed":
        return FAILED
    if row["delivered_ok"] >= row["expected_subscribers"]:
        # Every webhook that wanted the event has answered 2xx. The event may
        # still be pending because a hook created after it keeps failing —
        # that hook is not counted as expected, so this booking is delivered.
        return DELIVERED
    if row["event_status"] in ("pending", "delivering"):
        return QUEUED
    if row["delivered_ok"] == 0:
        return UNDELIVERED
    return PARTIAL


async def reconciliation(
    conn: asyncpg.Connection,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict:
    """Channel bookings vs the delivery of their latest booking event.

    This is the report for "the channel says they never got the booking": for
    every booking pushed in through the channel API it shows whether the
    confirming (or cancelling) event actually reached every webhook that
    wanted it.

    The date range filters on booking creation; both ends are optional.
    """
    rows = await conn.fetch(
        """
        SELECT
            b.id::text             AS booking_id,
            b.code,
            b.status               AS booking_status,
            b.checkin_date,
            b.checkout_date,
            b.total_amount::float8,
            b.created_at,
            e.id::text             AS event_id,
            e.event_type           AS event_type,
            e.status               AS event_status,
            e.happened_at          AS event_happened_at,
            e.last_error           AS event_last_error,
            COALESCE(exp.n, 0)     AS expected_subscribers,
            COALESCE(ok.n, 0)      AS delivered_ok
        FROM booking b
        JOIN property p ON p.id = b.property_id
        LEFT JOIN LATERAL (
            SELECT id, event_type, status, happened_at, last_error
            FROM outbox_event
            WHERE aggregate = 'booking' AND aggregate_id = b.id::text
            ORDER BY happened_at DESC
            LIMIT 1
        ) e ON true
        LEFT JOIN LATERAL (
            SELECT count(*)::int AS n
            FROM webhook_subscription s
            WHERE s.partner_id = p.partner_id
              AND s.enabled
              AND s.created_at <= e.happened_at
              AND ('*' = ANY (s.event_types) OR e.event_type = ANY (s.event_types))
        ) exp ON true
        LEFT JOIN LATERAL (
            SELECT count(*)::int AS n
            FROM webhook_delivery d
            JOIN webhook_subscription s ON s.id = d.subscription_id
            WHERE d.event_id = e.id
              AND d.status = 'success'
              AND s.partner_id = p.partner_id
              AND s.enabled
              AND s.created_at <= e.happened_at
        ) ok ON true
        WHERE b.origin = 'channel'
          AND ($1::date IS NULL OR b.created_at::date >= $1)
          AND ($2::date IS NULL OR b.created_at::date <= $2)
        ORDER BY b.created_at DESC
        LIMIT 200
        """,
        date_from,
        date_to,
    )

    bookings = []
    summary: dict[str, int] = {"total": len(rows), **{s: 0 for s in _BOOKING_EVENT_STATES}}
    for r in rows:
        state = _delivery_status(r)
        summary[state] = summary.get(state, 0) + 1
        bookings.append(
            {
                "booking_id": r["booking_id"],
                "code": r["code"],
                "status": r["booking_status"],
                "checkin_date": r["checkin_date"].isoformat(),
                "checkout_date": r["checkout_date"].isoformat(),
                "total_amount": r["total_amount"],
                "created_at": r["created_at"].isoformat(),
                "delivery_status": state,
                "event": (
                    {
                        "id": r["event_id"],
                        "event_type": r["event_type"],
                        "status": r["event_status"],
                        "happened_at": r["event_happened_at"].isoformat(),
                        "last_error": r["event_last_error"],
                        "expected_subscribers": r["expected_subscribers"],
                        "delivered_ok": r["delivered_ok"],
                    }
                    if r["event_id"]
                    else None
                ),
            }
        )

    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "summary": summary,
        "bookings": bookings,
    }
