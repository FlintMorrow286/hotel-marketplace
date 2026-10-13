"""Letters: queued with the booking change, written and sent later, never at a booking's cost.

The queue row is written by the confirmation and the refund in their own transaction, so these tests
look at three things: that a booking event always leaves its letters (and exactly once), that the
sender delivers, retries and gives up as documented, and that the SMTP transport really speaks SMTP
(against a small server written here, since no real one is available to a test).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import email
import email.policy
import re

import asyncpg
import pytest
import pytest_asyncio
from starlette.testclient import TestClient

from app.config import legal
from app.config.checks import ConfigError, check_config
from app.config.settings import Settings, settings
from app.db.pool import close_pool, init_pool
from app.jobs import mail as mail_job
from app.main import create_app
from app.modules.auth.jwt import create_access_token
from app.modules.booking import service as booking_service
from app.modules.booking.cancellation import free_cancellation_last_day
from app.modules.notification import email as mailer
from app.modules.notification import templates
from app.modules.payment import service as payment_service
from tests._consent import consent
from tests.test_admin import _seed_admin
from tests.test_booking import TODAY, _guest, _seed_unit
from tests.test_payment import _hold
from tests.test_payment_safety import _conflict_on_confirm, _far_booking
from tests.test_startup_checks import GOOD_SECRET, _production, _settings


@contextlib.asynccontextmanager
async def app_pool():
    """The app's pool, in this test's event loop, for the sender to use.

    A test that also boots the app with TestClient must not hold this at the same time: the app's
    lifespan makes its own pool in its own loop, and asyncpg connections do not cross loops.
    """
    await init_pool()
    try:
        yield
    finally:
        await close_pool()


@pytest_asyncio.fixture
async def pool(_test_db: str):
    async with app_pool():
        yield


class ErrorLog:
    """Stands in for the module's logger, which structlog routes differently from test to test."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    @property
    def events(self) -> list[str]:
        return [event for level, (event, _) in self._errors()]

    def _errors(self):
        return [("error", r) for r in self.records if r[1].get("_level") == "error"]

    def error(self, event: str, **fields) -> None:
        self.records.append((event, {**fields, "_level": "error"}))

    def warning(self, event: str, **fields) -> None:
        self.records.append((event, {**fields, "_level": "warning"}))

    def info(self, event: str, **fields) -> None:
        self.records.append((event, {**fields, "_level": "info"}))

    def text(self) -> str:
        return " ".join(f"{event} {fields}" for event, fields in self.records)


class Recorder:
    """A transport that remembers what it was given, and can be told to fail."""

    name = "log"

    def __init__(self, *errors: Exception, delay: float = 0.0) -> None:
        self.sent: list[mailer.Outgoing] = []
        self.calls = 0
        self._errors = list(errors)
        self._delay = delay

    async def send(self, message: mailer.Outgoing) -> None:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        self.sent.append(message)


async def _letters(conn: asyncpg.Connection, booking_id: str | None = None) -> list[asyncpg.Record]:
    rows = await conn.fetch(
        "SELECT * FROM email_message WHERE $1::uuid IS NULL OR booking_id = $1 ORDER BY kind",
        booking_id,
    )
    return list(rows)


async def _kinds(conn: asyncpg.Connection, booking_id: str) -> list[str]:
    return [r["kind"] for r in await _letters(conn, booking_id)]


async def _confirmed(conn: asyncpg.Connection, email_address: str) -> str:
    ut = await _seed_unit(conn, email_address)
    booking_id = await _hold(conn, ut)
    await payment_service.pay_and_confirm(booking_id, conn=conn)
    return booking_id


# ---------------------------------------------------------------- queued with the booking


@pytest.mark.asyncio
async def test_a_confirmation_leaves_one_letter_for_each_side(committed_conn) -> None:
    booking_id = await _confirmed(committed_conn, "mail-confirm@example.com")

    rows = await _letters(committed_conn, booking_id)

    assert [r["kind"] for r in rows] == ["guest_booking_confirmed", "partner_booking_confirmed"]
    assert {r["status"] for r in rows} == {"queued"}
    assert {r["attempts"] for r in rows} == {0}
    assert rows[0]["dedupe_key"] == f"guest_booking_confirmed:{booking_id}"


@pytest.mark.asyncio
async def test_a_channel_booking_tells_only_the_partner(committed_conn) -> None:
    """The channel's guest is the channel's: its own letters apply, ours would be a second set."""
    ut = await _seed_unit(committed_conn, "mail-channel@example.com")
    hold = await booking_service.create_hold(
        committed_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        origin="channel",
        **_guest(),
    )
    await payment_service.pay_and_confirm(hold["id"], conn=committed_conn)

    assert await _kinds(committed_conn, hold["id"]) == ["partner_booking_confirmed"]


@pytest.mark.asyncio
async def test_the_same_event_owes_each_recipient_one_letter(committed_conn, monkeypatch) -> None:
    booking_id = await _confirmed(committed_conn, "mail-once@example.com")
    errors = ErrorLog()
    monkeypatch.setattr(mailer, "log", errors)

    await mailer.enqueue_booking_mail(committed_conn, booking_id, "confirmed", origin="web")
    await mailer.enqueue_booking_mail(committed_conn, booking_id, "confirmed", origin="web")

    assert len(await _letters(committed_conn, booking_id)) == 2
    assert errors.events == [], "a repeat is expected, not an error"


