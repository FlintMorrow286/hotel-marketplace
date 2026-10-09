from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.rate import calendar, service

router = APIRouter(prefix="/v1", tags=["rates"])


class RatePlanCreate(BaseModel):
    unit_type_id: str
    name: str = Field(min_length=2, max_length=120)
    cancellation_policy: str = Field(default="flexible")


class SetPricesRequest(BaseModel):
    rate_plan_id: str
    date_from: dt.date
    date_to: dt.date
    price: float = Field(ge=0)
    min_stay: int = Field(default=1, ge=1)


class RatePlanPatch(BaseModel):
    """Change the commission share of a rate plan.

    ``null`` clears the override so the unit type falls back to the platform
    default. Only the commission is patchable: the plan's name and policy are
    set at creation, and its prices have their own endpoint.
    """

    commission_rate: float | None = Field(default=None, ge=0, le=1)


@router.post("/partner/rate-plans", status_code=status.HTTP_201_CREATED)
async def create_rate_plan(
    data: RatePlanCreate,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> dict:
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
            return await service.create_rate_plan(
                conn, data.unit_type_id, data.name, data.cancellation_policy
            )
        except service.ActivePlanExists as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="unit type already has an active rate plan",
            ) from exc
    finally:
        await get_pool().release(conn)


@router.get("/partner/rate-plans")
async def list_rate_plans(
    unit_type_id: str = Query(...),
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> list[dict]:
    conn = await get_pool().acquire()
    try:
        return await service.list_rate_plans(conn, unit_type_id)
    finally:
        await get_pool().release(conn)


@router.patch("/partner/rate-plans/{rate_plan_id}")
async def patch_rate_plan(
    rate_plan_id: str,
    data: RatePlanPatch,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> dict:
    """Set the commission share of a rate plan.

    A booking snapshots the rate at hold time, so this only affects stays
    booked after the change.
    """
    conn = await get_pool().acquire()
    try:
        owned = await conn.fetchval(
            "SELECT 1 FROM rate_plan rp JOIN unit_type ut ON ut.id = rp.unit_type_id "
            "JOIN property p ON p.id = ut.property_id "
            "WHERE rp.id = $1 AND p.partner_id = $2",
            rate_plan_id,
            token.sub,
        )
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rate plan not found")
        try:
            return await service.update_rate_plan(conn, rate_plan_id, data.commission_rate)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="rate plan not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
    finally:
        await get_pool().release(conn)


@router.post("/partner/prices")
async def set_prices(
    data: SetPricesRequest,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> dict:
    """Set the price for a date range (whole season in one call)."""
    conn = await get_pool().acquire()
    try:
        owned = await conn.fetchval(
            "SELECT 1 FROM rate_plan rp JOIN unit_type ut ON ut.id = rp.unit_type_id "
            "JOIN property p ON p.id = ut.property_id "
            "WHERE rp.id = $1 AND p.partner_id = $2",
            data.rate_plan_id,
            token.sub,
        )
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rate plan not found")
        try:
            await service.set_prices(
                conn, data.rate_plan_id, data.date_from, data.date_to, data.price, data.min_stay
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return {"status": "ok"}
    finally:
        await get_pool().release(conn)


@router.get("/partner/calendar")
async def partner_calendar(
    date_from: dt.date = Query(...),
    date_to: dt.date = Query(...),
    property_id: str | None = Query(default=None),
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """The partner chess-board grid: unit types x dates."""
    conn = await get_pool().acquire()
    try:
        try:
            return await calendar.get_calendar(conn, token.sub, date_from, date_to, property_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
    finally:
        await get_pool().release(conn)
