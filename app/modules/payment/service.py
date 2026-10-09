"""Booking payment + confirmation.

Instant book flow: a successful payment converts the hold into a confirmed
booking in the same transaction — the held inventory becomes "sold" and the
booking becomes visible to the partner.

All inventory row locks are taken again here (FOR UPDATE, ordered) so that
converting a hold can never race with a reaper or another guest.
"""

from __future__ import annotations

import datetime as dt

import asyncpg

from app.config import legal
from app.db.pool import get_pool
from app.db.tx import serializable, try_lock, unlock
from app.modules.booking.service import BookingError
from app.modules.payment.provider import PaymentResult, get_payment_provider
from app.utils.logger import get_logger

log = get_logger(__name__)


class PaymentError(Exception):
    pass


class BookingNotFound(BookingError):
    pass


async def _row_to_out(row: asyncpg.Record, lines: list[asyncpg.Record]) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "status": row["status"],
        "total_amount": float(row["total_amount"]),
        "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None,
        "checkin_date": row["checkin_date"].isoformat(),
        "checkout_date": row["checkout_date"].isoformat(),
        "lines": [{"date": ln["date"].isoformat(), "price": float(ln["price"])} for ln in lines],
    }


async def pay_and_confirm(booking_id: str, conn: asyncpg.Connection | None = None) -> dict:
    """Charge the guest and confirm the booking.

    The provider call happens OUTSIDE the transaction (it may be slow), but the
    inventory transition is transactional: lock rows, move hold -> sold, flip
    status. A payment that succeeds while the booking was already cancelled is
    refused rather than double-confirming.
    """
    provider = get_payment_provider()
    pool = None
    if conn is None:
        pool = get_pool()
        conn = await pool.acquire()
    lock_held = False
    try:
        # One payment per booking at a time: a double click or a client retry
        # must not reach the provider twice, or the guest is charged twice.
        lock_held = await try_lock(conn, "pay", booking_id)
        if not lock_held:
            raise PaymentError("a payment for this booking is already in progress")

        # --- read current state (no lock yet: provider call may take time)
        booking = await conn.fetchrow(
            """
            SELECT id::text, code, status, total_amount::float8, checkin_date, checkout_date,
                   unit_type_id::text, hold_expires_at
            FROM booking WHERE id = $1
            """,
            booking_id,
        )
        if booking is None:
            raise BookingNotFound("booking not found")
        if booking["status"] != "hold":
            raise PaymentError(f"booking is not holdable (status={booking['status']})")
        if booking["hold_expires_at"] is not None and booking["hold_expires_at"] < dt.datetime.now(
            dt.UTC
        ):
            raise PaymentError("hold expired; please book again")

        amount = booking["total_amount"]

        # --- call the provider (stub in Phase 1)
        result: PaymentResult = await provider.pay(booking_id, amount)
        if result.status != "succeeded":
            log.warning("payment-failed", booking_id=booking_id, provider=provider.name)
            await _record_payment(
                conn, booking_id, provider.name, amount, "failed", result.external_id
            )
            raise PaymentError("payment failed")

        # --- confirm transactionally (re-run if Postgres aborts it for a conflict)
        async def _confirm() -> tuple[dict, asyncpg.Record]:
            locked = await conn.fetch(
                """
                SELECT date, hold, sold FROM inventory_day
                WHERE unit_type_id = $1 AND date >= $2 AND date < $3
                ORDER BY date
                FOR UPDATE
                """,
                booking["unit_type_id"],
                booking["checkin_date"],
                booking["checkout_date"],
            )
            for row in locked:
                if row["hold"] < 1:
                    raise PaymentError(
                        f"hold vanished on {row['date']}; the booking is inconsistent"
                    )

            await conn.executemany(
                """
                UPDATE inventory_day
                SET hold = GREATEST(hold - 1, 0), sold = sold + 1
                WHERE unit_type_id = $1 AND date = $2
                """,
                [(booking["unit_type_id"], r["date"]) for r in locked],
            )

            row = await conn.fetchrow(
                """
                UPDATE booking
                SET status = 'confirmed',
                    paid_at = now(),
                    hold_expires_at = NULL,
                    commission_status = 'accrued'
                WHERE id = $1 AND status = 'hold'
                RETURNING id::text, code, status, total_amount::float8,
                          paid_at, checkin_date, checkout_date,
                          commission_amt::float8, property_id::text
                """,
                booking_id,
            )
            if row is None:
                raise PaymentError("booking changed state during payment")

            await _record_payment(
                conn, booking_id, provider.name, amount, "succeeded", result.external_id
            )

            lines = await conn.fetch(
                "SELECT date, price::float8 FROM booking_line WHERE booking_id = $1 ORDER BY date",
                booking_id,
            )
            # Tell the channels. Same transaction as the confirmation, so the
            # outbox row can never exist without the booking it describes.
            from app.modules.outbox import service as outbox_service

            await outbox_service.emit(
                conn,
                aggregate="booking",
                aggregate_id=booking_id,
                event_type=outbox_service.BOOKING_CONFIRMED,
                payload={
                    "booking_id": booking_id,
                    "code": row["code"],
                    "unit_type_id": booking["unit_type_id"],
                    "checkin_date": row["checkin_date"].isoformat(),
                    "checkout_date": row["checkout_date"].isoformat(),
                    "total_amount": float(row["total_amount"]),
                    "commission_amt": float(row["commission_amt"]),
                },
                property_id=row["property_id"],
            )

            return await _row_to_out(row, lines), row

        out, row = await serializable(conn, _confirm)
        log.info("booking-confirmed", booking_id=booking_id, code=row["code"])

        # After the commit: a confirmation that rolls back or re-runs must not notify.
        from app.modules.notification import service as notification_service

        try:
            partner_id = await conn.fetchval(
                "SELECT partner_id::text FROM property WHERE id = $1", row["property_id"]
            )
            guest = await conn.fetchrow(
                "SELECT guest_name, guest_email FROM booking WHERE id = $1", booking_id
            )
            payload = dict(row)
            payload["partner_id"] = partner_id
            payload["guest_name"] = guest["guest_name"]
            payload["guest_email"] = guest["guest_email"]
            await notification_service.notify_booking_confirmed(conn, payload)
        except Exception as exc:
            log.warning("notify-failed", booking_id=booking_id, error=str(exc))

        return out
    finally:
        if lock_held:
            await unlock(conn, "pay", booking_id)
        if pool is not None:
            await pool.release(conn)


