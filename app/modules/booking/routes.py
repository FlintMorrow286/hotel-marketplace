from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.db.pool import get_pool
from app.db.tx import serializable
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.booking import service
from app.modules.booking.schemas import HoldRequest, PartnerBookingLineOut, PartnerBookingOut

router = APIRouter(prefix="/v1/bookings", tags=["booking"])


def _line_dates(conn, booking_id: str):
    return conn.fetch(
        "SELECT date, price::float8 FROM booking_line WHERE booking_id = $1 ORDER BY date",
        booking_id,
    )


@router.get("/partner/list", response_model=list[PartnerBookingOut])
async def list_partner_bookings(
    booking_status: Annotated[str | None, Query(alias="status")] = None,
    token: TokenData = Depends(require_scope("partner")),
) -> list[PartnerBookingOut]:
    """Every booking on the partner's own inventory, newest first.

    The partner sees who is arriving and how the booking reached them — guest
    contacts, the booking channel, and the commission the platform takes. A
    guest looking up the same code through /by-code/{code} sees none of this.
    """
    pool = get_pool()
    conn = await pool.acquire()
    try:
        rows = await service.list_partner_bookings(conn, token.sub, booking_status)
        out: list[PartnerBookingOut] = []
        for r in rows:
            lines = await _line_dates(conn, r["id"])
            out.append(
                PartnerBookingOut(
                    id=r["id"],
                    code=r["code"],
                    status=r["status"],
                    total_amount=r["total_amount"],
                    commission_rate=r["commission_rate"],
                    commission_amount=r["commission_amt"],
                    origin=r["origin"],
                    source_channel=r["source_channel"],
                    guest_name=r["guest_name"],
                    guest_email=r["guest_email"],
                    guest_phone=r["guest_phone"],
                    property_id=r["property_id"],
                    property_name=r["property_name"],
                    unit_type_id=r["unit_type_id"],
                    unit_type_name=r["unit_type_name"],
                    checkin_date=r["checkin_date"],
                    checkout_date=r["checkout_date"],
                    created_at=r["created_at"],
                    lines=[
                        PartnerBookingLineOut(date=ln["date"], price=ln["price"]) for ln in lines
                    ],
                )
            )
        return out
    finally:
        await pool.release(conn)


@router.post("/hold", status_code=status.HTTP_201_CREATED)
async def create_hold(
    data: HoldRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict:
    """Create a time-limited hold on inventory.

    Returns 409 when the dates are not available (including the stop-sell case)
    or when the idempotency key collides with a different payload.
    """
    pool = get_pool()
    conn = await pool.acquire()
    try:

        async def _hold():
            row = await service.create_hold(
                conn,
                unit_type_id=data.unit_type_id,
                checkin=data.checkin,
                checkout=data.checkout,
                guest_name=data.guest.name,
                guest_email=str(data.guest.email),
                guest_phone=data.guest.phone,
                idempotency_key=idempotency_key,
            )
            return row, await _line_dates(conn, row["id"])

        try:
            row, lines = await serializable(conn, _hold)
        except service.NotAvailable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"dates not available: {exc}",
            ) from exc
        except service.Conflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"idempotency conflict: {exc}",
            ) from exc

        expires = row["hold_expires_at"]
        return {
            "id": row["id"],
            "code": row["code"],
            "status": row["status"],
            "total_amount": float(row["total_amount"]),
            # A replayed key can name a booking that is already confirmed.
            "hold_expires_at": expires.isoformat() if expires else None,
            "checkin_date": data.checkin.isoformat(),
            "checkout_date": data.checkout.isoformat(),
            "lines": [{"date": ln["date"].isoformat(), "price": ln["price"]} for ln in lines],
        }
    finally:
        await pool.release(conn)


@router.get("/by-code/{code}")
async def get_booking_by_code(code: str) -> dict:
    """Guest-facing lookup: the traveller knows the BK-XXXXXX code, never the id.

    The code is unguessable (6 chars from a 32-char alphabet) and answers only
    public stay details — no guest contacts, no payment state.
    """
    conn = await get_pool().acquire()
    try:
        row = await conn.fetchrow(
            """
            SELECT id::text, code, status, total_amount::float8, hold_expires_at,
                   checkin_date, checkout_date, unit_type_id::text
            FROM booking WHERE code = UPPER($1)
            """,
            code,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="booking not found")
        lines = await _line_dates(conn, row["id"])
        return {
            "id": row["id"],
            "code": row["code"],
            "status": row["status"],
            "total_amount": row["total_amount"],
            "hold_expires_at": row["hold_expires_at"].isoformat()
            if row["hold_expires_at"]
            else None,
            "checkin_date": row["checkin_date"].isoformat(),
            "checkout_date": row["checkout_date"].isoformat(),
            "lines": [{"date": ln["date"].isoformat(), "price": ln["price"]} for ln in lines],
        }
    finally:
        await get_pool().release(conn)


@router.get("/{booking_id}")
async def get_booking(booking_id: str) -> dict:
    conn = await get_pool().acquire()
    try:
        row = await conn.fetchrow(
            """
            SELECT id::text, code, status, total_amount::float8, hold_expires_at,
                   checkin_date, checkout_date, unit_type_id::text
            FROM booking WHERE id = $1
            """,
            booking_id,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="booking not found")
        lines = await _line_dates(conn, booking_id)
        return {
            "id": row["id"],
            "code": row["code"],
            "status": row["status"],
            "total_amount": row["total_amount"],
            "hold_expires_at": row["hold_expires_at"].isoformat()
            if row["hold_expires_at"]
            else None,
            "checkin_date": row["checkin_date"].isoformat(),
            "checkout_date": row["checkout_date"].isoformat(),
            "lines": [{"date": ln["date"].isoformat(), "price": ln["price"]} for ln in lines],
        }
    finally:
        await get_pool().release(conn)


@router.post("/{booking_id}/cancel")
async def cancel_booking(booking_id: str) -> dict:
    """Cancel a hold and return inventory to the pool."""
    pool = get_pool()
    conn = await pool.acquire()
    try:

        async def _cancel():
            await service.release_hold(conn, booking_id)
            return await conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)

        return {"id": booking_id, "status": await serializable(conn, _cancel)}
    finally:
        await pool.release(conn)
