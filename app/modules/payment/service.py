"""Booking payment + confirmation.

Instant book flow: a successful payment converts the hold into a confirmed
booking in the same transaction — the held inventory becomes "sold" and the
booking becomes visible to the partner.

All inventory row locks are taken again here (FOR UPDATE, ordered) so that
converting a hold can never race with a reaper or another guest.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import asyncpg

from app.config import legal
from app.db.pool import get_pool
from app.db.tx import serializable, try_lock, unlock
from app.modules.booking.service import BookingError, release_hold
from app.modules.payment.provider import PaymentProvider, PaymentResult, get_payment_provider
from app.utils.logger import get_logger
from app.utils.tz import today_at as _today_at

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

        try:
            # The money has moved: write that down first, so a charge never exists
            # without a row that says so, then confirm.
            await _record_payment(
                conn, booking_id, provider.name, amount, "succeeded", result.external_id
            )
            out, row = await serializable(conn, _confirm)
        except Exception as exc:
            # The guest has paid but the booking is not confirmed: the hold was reaped
            # during the provider call, or the transaction kept conflicting.
            log.error("confirmation-failed-after-charge", booking_id=booking_id, error=repr(exc))
            returned = await _give_back(conn, provider, booking_id, amount)
            if returned:
                # A returned charge is final. Closing the hold means a retry starts from
                # a new booking and a new charge, never from a provider that may answer
                # the old, idempotent one with its earlier success.
                await _close_returned_hold(conn, booking_id)
            reason = str(exc) if isinstance(exc, PaymentError) else "booking could not be confirmed"
            tail = (
                "your payment was returned"
                if returned
                else "your payment could not be returned automatically; support will refund it"
            )
            raise PaymentError(f"{reason}; {tail}") from exc
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


async def _refund_on_record(conn: asyncpg.Connection, booking_id: str) -> asyncpg.Record | None:
    """A provider refund made after the booking's last successful charge, if any.

    A refund that predates the last charge belongs to an earlier, returned
    payment (see `_give_back`), not to this one.
    """
    return await conn.fetchrow(
        """
        SELECT r.amount::float8 AS amount
        FROM payment r
        WHERE r.booking_id = $1
          AND r.status = 'refunded'
          AND r.created_at >= coalesce(
                (SELECT max(created_at) FROM payment
                  WHERE booking_id = $1 AND status = 'succeeded'),
                '-infinity'::timestamptz)
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        booking_id,
    )


