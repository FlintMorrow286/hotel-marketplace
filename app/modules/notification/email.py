"""Outgoing e-mail: a queue written with the booking change, and the sender that delivers it.

A letter is a row in `email_message`, inserted in the same transaction as the change it reports
(`enqueue_booking_mail`, called by the confirmation and the refund), so a confirmed booking always
has its letters waiting and a mail server that is down only delays them. The sender (`send_due`,
run by app/jobs/mail.py) claims due rows, writes the text from the booking as it is then, hands it
to a transport and records the outcome. It is at-least-once: a process that dies between the
server's "accepted" and the row's "sent" sends that letter again, under the same Message-ID.

Transports: `log` renders the letter, keeps it in the row and delivers nothing (development, or no
mail account yet); `smtp` delivers it. A failure of any of this never reaches a booking.
"""

from __future__ import annotations

import asyncio
import email.utils
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal, Protocol

import asyncpg

from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.notification import templates
from app.utils.logger import get_logger

log = get_logger(__name__)

# Letters claimed per sweep. A claim holds them for as long as the whole batch can take (every
# letter is cut off after `send_deadline`), so another process never takes a letter that is still
# being sent; a sweep that dies leaves its letters alone for that long, then they are tried again.
BATCH = 10
# Seconds to wait after the 1st, 2nd, ... failed attempt; the last value repeats.
BACKOFF_SEC = (30, 120, 600, 1800, 3600, 10800, 21600)

_CONFIRMATIONS = (templates.KIND_GUEST_CONFIRMED, templates.KIND_PARTNER_CONFIRMED)

_BOOKING_EVENTS = {
    "confirmed": (templates.KIND_PARTNER_CONFIRMED, templates.KIND_GUEST_CONFIRMED),
    "cancelled": (templates.KIND_PARTNER_CANCELLED, templates.KIND_GUEST_CANCELLED),
}


# ---------------------------------------------------------------- the queue


async def enqueue_booking_mail(
    conn: asyncpg.Connection,
    booking_id: str,
    event: Literal["confirmed", "cancelled"],
    *,
    origin: str,
) -> None:
    """Queue the letters a booking event owes. Call it inside the transaction of the change.

    The partner is told of every booking. The guest is told of a web booking only: a channel's
    guest is the channel's, and its own letters apply. The queue row is the only write, under a
    savepoint, so a failure here loses the letters (and says so at error level) but never the
    booking; a serialization conflict is not ours to swallow, the caller's retry has to see it.
    """
    partner_kind, guest_kind = _BOOKING_EVENTS[event]
    kinds = [partner_kind, *([guest_kind] if origin == "web" else [])]
    in_transaction = conn.is_in_transaction()
    try:
        async with conn.transaction():
            for kind in kinds:
                await conn.execute(
                    """
                    INSERT INTO email_message (dedupe_key, kind, booking_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    """,
                    f"{kind}:{booking_id}",
                    kind,
                    booking_id,
                )
    except (asyncpg.SerializationError, asyncpg.DeadlockDetectedError):
        if in_transaction:
            raise
        log.error(
            "email-enqueue-failed", booking_id=booking_id, booking_event=event, error="conflict"
        )
    except Exception as exc:
        log.error(
            "email-enqueue-failed", booking_id=booking_id, booking_event=event, error=str(exc)
        )


def send_deadline() -> float:
    """The longest one letter may take: smtplib's timeout covers each step, not the whole."""
    return 3 * settings.smtp_timeout_sec


def lease_sec(limit: int) -> float:
    return max(120.0, limit * send_deadline() + 60.0)


async def claim_due(conn: asyncpg.Connection, limit: int = BATCH) -> list[asyncpg.Record]:
    """Take the letters that are due, counting an attempt and holding them for the batch's lease."""
    return await conn.fetch(
        """
        UPDATE email_message m
        SET attempts = m.attempts + 1,
            next_attempt_at = now() + make_interval(secs => $2)
        WHERE m.id IN (
            SELECT id FROM email_message
            WHERE status = 'queued' AND next_attempt_at <= now()
            ORDER BY next_attempt_at, created_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED)
        RETURNING m.id::text AS id, m.kind, m.booking_id::text AS booking_id, m.attempts
        """,
        limit,
        lease_sec(limit),
    )


async def purge_old(conn: asyncpg.Connection) -> int:
    """Delete what has outlived its retention: a kept letter holds a recipient's address."""
    return await conn.fetchval(
        """
        WITH gone AS (
            DELETE FROM email_message
            WHERE (status = 'sent' AND sent_at < now() - make_interval(days => $1))
               OR (status = 'failed' AND created_at < now() - make_interval(days => $2))
            RETURNING 1)
        SELECT count(*) FROM gone
        """,
        settings.mail_sent_retention_days,
        settings.mail_failed_retention_days,
    )


# ---------------------------------------------------------------- transports


@dataclass(frozen=True)
class Outgoing:
    to: str
    subject: str
    body: str
    message_id: str