@pytest.mark.asyncio
async def test_no_confirmation_no_letters(committed_conn, monkeypatch) -> None:
    """The row is in the confirmation's transaction: when that one fails, there is no letter."""
    ut = await _seed_unit(committed_conn, "mail-rollback@example.com")
    booking_id = await _hold(committed_conn, ut)
    _conflict_on_confirm(monkeypatch)

    with pytest.raises(payment_service.PaymentError):
        await payment_service.pay_and_confirm(booking_id, conn=committed_conn)

    assert await _letters(committed_conn, booking_id) == []


@pytest.mark.asyncio
async def test_a_broken_queue_costs_a_letter_not_the_booking(committed_conn, caplog) -> None:
    ut = await _seed_unit(committed_conn, "mail-broken@example.com")
    booking_id = await _hold(committed_conn, ut)
    await committed_conn.execute("ALTER TABLE email_message RENAME TO email_message_away")
    try:
        out = await payment_service.pay_and_confirm(booking_id, conn=committed_conn)
    finally:
        await committed_conn.execute("ALTER TABLE email_message_away RENAME TO email_message")

    assert out["status"] == "confirmed"
    assert await _letters(committed_conn, booking_id) == []
    assert (
        await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
        == "confirmed"
    )


@pytest.mark.asyncio
async def test_a_refund_leaves_the_cancellation_letters(committed_conn) -> None:
    booking_id = await _far_booking(committed_conn, "mail-refund@example.com")
    await committed_conn.execute("DELETE FROM email_message")

    await payment_service.refund_booking(booking_id, conn=committed_conn)

    assert await _kinds(committed_conn, booking_id) == [
        "guest_booking_cancelled",
        "partner_booking_cancelled",
    ]


# ---------------------------------------------------------------- the sender


@pytest.mark.asyncio
async def test_the_sender_writes_each_letter_from_the_booking(committed_conn, pool) -> None:
    booking_id = await _confirmed(committed_conn, "mail-send@example.com")
    guest_email = await committed_conn.fetchval(
        "SELECT guest_email::text FROM booking WHERE id = $1", booking_id
    )
    code = await committed_conn.fetchval("SELECT code FROM booking WHERE id = $1", booking_id)
    transport = Recorder()

    took = await mailer.send_due(transport=transport)

    assert took == 2 and transport.calls == 2
    by_kind = {r["kind"]: r for r in await _letters(committed_conn, booking_id)}
    guest, partner = by_kind["guest_booking_confirmed"], by_kind["partner_booking_confirmed"]
    assert (guest["status"], guest["to_email"], guest["delivery"]) == ("sent", guest_email, "log")
    assert partner["to_email"] == "mail-send@example.com"
    assert code in guest["subject"] and code in partner["subject"]
    assert guest["sent_at"] is not None and guest["last_error"] is None
    assert "Бронь подтверждена и оплачена" in guest["body"]
    assert "/my-booking" in guest["body"]
    assert "Гость:" in partner["body"] and "+79991234567" in partner["body"]
    assert await mailer.send_due(transport=transport) == 0, "a sent letter is not sent again"


@pytest.mark.asyncio
async def test_a_letter_carries_nothing_that_works_as_a_key(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "mail-token@example.com", published=True)
    with TestClient(create_app()) as client:
        guest = _guest()
        hold = client.post(
            "/v1/bookings/hold",
            json={
                "unit_type_id": ut,
                "checkin": TODAY.isoformat(),
                "checkout": (TODAY + dt.timedelta(days=2)).isoformat(),
                "guest": {
                    "name": guest["guest_name"],
                    "email": guest["guest_email"],
                    "phone": guest["guest_phone"],
                },
                "consent": consent(),
            },
        ).json()
        paid = client.post(
            f"/v1/bookings/{hold['id']}/pay", headers={"X-Booking-Token": hold["access_token"]}
        )
        assert paid.status_code == 200, paid.text
    transport = Recorder()

    async with app_pool():
        await mailer.send_due(transport=transport)

    assert len(transport.sent) == 2
    for letter in transport.sent:
        assert hold["access_token"] not in letter.body
        assert hold["id"] not in letter.body, (
            "the booking id sits in URLs and logs: not a secret, but no use here"
        )
    rows = await _letters(committed_conn, hold["id"])
    guest_row = next(r for r in rows if r["kind"] == "guest_booking_confirmed")
    assert legal.LEGAL_DOCS_VERSION in guest_row["body"], "the version the guest accepted is named"


@pytest.mark.asyncio
async def test_the_cancellation_letters_say_what_was_paid_back(committed_conn, pool) -> None:
    booking_id = await _far_booking(committed_conn, "mail-cancel@example.com")
    await payment_service.refund_booking(booking_id, conn=committed_conn)
    total = await committed_conn.fetchval(
        "SELECT total_amount::float8 FROM booking WHERE id = $1", booking_id
    )
    transport = Recorder()

    await mailer.send_due(transport=transport, limit=20)

    rows = {r["kind"]: r for r in await _letters(committed_conn, booking_id)}
    assert (
        f"Возврат: {templates.money(total)}, отмена была бесплатной"
        in (rows["guest_booking_cancelled"]["body"])
    )
    assert "Отмена бесплатная" in rows["partner_booking_cancelled"]["body"]