async def refund_booking(booking_id: str, conn: asyncpg.Connection | None = None) -> dict:
    """Refund a confirmed booking: sold -> free, status -> refunded.

    Cancellation rule: free if cancelled before 24:00 of the day before
    check-in, by the calendar of the property. Later, the penalty applies (see
    app.config.legal): the partner keeps that share and the platform earns its
    commission on it. The provider is paid back once: the refund is written down
    before the booking changes, so a call that died in between is finished by
    the next one instead of refunding again.
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
            SELECT b.id::text, b.code, b.status, b.total_amount::float8, b.checkin_date,
                   b.checkout_date, b.unit_type_id::text, b.property_id::text, p.timezone
            FROM booking b
            JOIN property p ON p.id = b.property_id
            WHERE b.id = $1
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
        earlier = await _refund_on_record(conn, booking_id)
        if earlier is not None:
            amount = earlier["amount"]
            is_free = amount >= booking["total_amount"]
        else:
            today = _today_at(booking["timezone"])
            # A stay that is over is not the platform's to refund: the guest has had it. (A
            # refund the provider already made is finished above, whatever the date.)
            if booking["checkout_date"] <= today:
                raise PaymentError("the stay is already over; ask the property for a refund")
            # checkin_date is a date; the free window ends at that date boundary.
            is_free = today <= deadline
            # A late cancellation keeps the penalty share; the rest goes back to
            # the guest. The rate is a legal constant, not a technical default.
            if is_free:
                amount = booking["total_amount"]
            else:
                amount = round(booking["total_amount"] * (1 - legal.CANCELLATION_PENALTY_RATE), 2)
            result = await provider.refund(booking_id, amount)
            if result.status != "refunded":
                raise PaymentError("refund failed")
            await _record_payment_reliably(
                conn, booking_id, provider.name, amount, "refunded", result.external_id
            )
        kept = round(booking["total_amount"] - amount, 2)

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
            # The commission follows the money the partner keeps: all of it is
            # void on a free cancellation, a share of it on a late one.
            row = await conn.fetchrow(
                """
                UPDATE booking
                SET status = 'refunded',
                    cancelled_at = now(),
                    kept_amount = $2::numeric,
                    commission_amt = CASE
                        WHEN $3::boolean AND $2::numeric > 0
                        THEN round(commission_amt * $2::numeric / NULLIF(total_amount, 0), 2)
                        ELSE commission_amt END,
                    commission_status = CASE
                        WHEN $3::boolean AND $2::numeric > 0 THEN 'accrued' ELSE 'void' END
                WHERE id = $1 AND status IN ('confirmed', 'paid')
                RETURNING id::text, code, status, total_amount::float8,
                          paid_at, checkin_date, checkout_date
                """,
                booking_id,
                kept,
                legal.COMMISSION_ON_PENALTY,
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


async def _give_back(
    conn: asyncpg.Connection, provider: PaymentProvider, booking_id: str, amount: float
) -> bool:
    """Return a charge whose booking could not be confirmed. Never raises.

    False means the money is still with us: the `succeeded` row written when the
    charge went through has no `refunded` one after it, which is how it is found.
    """
    try:
        result = await provider.refund(booking_id, amount)
    except Exception as exc:
        log.error("charge-not-returned", booking_id=booking_id, amount=amount, error=str(exc))
        return False
    if result.status != "refunded":
        log.error(
            "charge-not-returned", booking_id=booking_id, amount=amount, provider_said=result.status
        )
        return False
    await _record_payment_reliably(
        conn, booking_id, provider.name, amount, "refunded", result.external_id
    )
    log.warning("charge-returned", booking_id=booking_id, amount=amount)
    return True


async def _close_returned_hold(conn: asyncpg.Connection, booking_id: str) -> None:
    """Release the hold of a booking whose charge was returned. Never raises."""
    try:
        await serializable(conn, lambda: release_hold(conn, booking_id))
    except Exception as exc:
        log.error("hold-not-released-after-return", booking_id=booking_id, error=str(exc))


async def _record_payment_reliably(
    conn: asyncpg.Connection,
    booking_id: str,
    provider: str,
    amount: float,
    status: str,
    external_id: str | None,
) -> None:
    """Write down what the provider did, trying a few times, and never raise.

    The provider has already acted: failing here must not stop the caller from
    finishing its own work (a refund that is not recorded must still close the
    booking, or a retry refunds the provider a second time). A row that is still
    missing after the retries is the ledger gap an error-level log points at.
    """
    for attempt in range(1, 4):
        try:
            await _record_payment(conn, booking_id, provider, amount, status, external_id)
            return
        except Exception as exc:
            if attempt == 3:
                log.error(
                    "payment-unrecorded",
                    booking_id=booking_id,
                    status=status,
                    amount=amount,
                    external_id=external_id,
                    error=str(exc),
                )
                return
            await asyncio.sleep(0.05 * attempt)


async def _record_payment(
    conn: asyncpg.Connection,
    booking_id: str,
    provider: str,
    amount: float,
    status: str,
    external_id: str | None,
) -> None:
    """Write one provider event down, once.

    An idempotent provider answers a repeated pay or refund with the same external id; that
    is one event, not two (a retry after a lost response used to leave two `succeeded` rows
    for one charge, which a ledger sum would count twice). Events without an id, such as
    failed attempts, are each their own row.
    """
    await conn.execute(
        """
        INSERT INTO payment (booking_id, provider, amount, status, external_id)
        SELECT $1::uuid, $2::text, $3::numeric, $4::text, $5::text
        WHERE $5::text IS NULL
           OR NOT EXISTS (
                SELECT 1 FROM payment
                WHERE booking_id = $1::uuid AND status = $4::text AND external_id = $5::text)
        """,
        booking_id,
        provider,
        amount,
        status,
        external_id,
    )


_ = dt
