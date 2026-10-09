from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.inventory import service

router = APIRouter(prefix="/v1", tags=["inventory"])

# The public read answers one row per night; a guest looks at weeks, not years.
MAX_AVAILABILITY_DAYS = 366


class CloseRangeRequest(BaseModel):
    unit_type_id: str
    date_from: dt.date
    date_to: dt.date
    closed: bool = True


class GenerateRequest(BaseModel):
    unit_type_id: str
    horizon_days: int = service.DEFAULT_HORIZON_DAYS


@router.post("/partner/inventory/generate")
async def generate_inventory(
    data: GenerateRequest,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> dict:
    """(Re)generate inventory_day rows for a unit type. Idempotent."""
    conn = await get_pool().acquire()
    try:
        owned = await conn.fetchval(
            "SELECT 1 FROM unit_type ut JOIN property p ON p.id = ut.property_id "
            "WHERE ut.id = $1 AND p.partner_id = $2",
            data.unit_type_id,
            token.sub,
        )
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit type not found")
        inserted = await service.ensure_inventory(conn, data.unit_type_id, data.horizon_days)
        return {"inserted": inserted}
    finally:
        await get_pool().release(conn)


@router.post("/partner/inventory/close")
async def close_dates(
    data: CloseRangeRequest,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> dict:
    """Close (stop sell) a date range for a unit type."""
    conn = await get_pool().acquire()
    try:
        owned = await conn.fetchval(
            "SELECT 1 FROM unit_type ut JOIN property p ON p.id = ut.property_id "
            "WHERE ut.id = $1 AND p.partner_id = $2",
            data.unit_type_id,
            token.sub,
        )
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit type not found")
        try:
            affected = await service.close_range(
                conn, data.unit_type_id, data.date_from, data.date_to, data.closed
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return {"affected": affected}
    finally:
        await get_pool().release(conn)


@router.get("/availability")
async def availability(
    unit_type_id: str = Query(...),
    date_from: dt.date = Query(...),
    date_to: dt.date = Query(...),
) -> dict:
    """Public: per-night availability over the half-open interval [from, to)."""
    if (date_to - date_from).days > MAX_AVAILABILITY_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"range is limited to {MAX_AVAILABILITY_DAYS} days",
        )
    conn = await get_pool().acquire()
    try:
        try:
            days = await service.get_availability(conn, unit_type_id, date_from, date_to)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return {
            "unit_type_id": unit_type_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "days": [{**d, "date": d["date"].isoformat()} for d in days],
        }
    finally:
        await get_pool().release(conn)