@pytest.mark.asyncio
async def test_a_late_cancellation_names_the_part_the_partner_keeps(committed_conn, pool) -> None:
    ut = await _seed_unit(committed_conn, "mail-late@example.com")
    booking_id = await _hold(committed_conn, ut)  # checks in today: past the free window
    await payment_service.pay_and_confirm(booking_id, conn=committed_conn)
    await payment_service.refund_booking(booking_id, conn=committed_conn)
    total = await committed_conn.fetchval(
        "SELECT total_amount::float8 FROM booking WHERE id = $1", booking_id
    )
    kept = round(total * legal.CANCELLATION_PENALTY_RATE, 2)
    transport = Recorder()

    await mailer.send_due(transport=transport, limit=20)

    rows = {r["kind"]: r for r in await _letters(committed_conn, booking_id)}
    assert f"удержано {templates.money(kept)}" in rows["guest_booking_cancelled"]["body"]
    assert f"у вас остаётся {templates.money(kept)}" in rows["partner_booking_cancelled"]["body"]


@pytest.mark.asyncio
async def test_a_temporary_failure_is_tried_again_later(committed_conn, pool) -> None:
    booking_id = await _confirmed(committed_conn, "mail-retry@example.com")
    flaky = Recorder(mailer.TemporaryMailError("server busy"), mailer.TemporaryMailError("busy"))

    await mailer.send_due(transport=flaky)

    rows = await _letters(committed_conn, booking_id)
    assert {r["status"] for r in rows} == {"queued"}
    assert {r["attempts"] for r in rows} == {1}
    assert all("busy" in r["last_error"] for r in rows)
    wait = await committed_conn.fetchval(
        "SELECT min(extract(epoch FROM next_attempt_at - now())) FROM email_message"
    )
    assert 0 < wait <= mailer.BACKOFF_SEC[0], "the first retry waits the first backoff"
    assert await mailer.send_due(transport=flaky) == 0, "and is not due before then"

    await committed_conn.execute("UPDATE email_message SET next_attempt_at = now()")
    await mailer.send_due(transport=flaky)
    assert {r["status"] for r in await _letters(committed_conn, booking_id)} == {"sent"}


@pytest.mark.asyncio
async def test_a_letter_is_given_up_on_after_its_attempts(
    committed_conn, pool, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "mail_max_attempts", 2)
    booking_id = await _confirmed(committed_conn, "mail-giveup@example.com")
    down = Recorder(*[mailer.TemporaryMailError("down")] * 4)

    await mailer.send_due(transport=down)
    await committed_conn.execute("UPDATE email_message SET next_attempt_at = now()")
    await mailer.send_due(transport=down)

    rows = await _letters(committed_conn, booking_id)
    assert {r["status"] for r in rows} == {"failed"}
    assert all("gave up after 2 attempts" in r["last_error"] for r in rows)
    await committed_conn.execute("UPDATE email_message SET next_attempt_at = now()")
    assert await mailer.send_due(transport=down) == 0, "a failed letter is left alone"


@pytest.mark.asyncio
async def test_a_refused_recipient_is_not_tried_again(committed_conn, pool) -> None:
    booking_id = await _confirmed(committed_conn, "mail-refused@example.com")
    refusing = Recorder(*[mailer.PermanentMailError("no such mailbox")] * 2)

    await mailer.send_due(transport=refusing)

    rows = await _letters(committed_conn, booking_id)
    assert {r["status"] for r in rows} == {"failed"} and {r["attempts"] for r in rows} == {1}
    assert refusing.calls == 2


@pytest.mark.asyncio
async def test_a_failure_nobody_foresaw_is_treated_as_try_later(committed_conn, pool) -> None:
    booking_id = await _confirmed(committed_conn, "mail-odd@example.com")

    await mailer.send_due(transport=Recorder(RuntimeError("boom"), RuntimeError("boom")))

    rows = await _letters(committed_conn, booking_id)
    assert {r["status"] for r in rows} == {"queued"}
    assert all("boom" in r["last_error"] for r in rows)


@pytest.mark.asyncio
async def test_a_letter_for_a_booking_that_is_gone_fails_and_the_rest_go_on(
    committed_conn, pool
) -> None:
    gone = await _confirmed(committed_conn, "mail-gone@example.com")
    await committed_conn.execute("DELETE FROM email_message WHERE booking_id = $1", gone)
    await committed_conn.execute(
        "INSERT INTO email_message (dedupe_key, kind, booking_id) "
        "VALUES ('guest_booking_confirmed:x', 'guest_booking_confirmed', gen_random_uuid())"
    )
    other = await _confirmed(committed_conn, "mail-stays@example.com")
    transport = Recorder()

    await mailer.send_due(transport=transport)

    ghost = await committed_conn.fetchrow(
        "SELECT status, last_error FROM email_message WHERE dedupe_key = 'guest_booking_confirmed:x'"
    )
    assert (ghost["status"], ghost["last_error"]) == ("failed", "the booking is gone")
    assert {r["status"] for r in await _letters(committed_conn, other)} == {"sent"}


@pytest.mark.asyncio
async def test_two_senders_at_once_send_a_letter_once(committed_conn, pool) -> None:
    await _confirmed(committed_conn, "mail-race@example.com")
    slow = Recorder(delay=0.2)

    took = await asyncio.gather(
        mailer.send_due(transport=slow, limit=1),
        mailer.send_due(transport=slow, limit=1),
        mailer.send_due(transport=slow, limit=1),
    )

    assert sum(took) == 2, "two letters, each taken by one sender"
    assert slow.calls == 2
    assert len({m.message_id for m in slow.sent}) == 2


