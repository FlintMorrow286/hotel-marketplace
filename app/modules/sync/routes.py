from __future__ import annotations

from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.sync import ical_export, ical_import
from app.utils.netguard import require_public_url

router = APIRouter(prefix="/v1", tags=["sync"])


class ImportSubscriptionRequest(BaseModel):
    url: str = Field(min_length=8)


def _check_url(url: str) -> str:
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


@router.get("/ical/{feed_token}.ics", response_class=PlainTextResponse)
async def ical_feed(feed_token: str) -> PlainTextResponse:
    """Public calendar feed. The token in the URL is the only credential.

    Channels poll this and map UID to their listing. Blocked dates are exactly
    what the partner sees on the chessboard.
    """
    conn = await get_pool().acquire()
    try:
        feed = await ical_export.get_feed_by_token(conn, feed_token)
        if feed is None or not feed["enabled"]:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
        body = await ical_export.build_calendar(conn, feed["unit_type_id"])
        if body is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
        return PlainTextResponse(
            content=body,
            media_type="text/calendar; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
    finally:
        await get_pool().release(conn)


@router.get("/partner/unit-types/{unit_type_id}/ical-feed")
async def get_ical_feed(
    unit_type_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Current export feed for a unit type the partner owns."""
    conn = await get_pool().acquire()
    try:
        feed = await ical_export.get_feed_for_unit_type(conn, unit_type_id, token.sub)
        if feed is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
        return {**feed, "url": _feed_url(feed["token"])}
    finally:
        await get_pool().release(conn)


@router.post("/partner/unit-types/{unit_type_id}/ical-feed")
async def rotate_ical_feed(
    unit_type_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Create or rotate the feed token. The old URL stops working at once."""
    conn = await get_pool().acquire()
    try:
        try:
            feed = await ical_export.create_or_rotate_feed(conn, unit_type_id, token.sub)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {**feed, "url": _feed_url(feed["token"])}
    finally:
        await get_pool().release(conn)


def _feed_url(feed_token: str) -> str:
    from app.config.settings import settings

    return f"{settings.app_base_url}/v1/ical/{feed_token}.ics"


# ---- iCal import -----------------------------------------------------------


async def _owned_subscription(conn, unit_type_id: str, partner_id: str) -> dict | None:
    return await conn.fetchrow(
        """
        SELECT s.id::text AS id, s.url, s.enabled, s.last_synced_at,
               s.last_status, s.last_error, s.last_blocked
        FROM ical_subscription s
        JOIN unit_type ut ON ut.id = s.unit_type_id
        JOIN property  p  ON p.id = ut.property_id
        WHERE s.unit_type_id = $1 AND p.partner_id = $2
        """,
        unit_type_id,
        partner_id,
    )


@router.get("/partner/unit-types/{unit_type_id}/ical-import")
async def get_ical_import(
    unit_type_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Current import subscription for a unit type the partner owns."""
    conn = await get_pool().acquire()
    try:
        sub = await _owned_subscription(conn, unit_type_id, token.sub)
        if sub is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="subscription not found"
            )
        return dict(sub)
    finally:
        await get_pool().release(conn)


@router.put("/partner/unit-types/{unit_type_id}/ical-import")
async def set_ical_import(
    unit_type_id: str,
    data: ImportSubscriptionRequest,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Create or replace the imported calendar URL. Sync runs on the next tick."""
    _check_url(data.url)
    await require_public_url(data.url)
    conn = await get_pool().acquire()
    try:
        owned = await conn.fetchval(
            """
            SELECT 1 FROM unit_type ut
            JOIN property p ON p.id = ut.property_id
            WHERE ut.id = $1 AND p.partner_id = $2
            """,
            unit_type_id,
            token.sub,
        )
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit type not found")
        row = await conn.fetchrow(
            """
            INSERT INTO ical_subscription (unit_type_id, url)
            VALUES ($1, $2)
            ON CONFLICT (unit_type_id) DO UPDATE
                SET url = EXCLUDED.url,
                    last_synced_at = NULL,
                    last_status = 'pending',
                    last_error = NULL
            RETURNING id::text, url, enabled, last_synced_at, last_status,
                      last_error, last_blocked
            """,
            unit_type_id,
            data.url,
        )
        return dict(row)
    finally:
        await get_pool().release(conn)


@router.delete("/partner/unit-types/{unit_type_id}/ical-import")
async def delete_ical_import(
    unit_type_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Stop importing. Imported closures stay in place until the partner clears them."""
    conn = await get_pool().acquire()
    try:
        sub = await _owned_subscription(conn, unit_type_id, token.sub)
        if sub is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="subscription not found"
            )
        await conn.execute("DELETE FROM ical_subscription WHERE id = $1", sub["id"])
        return {"deleted": sub["id"]}
    finally:
        await get_pool().release(conn)


@router.post("/partner/unit-types/{unit_type_id}/ical-import/sync")
async def sync_ical_import_now(
    unit_type_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """Fetch the calendar right now instead of waiting for the poller."""
    conn = await get_pool().acquire()
    try:
        sub = await _owned_subscription(conn, unit_type_id, token.sub)
        if sub is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="subscription not found"
            )
        try:
            return await ical_import.sync_subscription(conn, sub["id"])
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    finally:
        await get_pool().release(conn)