class TemporaryMailError(Exception):
    """Not delivered, worth trying again later (server busy, down, or misconfigured)."""


class PermanentMailError(Exception):
    """Not delivered, and trying again cannot change that (no such mailbox, refused outright)."""


class MailTransport(Protocol):
    name: str

    async def send(self, message: Outgoing) -> None: ...


class LogTransport:
    """Delivers nothing. The letter stays in its row, which is what a developer reads."""

    name = "log"

    async def send(self, message: Outgoing) -> None:
        log.info("email-logged", message_id=message.message_id, subject=message.subject)


_ADDRESS = re.compile(r"<?[^\s<>(),;:'\"]+@[^\s<>(),;:'\"]+>?")


def scrubbed(text: str) -> str:
    """The text with e-mail addresses blanked, for the log: error-level lines go to Sentry as events."""
    return _ADDRESS.sub("<address>", text)


def _from_domain() -> str:
    return email.utils.parseaddr(settings.mail_from)[1].rpartition("@")[2] or "localhost"


def _build(message: Outgoing) -> EmailMessage:
    mime = EmailMessage()
    mime["From"] = settings.mail_from
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime["Date"] = email.utils.formatdate()
    mime["Message-ID"] = message.message_id
    mime["Auto-Submitted"] = "auto-generated"
    if settings.mail_reply_to:
        mime["Reply-To"] = settings.mail_reply_to
    # Quoted-printable keeps the body 7-bit, which every relay accepts, Cyrillic or not.
    mime.set_content(message.body, charset="utf-8", cte="quoted-printable")
    return mime


def _smtp_send(mime: EmailMessage) -> None:
    host, port, timeout = settings.smtp_host, settings.smtp_port, settings.smtp_timeout_sec
    context = ssl.create_default_context()
    if settings.smtp_ssl:
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    with client:
        client.ehlo()
        if settings.smtp_starttls and not settings.smtp_ssl:
            client.starttls(context=context)
            client.ehlo()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(mime)


class SmtpTransport:
    """Delivers through SMTP (smtplib in a thread: the sender is the only caller, one at a time)."""

    name = "smtp"

    async def send(self, message: Outgoing) -> None:
        try:
            mime = _build(message)
        except ValueError as exc:  # a header the standard does not allow: this letter cannot go
            raise PermanentMailError(f"cannot build the message: {exc}") from exc
        try:
            await asyncio.to_thread(_smtp_send, mime)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = {code for code, _ in exc.recipients.values()}
            # 5xx: the mailbox is not there. 4xx (greylisting, quota): the server may take it later.
            if codes and all(500 <= code < 600 for code in codes):
                raise PermanentMailError(f"recipient refused: {exc.recipients}") from exc
            raise TemporaryMailError(f"recipient deferred: {exc.recipients}") from exc
        except smtplib.SMTPDataError as exc:
            if 500 <= exc.smtp_code < 600:
                raise PermanentMailError(
                    f"message rejected: {exc.smtp_code} {exc.smtp_error!r}"
                ) from exc
            raise TemporaryMailError(f"message deferred: {exc.smtp_code}") from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise PermanentMailError(f"the server cannot take this message: {exc}") from exc
        except smtplib.SMTPAuthenticationError as exc:
            # The account is wrong: an operator fixes it, and the queue then flows. Do not give up.
            log.error("email-smtp-auth-failed", code=exc.smtp_code)
            raise TemporaryMailError("SMTP authentication failed") from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            # Includes a refused sender, a closed connection and a failed TLS handshake.
            raise TemporaryMailError(f"{type(exc).__name__}: {exc}") from exc


def get_transport() -> MailTransport:
    if settings.mail_backend == "smtp":
        return SmtpTransport()
    if settings.mail_backend == "log":
        return LogTransport()
    raise ValueError(f"MAIL_BACKEND={settings.mail_backend!r} has no implementation")


# ---------------------------------------------------------------- the sender


async def load_mail_data(conn: asyncpg.Connection, booking_id: str) -> templates.BookingMail | None:
    row = await conn.fetchrow(
        """
        SELECT b.code, b.status, b.origin, b.guest_name, b.guest_email::text AS guest_email, b.guest_phone,
               b.checkin_date, b.checkout_date, b.total_amount::float8 AS total,
               b.commission_amt::float8 AS commission, b.kept_amount::float8 AS kept,
               b.docs_version,
               p.name AS property_name, p.city, p.checkin_time, p.checkout_time,
               ut.name AS unit_name, pa.name AS partner_name, pa.email::text AS partner_email,
               (SELECT r.amount::float8 FROM payment r
                 WHERE r.booking_id = b.id AND r.status = 'refunded'
                 ORDER BY r.created_at DESC LIMIT 1) AS refunded
        FROM booking b
        JOIN property p ON p.id = b.property_id
        JOIN unit_type ut ON ut.id = b.unit_type_id
        JOIN partner pa ON pa.id = p.partner_id
        WHERE b.id = $1
        """,
        booking_id,
    )
    if row is None:
        return None
    return templates.BookingMail(
        code=row["code"],
        status=row["status"],
        origin=row["origin"],
        guest_name=row["guest_name"],
        guest_email=row["guest_email"],
        guest_phone=row["guest_phone"],
        checkin=row["checkin_date"],
        checkout=row["checkout_date"],
        checkin_time=row["checkin_time"],
        checkout_time=row["checkout_time"],
        total=row["total"],
        commission=row["commission"],
        kept=row["kept"],
        refunded=row["refunded"],
        docs_version=row["docs_version"],
        property_name=row["property_name"],
        city=row["city"],
        unit_name=row["unit_name"],
        partner_name=row["partner_name"],
        partner_email=row["partner_email"],
    )