@pytest.mark.asyncio
async def test_a_claimed_letter_is_held_until_its_lease_runs_out(committed_conn, pool) -> None:
    await _confirmed(committed_conn, "mail-lease@example.com")

    first = await mailer.claim_due(committed_conn)
    again = await mailer.claim_due(committed_conn)
    await committed_conn.execute(
        "UPDATE email_message SET next_attempt_at = now() - interval '1 s'"
    )
    later = await mailer.claim_due(committed_conn)

    assert (len(first), len(again), len(later)) == (2, 0, 2)
    assert {r["attempts"] for r in later} == {2}


@pytest.mark.asyncio
async def test_old_letters_are_deleted_and_recent_ones_are_not(
    committed_conn, pool, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "mail_sent_retention_days", 30)
    monkeypatch.setattr(settings, "mail_failed_retention_days", 180)
    await _confirmed(committed_conn, "mail-purge@example.com")
    await mailer.send_due(transport=Recorder(), limit=20)
    await committed_conn.execute(
        "UPDATE email_message SET status = 'failed' WHERE kind LIKE 'partner%'"
    )
    rows = {r["kind"]: r["id"] for r in await _letters(committed_conn)}
    old, kept = rows["guest_booking_confirmed"], rows["partner_booking_confirmed"]

    await committed_conn.execute(
        "UPDATE email_message SET sent_at = now() - interval '31 days' WHERE id = $1", old
    )
    assert await mailer.purge_old(committed_conn) == 1
    assert [r["id"] for r in await _letters(committed_conn)] == [kept], (
        "a failed letter outlives it"
    )

    await committed_conn.execute(
        "UPDATE email_message SET created_at = now() - interval '181 days' WHERE id = $1", kept
    )
    assert await mailer.purge_old(committed_conn) == 1
    assert await _letters(committed_conn) == []


@pytest.mark.asyncio
async def test_the_job_sends_what_is_queued_and_stops_when_cancelled(
    committed_conn, pool, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "mail_poll_interval_sec", 0.05)
    booking_id = await _confirmed(committed_conn, "mail-job@example.com")

    task = asyncio.create_task(mail_job.mail_loop())
    try:
        for _ in range(60):
            if {r["status"] for r in await _letters(committed_conn, booking_id)} == {"sent"}:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert {r["status"] for r in await _letters(committed_conn, booking_id)} == {"sent"}
    assert {r["delivery"] for r in await _letters(committed_conn, booking_id)} == {"log"}


@pytest.mark.asyncio
async def test_the_log_backend_keeps_the_text_and_a_real_one_does_not(committed_conn, pool) -> None:
    booking_id = await _confirmed(committed_conn, "mail-pii@example.com")
    real = Recorder()
    real.name = "smtp"

    await mailer.send_due(transport=real, limit=20)

    rows = await _letters(committed_conn, booking_id)
    assert {r["delivery"] for r in rows} == {"smtp"}
    assert all(r["body"] is None for r in rows), "after a real delivery the text is not kept"
    assert all(r["subject"] and r["to_email"] for r in rows)


@pytest.mark.asyncio
async def test_a_confirmation_held_back_by_a_retry_is_not_sent_after_the_cancellation(
    committed_conn, pool
) -> None:
    """The mail server hiccups, the guest cancels, the server is back: the late "confirmed" would lie."""
    booking_id = await _far_booking(committed_conn, "mail-overtaken@example.com")
    await mailer.send_due(transport=Recorder(*[mailer.TemporaryMailError("busy")] * 2), limit=20)
    await payment_service.refund_booking(booking_id, conn=committed_conn)
    await committed_conn.execute("UPDATE email_message SET next_attempt_at = now()")
    transport = Recorder()

    await mailer.send_due(transport=transport, limit=20)

    rows = {r["kind"]: r for r in await _letters(committed_conn, booking_id)}
    assert [m.subject.split(" — ")[0] for m in sorted(transport.sent, key=lambda m: m.to)] == [
        f"Бронь {rows['guest_booking_cancelled']['subject'].split()[1]} отменена",
        f"Бронь {rows['guest_booking_cancelled']['subject'].split()[1]} отменена",
    ], "only the two cancellation letters went out"
    for kind in ("guest_booking_confirmed", "partner_booking_confirmed"):
        assert rows[kind]["status"] == "skipped"
        assert "the booking is refunded by now" in rows[kind]["last_error"]
    assert {
        rows[k]["status"] for k in ("guest_booking_cancelled", "partner_booking_cancelled")
    } == {"sent"}
    assert await mailer.send_due(transport=transport) == 0, "a skipped letter is not tried again"


@pytest.mark.asyncio
async def test_an_address_the_server_refused_stays_out_of_the_log(
    committed_conn, pool, monkeypatch
) -> None:
    """Error-level lines go to Sentry as events: a guest's address must not be in them."""
    booking_id = await _confirmed(committed_conn, "mail-nolog@example.com")
    errors = ErrorLog()
    monkeypatch.setattr(mailer, "log", errors)
    refused = "recipient refused: {'someone@example.com': (550, b'5.1.1 no such user')}"
    deferred = "recipient deferred: {'someone@example.com': (450, b'try later')}"

    await mailer.send_due(
        transport=Recorder(mailer.PermanentMailError(refused), mailer.TemporaryMailError(deferred)),
        limit=20,
    )

    assert "someone@example.com" not in errors.text()
    assert "<address>" in errors.text() and "550" in errors.text()
    rows = {r["status"]: r for r in await _letters(committed_conn, booking_id)}
    assert "someone@example.com" in rows["failed"]["last_error"], "the row keeps it, for the admin"
    assert rows["failed"]["to_email"] and rows["failed"]["subject"], (
        "and says who the letter was for"
    )
    assert "someone@example.com" in rows["queued"]["last_error"]