def _hold_still_valid(_conn: asyncpg.Connection, _booking: asyncpg.Record) -> bool:
    """Expiry is enforced by the route + DB; kept for future pre-auth checks."""
    return True


async def refund_booking(booking_id: str, conn: asyncpg.Connection | None = None) -> dict:
    """Refund a confirmed booking: sold -> free, status -> refunded.

    Cancellation rule: free if cancelled before 24:00 of the day before
    check-in. Later, the penalty applies (see app.config.legal). This function
    records the deadline it used so reports can be verified.
    """
    provider = get_payment_provider()
    pool = None
    if conn is None:
        pool = get_pool()
        conn = await pool.acquire()
    lock_held = False
    try:
        # One refund per booking at a time: the provider call is not repeatable.
        lock_held = await try_lock(conn, "refund", booking_id)
        if not lock_held:
            raise PaymentError("a refund for this booking is already in progress")

        booking = await conn.fetchrow(
            """
            SELECT id::text, code, status, total_amount::float8, checkin_date, checkout_date,
                   unit_type_id::text, property_id::text
            FROM booking WHERE id = $1
            """,
            booking_id,
        )
        if booking is None:
            raise BookingNotFound("booking not found")
        if booking["status"] not in ("confirmed", "paid"):
            raise PaymentError(f"cannot refund booking with status={booking['status']}")

        deadline = booking["checkin_date"] - dt.timedelta(
            hours=legal.CANCELLATION_FREE_BEFORE_HOURS
        )
        # checkin_date is a date; the free window ends at that date boundary.
        is_free = dt.date.today() <= deadline

        # A late cancellation keeps the penalty share; the rest goes back to
        # the guest. The rate is a legal constant, not a technical default.
        if is_free:
            amount = booking["total_amount"]
        else:
            amount = round(booking["total_amount"] * (1 - legal.CANCELLATION_PENALTY_RATE), 2)
        result = await provider.refund(booking_id, amount)
        if result.status != "refunded":
            raise PaymentError("refund failed")

        async def _refund() -> dict:
            locked = await conn.fetch(
                """
                SELECT date, sold FROM inventory_day
                WHERE unit_type_id = $1 AND date >= $2 AND date < $3
                ORDER BY date
                FOR UPDATE
                """,
                booking["unit_type_id"],
                booking["checkin_date"],
                booking["checkout_date"],
            )
            await conn.executemany(
                """
                UPDATE inventory_day
                SET sold = GREATEST(sold - 1, 0)
                WHERE unit_type_id = $1 AND date = $2
                """,
                [(booking["unit_type_id"], r["date"]) for r in locked],
            )
            row = await conn.fetchrow(
                """
                UPDATE booking
                SET status = 'refunded', cancelled_at = now(), commission_status = 'void'
                WHERE id = $1 AND status IN ('confirmed', 'paid')
                RETURNING id::text, code, status, total_amount::float8,
                          paid_at, checkin_date, checkout_date
                """,
                booking_id,
            )
            if row is None:
                raise PaymentError("booking changed state during refund")

            from app.modules.outbox import service as outbox_service

            await outbox_service.emit(
                conn,
                aggregate="booking",
                aggregate_id=booking_id,
                event_type=outbox_service.BOOKING_CANCELLED,
                payload={
                    "booking_id": booking_id,
                    "code": row["code"],
                    "unit_type_id": booking["unit_type_id"],
                    "checkin_date": row["checkin_date"].isoformat(),
                    "checkout_date": row["checkout_date"].isoformat(),
                    "total_amount": float(row["total_amount"]),
                    "free_cancelled": is_free,
                },
                property_id=booking["property_id"],
            )
            await _record_payment(
                conn, booking_id, provider.name, amount, "refunded", result.external_id
            )
            lines = await conn.fetch(
                "SELECT date, price::float8 FROM booking_line WHERE booking_id = $1 ORDER BY date",
                booking_id,
            )
            out = await _row_to_out(row, lines)
            out["free_cancelled"] = is_free
            out["refunded_amount"] = amount
            return out

        out = await serializable(conn, _refund)
        log.info(
            "booking-refunded",
            booking_id=booking_id,
            code=out["code"],
            free_cancelled=is_free,
            deadline=deadline.isoformat(),
            refunded_amount=amount,
        )
        return out
    finally:
        if lock_held:
            await unlock(conn, "refund", booking_id)
        if pool is not None:
            await pool.release(conn)


async def _record_payment(
    conn: asyncpg.Connection,
    booking_id: str,
    provider: str,
    amount: float,
    status: str,
    external_id: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO payment (booking_id, provider, amount, status, external_id)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT DO NOTHING
        """,
        booking_id,
        provider,
        amount,
        status,
        external_id,
    )


_ = dt
