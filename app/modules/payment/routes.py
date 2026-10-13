from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.db.pool import get_pool
from app.modules.booking.access import require_token
from app.modules.payment import service

router = APIRouter(prefix="/v1/bookings", tags=["payment"])
payments_router = APIRouter(prefix="/v1/payments", tags=["payment"])


@router.post("/{booking_id}/pay", dependencies=[Depends(require_token)])
async def pay_booking(booking_id: str, response: Response) -> dict:
    """Pay for a booking.

    An instant provider (the stub) charges and confirms in this call. An acquirer takes the
    payment on its own page: the answer is 202 with `payment_url` to send the guest to, and the
    booking is confirmed when the provider reports the result. A second call while that payment
    is open returns the same address.
    """
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
        if service.pays_on_provider_page():
            result = await service.start_payment(booking_id)
            if result["status"] == "pending":
                response.status_code = status.HTTP_202_ACCEPTED
            return result
        return await service.pay_and_confirm(booking_id)
    except service.BookingNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="booking not found"
        ) from exc
    except service.PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{booking_id}/payment/sync", dependencies=[Depends(require_token)])
async def sync_booking_payment(booking_id: str) -> dict:
    """Ask the provider where the booking's payment stands.

    The checkout calls this when the guest comes back from the payment page, so it does not wait
    for the provider's notification. `state` is confirmed, pending, failed, returned, stuck or none.
    """
    try:
        return await service.sync_payment(booking_id)
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


@payments_router.post("/notifications/{provider}")
async def payment_notification(provider: str, request: Request) -> dict:
    """Where the acquirer reports a payment. Public: the provider's signature is the credential.

    200 once the notification was read, also for a payment this shop does not know, so the
    provider stops repeating it; 503 while the booking is busy, which makes it try again.
    """
    try:
        await service.receive_notification(provider, await request.body(), request.headers)
    except service.NotificationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except service.PaymentBusy as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="payment is being processed; try again",
            headers={"Retry-After": "1"},
        ) from exc
    return {"ok": True}
