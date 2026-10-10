from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.property import service
from app.modules.property.schemas import (
    PropertyCreate,
    PropertyOut,
    PropertyUpdate,
)
from app.modules.property.unit_type_routes import UnitTypeOut

router = APIRouter(prefix="/v1", tags=["property"])

# `guests` is compared with an int4 column and `offset` is a bigint. Past the type
# asyncpg raises DataError, which uuid_http answers as a 404 "not found".
INT4_MAX = 2**31 - 1
INT8_MAX = 2**63 - 1


async def _partner_conn(token: Annotated[TokenData, Depends(require_scope("partner"))]):
    """Acquire a DB connection scoped to the authenticated partner."""
    pool = get_pool()
    conn = await pool.acquire()
    await conn.execute("SELECT set_config('app.partner_id', $1, true)", token.sub)
    try:
        yield conn
    finally:
        await pool.release(conn)


@router.post(
    "/partner/properties",
    response_model=PropertyOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_property(
    data: PropertyCreate,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> PropertyOut:
    conn = await get_pool().acquire()
    try:
        return await service.create_property(conn, token.sub, data)
    finally:
        await get_pool().release(conn)


@router.get("/partner/properties", response_model=list[PropertyOut])
async def list_my_properties(
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> list[PropertyOut]:
    conn = await get_pool().acquire()
    try:
        return await service.get_partner_properties(conn, token.sub)
    finally:
        await get_pool().release(conn)


@router.patch("/partner/properties/{property_id}", response_model=PropertyOut)
async def update_my_property(
    property_id: str,
    data: PropertyUpdate,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> PropertyOut:
    conn = await get_pool().acquire()
    try:
        result = await service.update_property(conn, property_id, token.sub, data)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="property not found")
        return result
    finally:
        await get_pool().release(conn)


class PropertyPage(BaseModel):
    """One page of the catalog: the items plus what the UI needs to page."""

    items: list[dict]
    total: int
    limit: int
    offset: int


@router.get("/properties", response_model=PropertyPage)
async def list_public_properties(
    city: str | None = None,
    guests: Annotated[int | None, Query(ge=1, le=INT4_MAX)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    date_from: dt.date | None = Query(default=None),
    date_to: dt.date | None = Query(default=None),
    amenities: list[str] | None = Query(default=None),
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0, le=INT8_MAX)] = 0,
) -> PropertyPage:
    """Public catalog — only published properties, no partner-internal fields.

    `guests` keeps a property that has any room type sleeping that many.
    `q` matches name or city. `date_from`/`date_to` keep a property that has a
    room type free for the whole half-open stay — and the range must run
    forward: `checkout <= checkin` is not a stay, so it is a client error
    rather than a filter that silently matches nothing. `amenities` keeps a
    property offering *all* of the given keys. Page by limit/offset; the total
    comes back so the UI shows how many results the filters left.
    """
    if date_from is not None and date_to is not None and date_to <= date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_to must be after date_from",
        )
    conn = await get_pool().acquire()
    try:
        return PropertyPage(
            **await service.list_public_properties(
                conn, city, guests, q, date_from, date_to, amenities, limit, offset
            )
        )
    finally:
        await get_pool().release(conn)


@router.get("/properties/{property_id}", response_model=dict)
async def get_public_property(property_id: str) -> dict:
    conn = await get_pool().acquire()
    try:
        row = await service.get_public_property(conn, property_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="property not found")
        return row
    finally:
        await get_pool().release(conn)


@router.get("/public/unit-types/{property_id}", response_model=list[UnitTypeOut])
async def list_public_unit_types(property_id: str) -> list[UnitTypeOut]:
    """Public room list for a published property - what the guest books.

    Unauthenticated (this is the catalog); unpublished properties answer 404,
    same as the public property endpoint, so the two never disagree.
    """

    conn = await get_pool().acquire()
    try:
        published = await conn.fetchval(
            "SELECT 1 FROM property WHERE id = $1 AND status = 'published'",
            property_id,
        )
        if not published:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="property not found")
        rooms = await service.list_public_unit_types(conn, property_id)
        return [UnitTypeOut(**r) for r in rooms]
    finally:
        await get_pool().release(conn)