async def send_due(*, transport: MailTransport | None = None, limit: int = BATCH) -> int:
    """One sweep: claim the due letters and send each. Returns how many it took."""
    transport = transport or get_transport()
    pool = get_pool()
    conn = await pool.acquire()
    try:
        rows = await claim_due(conn, limit)
        for row in rows:
            try:
                await deliver(conn, transport, row)
            except Exception as exc:  # one letter must not stop the rest of the batch
                log.error("email-deliver-crashed", id=row["id"], error=repr(exc))
        return len(rows)
    finally:
        await pool.release(conn)


async def deliver(
    conn: asyncpg.Connection, transport: MailTransport, row: asyncpg.Record
) -> Literal["sent", "retry", "failed", "skipped"]:
    """Write one claimed letter, send it and record how it went."""
    data = await load_mail_data(conn, row["booking_id"])
    if data is None:
        await _mark_failed(conn, row, "the booking is gone")
        return "failed"
    if row["kind"] in _CONFIRMATIONS and data.status not in ("confirmed", "paid"):
        # Held back by a retry while the booking was cancelled: the cancellation letter is the
        # news now, and "your booking is confirmed" after it would be false.
        await conn.execute(
            """
            UPDATE email_message
            SET status = 'skipped', last_error = $2
            WHERE id = $1
            """,
            row["id"],
            f"not sent: the booking is {data.status} by now",
        )
        log.info("email-skipped", id=row["id"], kind=row["kind"], booking_status=data.status)
        return "skipped"
    try:
        text = templates.render(
            row["kind"], data, base_url=settings.app_base_url, reply_to=settings.mail_reply_to
        )
        outgoing = Outgoing(text.to, text.subject, text.body, f"<{row['id']}@{_from_domain()}>")
    except Exception as exc:
        await _mark_failed(conn, row, f"cannot write the letter: {exc!r}")
        return "failed"

    try:
        await asyncio.wait_for(transport.send(outgoing), timeout=send_deadline())
    except PermanentMailError as exc:
        await _mark_failed(conn, row, str(exc), text)
        return "failed"
    except Exception as exc:  # temporary, or anything unforeseen: treat both as "try later"
        reason = str(exc) or type(exc).__name__  # a timeout has no message
        if row["attempts"] >= settings.mail_max_attempts:
            await _mark_failed(
                conn, row, f"gave up after {row['attempts']} attempts: {reason}", text
            )
            return "failed"
        delay = BACKOFF_SEC[min(row["attempts"], len(BACKOFF_SEC)) - 1]
        await conn.execute(
            """
            UPDATE email_message
            SET next_attempt_at = now() + make_interval(secs => $2), last_error = $3
            WHERE id = $1
            """,
            row["id"],
            float(delay),
            reason[:500],
        )
        log.warning(
            "email-send-failed",
            id=row["id"],
            attempts=row["attempts"],
            retry_in_sec=delay,
            error=scrubbed(reason)[:200],
        )
        return "retry"

    await conn.execute(
        """
        UPDATE email_message
        SET status = 'sent', sent_at = now(), last_error = NULL,
            to_email = $2, subject = $3, delivery = $4,
            body = CASE WHEN $4 = 'log' THEN $5 ELSE NULL END
        WHERE id = $1
        """,
        row["id"],
        text.to,
        text.subject,
        transport.name,
        text.body,
    )
    return "sent"


async def _mark_failed(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    error: str,
    text: templates.Rendered | None = None,
) -> None:
    """Give a letter up. The row keeps the full error and, when the letter was written, who it was
    for (an admin has to see which address was refused); the log gets the error without addresses."""
    await conn.execute(
        """
        UPDATE email_message
        SET status = 'failed', last_error = $2,
            to_email = coalesce($3, to_email), subject = coalesce($4, subject)
        WHERE id = $1
        """,
        row["id"],
        error[:500],
        text.to if text else None,
        text.subject if text else None,
    )
    log.error(
        "email-failed-permanently",
        id=row["id"],
        kind=row["kind"],
        booking_id=row["booking_id"],
        error=scrubbed(error)[:200],
    )
