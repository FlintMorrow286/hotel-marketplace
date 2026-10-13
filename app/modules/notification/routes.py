"""Admin view of the mail queue: what went out, what is waiting, what could not be sent."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/emails")
async def list_emails(
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(queued|sent|failed|skipped)$"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """The newest letters with their state, and how many there are in each state.

    `body` is present only for letters the log backend kept instead of delivering.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        counts = {"queued": 0, "sent": 0, "failed": 0, "skipped": 0}
        for row in await conn.fetch("SELECT status, count(*) AS n FROM email_message GROUP BY 1"):
            counts[row["status"]] = row["n"]
        rows = await conn.fetch(
            """
            SELECT m.id::text, m.kind, m.booking_id::text, b.code AS booking_code, m.to_email,
                   m.subject, m.body, m.delivery, m.status, m.attempts, m.last_error,
                   m.created_at, m.sent_at
            FROM email_message m
            LEFT JOIN booking b ON b.id = m.booking_id
            WHERE ($1::text IS NULL OR m.status = $1)
            ORDER BY m.created_at DESC
            LIMIT $2
            """,
            status_filter,
            limit,
        )
    return {
        "backend": settings.mail_backend,
        "counts": counts,
        "items": [dict(r) for r in rows],
    }


@router.post("/emails/{email_id}/retry")
async def retry_email(
    email_id: str,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Put a letter that could not be sent back in the queue, with its attempts counted afresh."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE email_message
            SET status = 'queued', attempts = 0, next_attempt_at = now(), last_error = NULL
            WHERE id = $1 AND status = 'failed'
            RETURNING id::text, kind
            """,
            email_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="letter not found or not in 'failed' state",
        )
    return {"retried": row["id"], "kind": row["kind"]}
