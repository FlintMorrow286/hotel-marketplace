"""Outbox-facing routes.

Two audiences:
- the partner subscribing their channel's webhook (JWT scope partner);
- the staff watching what got delivered and retrying what died (admin).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.outbox import service
from app.utils.netguard import require_public_url
from app.utils.secrets import seal as seal_secret

router = APIRouter(prefix="/v1", tags=["outbox"])


# ---- partner: webhook subscriptions ---------------------------------------


class WebhookCreate(BaseModel):
    url: str = Field(min_length=8)
    secret: str = Field(min_length=8, max_length=200)
    event_types: list[str] = Field(default_factory=lambda: ["*"])


def _check_url(url: str) -> str:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        valid = parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:  # e.g. an unbalanced "[" in the host
        valid = False
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="url must be http(s)://…",
        )
    return url


@router.get("/partner/webhooks")
async def list_webhooks(
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> list[dict]:
    """The partner's subscriptions. The secret is never returned."""
    conn = await get_pool().acquire()
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, url, event_types, enabled, last_delivery_at,
                   last_status, last_error, created_at
            FROM webhook_subscription
            WHERE partner_id = $1
            ORDER BY created_at DESC
            """,
            token.sub,
        )
        return [dict(r) for r in rows]
    finally:
        await get_pool().release(conn)


@router.post("/partner/webhooks", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    data: WebhookCreate,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Subscribe a webhook. The partner keeps the same secret on their side."""
    _check_url(data.url)
    await require_public_url(data.url)
    unknown = [t for t in data.event_types if t != "*" and t not in service.ALL_EVENT_TYPES]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown event types: {unknown}",
        )
    conn = await get_pool().acquire()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_subscription (partner_id, url, event_types, secret_sealed)
            VALUES ($1, $2, $3, $4)
            RETURNING id::text, url, event_types, enabled, created_at
            """,
            token.sub,
            data.url,
            data.event_types,
            seal_secret(data.secret),
        )
        return dict(row)
    finally:
        await get_pool().release(conn)


@router.delete("/partner/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Unsubscribe. Events already queued for it are not retried."""
    conn = await get_pool().acquire()
    try:
        result = await conn.execute(
            "DELETE FROM webhook_subscription WHERE id = $1 AND partner_id = $2",
            webhook_id,
            token.sub,
        )
        if not result.endswith(" 1"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook not found")
        return {"deleted": webhook_id}
    finally:
        await get_pool().release(conn)


# ---- admin: reconciliation ------------------------------------------------


@router.get("/admin/outbox")
async def list_outbox(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> list[dict]:
    """The delivery ledger: what happened, what got out, what is stuck."""
    conn = await get_pool().acquire()
    try:
        args: list = [limit]
        where = ""
        if status_filter:
            args = [status_filter, limit]
            where = "WHERE e.status = $1"
        rows = await conn.fetch(
            f"""
            SELECT e.id::text, e.aggregate, e.aggregate_id, e.event_type,
                   e.payload, e.status, e.attempts, e.max_attempts,
                   e.last_error, e.happened_at, e.published_at,
                   (SELECT count(*) FROM webhook_delivery d
                    WHERE d.event_id = e.id AND d.status = 'success') AS delivered_ok,
                   (SELECT count(*) FROM webhook_delivery d
                    WHERE d.event_id = e.id AND d.status = 'failed')  AS delivered_fail
            FROM outbox_event e
            {where}
            ORDER BY e.happened_at DESC
            LIMIT $%d
            """
            % len(args),
            *args,
        )
        return [dict(r) for r in rows]
    finally:
        await get_pool().release(conn)


@router.get("/admin/outbox/metrics")
async def outbox_metrics(
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Queue health at a glance: depth, dead letters, how fast events leave."""
    conn = await get_pool().acquire()
    try:
        return await service.queue_metrics(conn)
    finally:
        await get_pool().release(conn)


@router.get("/admin/reconciliation")
async def reconciliation(
    date_from: dt.date | None = Query(default=None),
    date_to: dt.date | None = Query(default=None),
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Channel bookings vs the delivery of their booking events.

    For every booking pushed in through the channel API: did the confirming
    (or cancelling) event reach every webhook that wanted it? This is the
    report to reach for when a partner claims they never got a booking.
    """
    conn = await get_pool().acquire()
    try:
        return await service.reconciliation(conn, date_from, date_to)
    finally:
        await get_pool().release(conn)


@router.post("/admin/outbox/{event_id}/retry")
async def retry_outbox_event(
    event_id: str,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Put a dead-lettered event back in the queue."""
    conn = await get_pool().acquire()
    try:
        row = await conn.fetchrow(
            """
            UPDATE outbox_event
            SET status = 'pending', next_attempt_at = now(), last_error = NULL
            WHERE id = $1 AND status = 'failed'
            RETURNING id::text, event_type
            """,
            event_id,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="event not found or not in 'failed' state",
            )
        return {"retried": row["id"], "event_type": row["event_type"]}
    finally:
        await get_pool().release(conn)
