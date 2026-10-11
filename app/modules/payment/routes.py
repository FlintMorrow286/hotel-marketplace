from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.db.pool import get_pool
from app.modules.booking.access import require_token
from app.modules.payment import service

router = APIRouter(prefix="/v1/bookings", tags=["payment"])


@router.post("/{booking_id}/pay", dependencies=[Depends(require_token)])
async def pay_booking(booking_id: str) -> dict:
    """Charge the guest (stub in Phase 1) and confirm the booking instantly."""
    pool = get_pool()
    conn = await pool.acquire()
    try:
        # Refresh the hold so an expired one is rejected before charging.
        await conn.execute(
            """
            UPDATE booking
            SET hold_expires_at = now() + interval '15 minutes'
            WHERE id = $1 AND status = 'hold' AND hold_expires_at > now()
            """,
            booking_id,
        )
    finally:
        await pool.release(conn)

    try:
        return await service.pay_and_confirm(booking_id)
    except service.BookingNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="booking not found"
        ) from exc
    except service.PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{booking_id}/refund", dependencies=[Depends(require_token)])
async def refund_booking(booking_id: str) -> dict:
    """Refund a confirmed booking and return inventory to the pool."""
    try:
        return await service.refund_booking(booking_id)
    except service.BookingNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="booking not found"
        ) from exc
    except service.PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