@pytest.mark.parametrize(
    ("text", "scrubbed"),
    [
        (
            "recipient refused: {'a.b+c@mail.example.com': (550, b'x')}",
            "recipient refused: {'<address>': (550, b'x')}",
        ),
        ("550 <ivan@example.com>: mailbox unavailable", "550 <address>: mailbox unavailable"),
        ("no addresses here: 421 try later", "no addresses here: 421 try later"),
    ],
)
def test_addresses_are_blanked_for_the_log(text: str, scrubbed: str) -> None:
    assert mailer.scrubbed(text) == scrubbed


@pytest.mark.asyncio
async def test_a_letter_that_takes_too_long_is_cut_off_and_tried_again(
    committed_conn, pool, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "smtp_timeout_sec", 0.02)  # a letter gets three times that
    booking_id = await _confirmed(committed_conn, "mail-slow@example.com")

    await mailer.send_due(transport=Recorder(delay=0.5), limit=20)

    rows = await _letters(committed_conn, booking_id)
    assert {r["status"] for r in rows} == {"queued"} and {r["attempts"] for r in rows} == {1}
    assert all("TimeoutError" in r["last_error"] for r in rows)


def test_a_claim_lasts_as_long_as_its_batch_can(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_timeout_sec", 20.0)
    assert mailer.send_deadline() == 60.0 and mailer.lease_sec(10) == 660.0
    monkeypatch.setattr(settings, "smtp_timeout_sec", 60.0)
    assert mailer.lease_sec(10) == 1860.0
    monkeypatch.setattr(settings, "smtp_timeout_sec", 0.1)
    assert mailer.lease_sec(1) == 120.0, "never shorter than two minutes"


# ---------------------------------------------------------------- the text


def _sample(**overrides) -> templates.BookingMail:
    values = dict(
        code="BK-7K2Q9F",
        status="confirmed",
        origin="web",
        guest_name="Иван Гость",
        guest_email="ivan@example.com",
        guest_phone="+79991234567",
        checkin=dt.date(2027, 7, 12),
        checkout=dt.date(2027, 7, 14),
        checkin_time=dt.time(14, 0),
        checkout_time=dt.time(12, 0),
        total=13000.0,
        commission=1560.0,
        kept=None,
        refunded=None,
        docs_version="2026.09-draft",
        property_name="Дом у моря",
        city="Коктебель",
        unit_name="Номер с балконом",
        partner_name="Мария",
        partner_email="maria@example.com",
    )
    return templates.BookingMail(**{**values, **overrides})


def test_the_confirmation_says_what_a_guest_needs_to_know() -> None:
    letter = templates.render(
        templates.KIND_GUEST_CONFIRMED, _sample(), base_url="https://hotels.example.com/"
    )

    assert letter.to == "ivan@example.com"
    assert letter.subject == "Бронь BK-7K2Q9F подтверждена — Дом у моря"
    for line in (
        "Здравствуйте, Иван Гость!",
        "Код брони: BK-7K2Q9F",
        "Объект: Дом у моря, Номер с балконом, Коктебель",
        "Заезд: 12 июля 2027, с 14:00",
        "Выезд: 14 июля 2027, до 12:00",
        "2\u00a0ночи, оплачено 13\u00a0000\u00a0₽",
        "Отмена бесплатна до конца дня 11 июля 2027 по времени объекта.",
        f"Позже удерживается {round(legal.CANCELLATION_PENALTY_RATE * 100)}% стоимости.",
        "https://hotels.example.com/my-booking",
        "версия документов 2026.09-draft): https://hotels.example.com/legal/offer",
        "отвечать на него не нужно",
    ):
        assert line in letter.body, line
    assert "None" not in letter.body


def test_a_reply_address_replaces_the_no_reply_line() -> None:
    letter = templates.render(
        templates.KIND_GUEST_CONFIRMED,
        _sample(docs_version=None),
        base_url="https://h.example.com",
        reply_to="help@h.example.com",
    )

    assert "Вопросы по брони можно задать на help@h.example.com." in letter.body
    assert "отвечать на него не нужно" not in letter.body
    assert "версия документов" not in letter.body


def test_the_cancellation_deadline_follows_the_refund_rule(monkeypatch) -> None:
    checkin = dt.date(2027, 7, 12)
    assert free_cancellation_last_day(checkin) == dt.date(2027, 7, 11)
    monkeypatch.setattr(legal, "CANCELLATION_FREE_BEFORE_HOURS", 48)
    assert free_cancellation_last_day(checkin) == dt.date(2027, 7, 10)

    letter = templates.render(templates.KIND_GUEST_CONFIRMED, _sample(), base_url="https://h")

    assert "до конца дня 10 июля 2027" in letter.body


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (0, "0\u00a0₽"),
        (999.0, "999\u00a0₽"),
        (13000.0, "13\u00a0000\u00a0₽"),
        (1234567.0, "1\u00a0234\u00a0567\u00a0₽"),
        (3250.5, "3\u00a0250,50\u00a0₽"),
        (0.07, "0,07\u00a0₽"),
    ],
)
def test_money_is_written_the_russian_way(amount: float, text: str) -> None:
    assert templates.money(amount) == text


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (1, "ночь"),
        (2, "ночи"),
        (4, "ночи"),
        (5, "ночей"),
        (11, "ночей"),
        (12, "ночей"),
        (21, "ночь"),
        (22, "ночи"),
        (25, "ночей"),
        (101, "ночь"),
        (111, "ночей"),
    ],
)
def test_nights_agree_with_their_number(n: int, word: str) -> None:
    assert templates.plural(n, "ночь", "ночи", "ночей") == word


