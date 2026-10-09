from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.admin import report, service
from app.modules.admin.schemas import (
    AdminLoginRequest,
    AdminPropertyOut,
    ModerationRequest,
)
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.utils.ratelimit_http import SUBJECT_FACTOR, enforce

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/login")
async def admin_login(data: AdminLoginRequest, request: Request) -> dict:
    """Staff login. Issues an admin-scoped token (not a partner token)."""
    await enforce(request, "admin-login", settings.login_limit_per_min)
    await enforce(
        request,
        "admin-login-account",
        settings.login_limit_per_min * SUBJECT_FACTOR,
        subject=data.email,
    )
    conn = await get_pool().acquire()
    try:
        token = await service.login_admin(conn, data.email, data.password)
        return {"access_token": token, "token_type": "bearer", "scope": "admin"}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        ) from exc
    finally:
        await get_pool().release(conn)


@router.get("/reports/commission")
async def commission_report(
    date_from: dt.date | None = Query(default=None),
    date_to: dt.date | None = Query(default=None),
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Commission accrued per partner. Admin scope only."""
    conn = await get_pool().acquire()
    try:
        return await report.commission_report(conn, date_from, date_to)
    finally:
        await get_pool().release(conn)


@router.get("/properties", response_model=list[AdminPropertyOut])
async def list_properties(
    status_filter: str | None = Query(default=None, alias="status"),
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> list[AdminPropertyOut]:
    """Moderation queue: every property, newest submissions first."""
    conn = await get_pool().acquire()
    try:
        rows = await service.list_properties_for_admin(conn, status_filter)
        return [AdminPropertyOut(**r) for r in rows]
    finally:
        await get_pool().release(conn)


@router.patch("/properties/{property_id}/status", response_model=AdminPropertyOut)
async def moderate_property(
    property_id: str,
    data: ModerationRequest,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> AdminPropertyOut:
    """Approve / reject / block a property. Illegal transitions answer 409."""
    conn = await get_pool().acquire()
    try:
        try:
            row = await service.set_property_status(conn, property_id, data.status)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="property not found")
        return AdminPropertyOut(**row)
    finally:
        await get_pool().release(conn)