def test_a_name_cannot_break_a_header() -> None:
    letter = templates.render(
        templates.KIND_PARTNER_CONFIRMED,
        _sample(property_name="Дом\r\nBcc: spy@example.com"),
        base_url="https://h",
    )

    assert "\n" not in letter.subject and "\r" not in letter.subject


def test_the_partner_is_told_how_to_reach_the_guest() -> None:
    letter = templates.render(templates.KIND_PARTNER_CONFIRMED, _sample(), base_url="https://h")

    assert letter.to == "maria@example.com"
    assert "Гость: Иван Гость, +79991234567, ivan@example.com" in letter.body
    assert "комиссия платформы 1\u00a0560\u00a0₽" in letter.body


def test_an_unknown_kind_is_not_written() -> None:
    with pytest.raises(ValueError):
        templates.render("newsletter", _sample(), base_url="https://h")


# ---------------------------------------------------------------- the SMTP transport


class FakeSmtp:
    """Enough of an SMTP server to receive a letter: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def __init__(
        self,
        *,
        rcpt: str = "250 ok",
        data: str = "250 queued",
        login: tuple[str, str] | None = None,
        greeting: str = "220 fake ESMTP",
    ) -> None:
        self.rcpt, self.data, self.login, self.greeting = rcpt, data, login, greeting
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []
        self.auth: list[str] = []
        self.commands: list[str] = []
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> FakeSmtp:
        self._server = await asyncio.start_server(self._session, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(line.encode() + b"\r\n")
            await writer.drain()

        sender, rcpts = "", []
        await say(self.greeting)
        if not self.greeting.startswith("220"):
            writer.close()
            return
        while line := (await reader.readline()).decode().rstrip("\r\n"):
            self.commands.append(line.split(" ")[0].upper())
            verb = line.upper()
            if verb.startswith("EHLO"):
                writer.write(b"250-fake\r\n250-8BITMIME\r\n250 AUTH PLAIN\r\n")
                await writer.drain()
            elif verb.startswith("AUTH PLAIN"):
                self.auth.append(base64.b64decode(line.split(" ")[2]).decode())
                ok = self.login is None or self.auth[-1] == "\0".join(("", *self.login))
                await say("235 ok" if ok else "535 5.7.8 bad credentials")
            elif verb.startswith("MAIL FROM"):
                sender = re.search(r"<([^>]*)>", line).group(1)
                await say("250 ok")
            elif verb.startswith("RCPT TO"):
                await say(self.rcpt)
                if self.rcpt.startswith("250"):
                    rcpts.append(re.search(r"<([^>]*)>", line).group(1))
            elif verb == "DATA":
                await say("354 go on")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    data += await reader.readline()
                if self.data.startswith("250"):
                    self.messages.append(data[:-3])
                    self.envelopes.append((sender, rcpts))
                await say(self.data)
            elif verb == "QUIT":
                await say("221 bye")
                break
            else:
                await say("250 ok")
        writer.close()


def _smtp(monkeypatch, server: FakeSmtp, **overrides) -> mailer.SmtpTransport:
    values = dict(
        mail_backend="smtp",
        smtp_host="127.0.0.1",
        smtp_port=server.port,
        smtp_starttls=False,
        smtp_ssl=False,
        smtp_user="",
        smtp_password="",
        smtp_timeout_sec=5.0,
        mail_from="Бронирование <no-reply@hotels.example.com>",
        mail_reply_to="",
    )
    for name, value in {**values, **overrides}.items():
        monkeypatch.setattr(settings, name, value)
    return mailer.SmtpTransport()


_LETTER = mailer.Outgoing(
    to="ivan@example.com",
    subject="Бронь BK-7K2Q9F подтверждена — Дом у моря",
    body="Здравствуйте, Иван!\nСумма 13\u00a0000\u00a0₽\n",
    message_id="<0b7e1a9e@hotels.example.com>",
)


@pytest.mark.asyncio
async def test_smtp_delivers_a_letter_that_reads_back_intact(monkeypatch) -> None:
    async with FakeSmtp() as server:
        transport = _smtp(monkeypatch, server, mail_reply_to="help@hotels.example.com")

        await transport.send(_LETTER)

    assert server.envelopes == [("no-reply@hotels.example.com", ["ivan@example.com"])]
    letter = email.message_from_bytes(server.messages[0], policy=email.policy.default)
    assert letter["Subject"] == _LETTER.subject
    assert letter["To"] == "ivan@example.com"
    assert "no-reply@hotels.example.com" in letter["From"] and "Бронирование" in letter["From"]
    assert letter["Message-ID"] == _LETTER.message_id
    assert letter["Reply-To"] == "help@hotels.example.com"
    assert letter["Auto-Submitted"] == "auto-generated"
    assert letter["Date"]
    assert letter.get_content().replace("\r\n", "\n") == _LETTER.body
    assert b"\xd0" not in server.messages[0], "the wire is 7-bit: quoted-printable, not raw UTF-8"
    assert "AUTH" not in server.commands, "no account configured: no login"


@pytest.mark.asyncio
async def test_smtp_logs_in_when_an_account_is_configured(monkeypatch) -> None:
    async with FakeSmtp(login=("mailer", "s3cret")) as server:
        transport = _smtp(monkeypatch, server, smtp_user="mailer", smtp_password="s3cret")
        await transport.send(_LETTER)

        assert server.auth == ["\0mailer\0s3cret"] and len(server.messages) == 1


@pytest.mark.asyncio
async def test_a_wrong_account_is_not_a_reason_to_give_up_on_the_letter(monkeypatch) -> None:
    async with FakeSmtp(login=("mailer", "s3cret")) as server:
        transport = _smtp(monkeypatch, server, smtp_user="mailer", smtp_password="wrong")

        with pytest.raises(mailer.TemporaryMailError, match="authentication"):
            await transport.send(_LETTER)

    assert server.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_kwargs", "expected"),
    [
        ({"rcpt": "550 5.1.1 no such user"}, mailer.PermanentMailError),
        ({"rcpt": "450 4.2.0 greylisted, try later"}, mailer.TemporaryMailError),
        ({"data": "554 5.7.1 rejected as spam"}, mailer.PermanentMailError),
        ({"data": "451 4.3.0 local error"}, mailer.TemporaryMailError),
        ({"greeting": "421 4.3.2 shutting down"}, mailer.TemporaryMailError),
    ],
)
async def test_smtp_answers_are_sorted_into_final_and_try_later(
    monkeypatch, server_kwargs, expected
) -> None:
    async with FakeSmtp(**server_kwargs) as server:
        transport = _smtp(monkeypatch, server)

        with pytest.raises(expected):
            await transport.send(_LETTER)


@pytest.mark.asyncio
async def test_an_unreachable_server_is_try_later(monkeypatch) -> None:
    async with FakeSmtp() as server:
        port = server.port
        transport = _smtp(monkeypatch, server, smtp_port=port)
    # The server is closed now: nothing listens on that port.

    with pytest.raises(mailer.TemporaryMailError):
        await transport.send(_LETTER)


@pytest.mark.asyncio
async def test_a_header_the_standard_forbids_fails_the_letter_for_good(monkeypatch) -> None:
    async with FakeSmtp() as server:
        transport = _smtp(monkeypatch, server)
        broken = mailer.Outgoing("a@example.com", "line one\nBcc: x@example.com", "text", "<1@h>")

        with pytest.raises(mailer.PermanentMailError, match="cannot build"):
            await transport.send(broken)

    assert server.messages == []


@pytest.mark.asyncio
async def test_the_sender_delivers_through_smtp_and_forgets_the_text(
    committed_conn, pool, monkeypatch
) -> None:
    booking_id = await _confirmed(committed_conn, "mail-wire@example.com")
    async with FakeSmtp() as server:
        _smtp(monkeypatch, server)

        took = await mailer.send_due()

    assert took == 2 and len(server.messages) == 2
    rows = await _letters(committed_conn, booking_id)
    assert {r["delivery"] for r in rows} == {"smtp"} and all(r["body"] is None for r in rows)
    ids = {email.message_from_bytes(m)["Message-ID"] for m in server.messages}
    assert ids == {f"<{r['id']}@hotels.example.com>" for r in rows}, "the id follows the queue row"


def test_the_backend_setting_picks_the_transport(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mail_backend", "log")
    assert isinstance(mailer.get_transport(), mailer.LogTransport)
    monkeypatch.setattr(settings, "mail_backend", "smtp")
    assert isinstance(mailer.get_transport(), mailer.SmtpTransport)
    monkeypatch.setattr(settings, "mail_backend", "carrier-pigeon")
    with pytest.raises(ValueError, match="carrier-pigeon"):
        mailer.get_transport()


# ---------------------------------------------------------------- configuration


def test_a_blank_mail_setting_is_the_default() -> None:
    """Compose passes a key as an empty string when .env names it without a value."""
    blank = Settings(_env_file=None, smtp_port="", smtp_starttls=" ", smtp_ssl="")

    assert (blank.smtp_port, blank.smtp_starttls, blank.smtp_ssl) == (587, True, False)


def test_the_log_backend_is_accepted_everywhere_and_says_so_at_boot() -> None:
    check_config(_settings(app_env="local"))
    check_config(_production())  # no account yet: the deployment runs, and logs a warning


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"mail_backend": "carrier-pigeon"}, "MAIL_BACKEND"),
        ({"mail_backend": "smtp", "smtp_host": ""}, "SMTP_HOST"),
        (
            {"mail_backend": "smtp", "smtp_host": "smtp.example.com", "mail_from": "nobody"},
            "MAIL_FROM",
        ),
        (
            {
                "mail_backend": "smtp",
                "smtp_host": "smtp.example.com",
                "smtp_ssl": True,
                "smtp_starttls": True,
            },
            "SMTP_STARTTLS and SMTP_SSL",
        ),
        (
            {"mail_backend": "smtp", "smtp_host": "smtp.example.com", "smtp_user": "mailer"},
            "SMTP_USER and SMTP_PASSWORD",
        ),
    ],
)
@pytest.mark.parametrize("env", ["local", "production"])
def test_a_mail_configuration_that_cannot_work_is_refused_everywhere(
    env: str, overrides: dict, complaint: str
) -> None:
    settings_ = (
        _production(**overrides)
        if env == "production"
        else _settings(app_env="local", jwt_secret=GOOD_SECRET, **overrides)
    )

    with pytest.raises(ConfigError, match=complaint):
        check_config(settings_)


def test_a_working_smtp_configuration_passes() -> None:
    working = dict(
        mail_backend="smtp",
        smtp_host="smtp.example.com",
        smtp_user="mailer",
        smtp_password="s3cret",
        mail_from="Hotels <no-reply@example.com>",
    )
    check_config(_production(**working))
    check_config(_production(**{**working, "smtp_starttls": False, "smtp_ssl": True}))


def test_production_does_not_send_a_password_in_clear_text() -> None:
    with pytest.raises(ConfigError, match="clear text"):
        check_config(
            _production(
                mail_backend="smtp",
                smtp_host="smtp.example.com",
                smtp_user="mailer",
                smtp_password="s3cret",
                smtp_starttls=False,
            )
        )
    # Development may talk to a local catcher without TLS, account or not.
    check_config(
        _settings(
            app_env="local",
            mail_backend="smtp",
            smtp_host="localhost",
            smtp_user="mailer",
            smtp_password="s3cret",
            smtp_starttls=False,
        )
    )


def test_the_pages_learn_what_they_may_promise(monkeypatch) -> None:
    client = TestClient(create_app())  # no lifespan: the answer needs no database
    cases = [
        ("stub", "log", True, False),
        ("mock_acquiring", "smtp", True, True),
        ("tinkoff", "smtp", False, True),
    ]
    for payment, mail, test_mode, enabled in cases:
        monkeypatch.setattr(settings, "payment_mode", payment)
        monkeypatch.setattr(settings, "mail_backend", mail)
        assert client.get("/v1/config").json() == {
            "payment_test_mode": test_mode,
            "email_enabled": enabled,
        }


# ---------------------------------------------------------------- the admin's view


@pytest.mark.asyncio
async def test_the_admin_sees_the_queue_and_can_put_a_failed_letter_back(
    committed_conn,
) -> None:
    await _seed_admin(committed_conn, "mail-admin@example.com")
    admin_id = await committed_conn.fetchval("SELECT id::text FROM admin LIMIT 1")
    token = create_access_token(admin_id, "admin")
    booking_id = await _confirmed(committed_conn, "mail-admin-view@example.com")
    async with app_pool():
        refusing = Recorder(mailer.PermanentMailError("no such mailbox"))
        await mailer.send_due(transport=refusing, limit=1)
        await mailer.send_due(transport=Recorder(), limit=1)
    rows = {r["status"]: r for r in await _letters(committed_conn, booking_id)}
    code = await committed_conn.fetchval("SELECT code FROM booking WHERE id = $1", booking_id)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(create_app()) as client:
        everything = client.get("/v1/admin/emails", headers=headers)
        failed = client.get("/v1/admin/emails?status=failed", headers=headers)
        retried = client.post(f"/v1/admin/emails/{rows['failed']['id']}/retry", headers=headers)
        again = client.post(f"/v1/admin/emails/{rows['failed']['id']}/retry", headers=headers)
        sent_one = client.post(f"/v1/admin/emails/{rows['sent']['id']}/retry", headers=headers)
        bad_filter = client.get("/v1/admin/emails?status=lost", headers=headers)

    body = everything.json()
    assert everything.status_code == 200
    assert body["backend"] == settings.mail_backend
    assert body["counts"] == {"queued": 0, "sent": 1, "failed": 1, "skipped": 0}
    assert {item["booking_code"] for item in body["items"]} == {code}
    kept = next(item for item in body["items"] if item["status"] == "sent")
    assert "Бронь" in kept["body"], "the log backend's text is there to read"
    assert [i["status"] for i in failed.json()["items"]] == ["failed"]
    assert failed.json()["items"][0]["last_error"] == "no such mailbox"
    assert retried.status_code == 200 and retried.json()["retried"] == str(rows["failed"]["id"])
    assert again.status_code == 404 and sent_one.status_code == 404
    assert bad_filter.status_code == 422
    after = await _letters(committed_conn, booking_id)
    assert {r["status"] for r in after} == {"queued", "sent"}
    assert next(r for r in after if r["status"] == "queued")["attempts"] == 0


@pytest.mark.asyncio
async def test_only_staff_see_the_queue(committed_conn) -> None:
    partner = create_access_token("00000000-0000-0000-0000-000000000001", "partner")

    with TestClient(create_app()) as client:
        anonymous = client.get("/v1/admin/emails")
        as_partner = client.get("/v1/admin/emails", headers={"Authorization": f"Bearer {partner}"})
        retry = client.post(
            "/v1/admin/emails/00000000-0000-0000-0000-000000000002/retry",
            headers={"Authorization": f"Bearer {partner}"},
        )

    assert anonymous.status_code in (401, 403)
    assert as_partner.status_code in (401, 403) and retry.status_code in (401, 403)
