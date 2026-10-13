"""Payment on the provider's page, played against the mock acquirer.

The guest is sent to a page of the provider and the answer comes later, so the core has to be right
about everything that can happen in between: the notification arrives twice, or never, or before it
is true; the guest pays after the hold was released; the provider took another amount. The mock's
state is its own table, so these tests act as the bank by writing it, the way a real bank would be
asked: the core must never act on what it is told, only on what the provider answers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio
from starlette.testclient import TestClient

from app.config import legal
from app.config.settings import settings
from app.db.pool import close_pool, init_pool
from app.main import create_app
from app.modules.booking import access
from app.modules.booking import service as booking_service
from app.modules.payment import mock_acquirer, service
from app.modules.payment.mock_acquirer import MockAcquiringProvider
from app.modules.payment.provider import get_payment_provider
from tests._consent import consent
from tests.test_booking import TODAY, _guest, _seed_unit
from tests.test_payment import _hold
from tests.test_payment_safety import _conflict_on_confirm

SIGNATURE = mock_acquirer.SIGNATURE_HEADER


@pytest_asyncio.fixture
async def acquirer(monkeypatch, _test_db: str):
    """The mock acquirer is the payment provider, and the app's pool lives in the test's loop."""
    monkeypatch.setattr(settings, "payment_mode", "mock_acquiring")
    await init_pool()
    try:
        yield
    finally:
        await close_pool()


def _signed(payment_id: str) -> tuple[bytes, dict[str, str]]:
    """A notification as the mock bank sends it."""
    body = json.dumps({"payment_id": payment_id}).encode()
    return body, {SIGNATURE: mock_acquirer.sign(body)}


async def _bank(conn: asyncpg.Connection, payment_id: str, status: str) -> None:
    """The bank decides, on its own side, without telling the shop."""
    await conn.execute(
        "UPDATE mock_acquirer_payment SET status = $2, decided_at = now() WHERE id = $1",
        payment_id,
        status,
    )


async def _open(conn: asyncpg.Connection, booking_id: str) -> str:
    session = await service.start_payment(booking_id, conn=conn)
    assert session["status"] == "pending", session
    return session["payment_id"]


async def _statuses(conn: asyncpg.Connection, booking_id: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT status FROM payment WHERE booking_id = $1 ORDER BY created_at", booking_id
    )
    return [r["status"] for r in rows]


async def _booking_status(conn: asyncpg.Connection, booking_id: str) -> str:
    return await conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)


async def _confirmations(conn: asyncpg.Connection, booking_id: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM outbox_event WHERE aggregate_id = $1 AND event_type = $2",
        booking_id,
        "booking.confirmed",
    )


async def _nights(conn: asyncpg.Connection, ut: str) -> tuple[int, int]:
    """(held, sold) summed over the unit's nights."""
    row = await conn.fetchrow(
        "SELECT coalesce(sum(hold), 0) AS held, coalesce(sum(sold), 0) AS sold "
        "FROM inventory_day WHERE unit_type_id = $1",
        ut,
    )
    return row["held"], row["sold"]


# ---------------------------------------------------------------- opening a payment


@pytest.mark.asyncio
async def test_a_session_is_opened_once_and_resumed(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-open@example.com")
    booking_id = await _hold(committed_conn, ut)

    first = await service.start_payment(booking_id, conn=committed_conn)
    again = await service.start_payment(booking_id, conn=committed_conn)

    assert first["status"] == "pending"
    assert again == first, "a second click must get the same page, not a second payment"
    assert first["payment_url"].endswith(f"/mock-acquirer/pay/{first['payment_id']}")
    rows = await committed_conn.fetch(
        "SELECT provider, status, external_id, payment_url, amount::float8 AS amount "
        "FROM payment WHERE booking_id = $1",
        booking_id,
    )
    assert [(r["provider"], r["status"]) for r in rows] == [("mock_acquiring", "pending")]
    assert rows[0]["external_id"] == first["payment_id"]
    assert rows[0]["payment_url"] == first["payment_url"]
    total = await committed_conn.fetchval(
        "SELECT total_amount::float8 FROM booking WHERE id = $1", booking_id
    )
    assert rows[0]["amount"] == total
    assert await committed_conn.fetchval("SELECT count(*) FROM mock_acquirer_payment") == 1
    assert await _booking_status(committed_conn, booking_id) == "hold"


@pytest.mark.asyncio
async def test_a_hold_that_ran_out_cannot_open_a_payment(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-lapsed@example.com")
    booking_id = await _hold(committed_conn, ut)
    await committed_conn.execute(
        "UPDATE booking SET hold_expires_at = now() - interval '1 minute' WHERE id = $1",
        booking_id,
    )

    with pytest.raises(service.PaymentError, match="hold expired"):
        await service.start_payment(booking_id, conn=committed_conn)

    assert await committed_conn.fetchval("SELECT count(*) FROM mock_acquirer_payment") == 0


@pytest.mark.asyncio
async def test_a_booking_that_is_already_paid_answers_confirmed(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-paid-twice@example.com")
    booking_id = await _hold(committed_conn, ut)
    await _bank(committed_conn, await _open(committed_conn, booking_id), "CONFIRMED")
    await service.sync_payment(booking_id, conn=committed_conn)

    again = await service.start_payment(booking_id, conn=committed_conn)

    assert again["status"] == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded"]


# ---------------------------------------------------------------- the result arrives


@pytest.mark.asyncio
async def test_the_bank_paying_confirms_the_booking_once(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-pay@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")

    body, headers = _signed(payment_id)
    await service.receive_notification("mock_acquiring", body, headers)
    await service.receive_notification("mock_acquiring", body, headers)  # banks repeat themselves

    assert await _booking_status(committed_conn, booking_id) == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded"]
    assert await _confirmations(committed_conn, booking_id) == 1
    assert await _nights(committed_conn, ut) == (0, 2)


@pytest.mark.asyncio
async def test_a_notification_alone_confirms_nothing(acquirer, committed_conn) -> None:
    """The bank has not decided; a (genuinely signed) notification says it has. The core asks."""
    ut = await _seed_unit(committed_conn, "acq-claim@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)

    body, headers = _signed(payment_id)
    await service.receive_notification("mock_acquiring", body, headers)

    assert await _booking_status(committed_conn, booking_id) == "hold"
    assert await _statuses(committed_conn, booking_id) == ["pending"]


@pytest.mark.asyncio
async def test_a_notification_that_is_not_genuine_is_refused(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-forged@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    body, _ = _signed(payment_id)

    for headers in ({}, {SIGNATURE: "0" * 64}, {SIGNATURE: "not even hex"}):
        with pytest.raises(service.NotificationRejected) as refused:
            await service.receive_notification("mock_acquiring", body, headers)
        assert refused.value.status_code == 400
    unreadable = b"payment_id=" + payment_id.encode()
    with pytest.raises(service.NotificationRejected) as refused:
        await service.receive_notification(
            "mock_acquiring", unreadable, {SIGNATURE: mock_acquirer.sign(unreadable)}
        )
    assert refused.value.status_code == 400
    with pytest.raises(service.NotificationRejected) as refused:
        await service.receive_notification("tinkoff", body, {SIGNATURE: mock_acquirer.sign(body)})
    assert refused.value.status_code == 404

    assert await _booking_status(committed_conn, booking_id) == "hold"
    assert await _statuses(committed_conn, booking_id) == ["pending"]


@pytest.mark.asyncio
async def test_a_notification_about_a_payment_the_shop_does_not_know_is_ignored(
    acquirer, committed_conn
) -> None:
    body, headers = _signed("mock_does_not_exist")

    await service.receive_notification("mock_acquiring", body, headers)  # no error: no retries

    assert await committed_conn.fetchval("SELECT count(*) FROM payment") == 0


@pytest.mark.asyncio
async def test_a_declined_payment_ends_the_attempt_not_the_hold(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-decline@example.com")
    booking_id = await _hold(committed_conn, ut)
    first = await _open(committed_conn, booking_id)
    await _bank(committed_conn, first, "REJECTED")

    assert (await service.sync_payment(booking_id, conn=committed_conn))["state"] == "failed"
    assert await _booking_status(committed_conn, booking_id) == "hold"
    assert await _statuses(committed_conn, booking_id) == ["pending", "failed"]

    second = await _open(committed_conn, booking_id)  # the guest tries again

    assert second != first
    assert await _statuses(committed_conn, booking_id) == ["pending", "failed", "pending"]
    await _bank(committed_conn, second, "CONFIRMED")
    assert (await service.sync_payment(booking_id, conn=committed_conn))["state"] == "confirmed"
    assert await _nights(committed_conn, ut) == (0, 2)


@pytest.mark.asyncio
async def test_a_session_that_ran_out_is_a_failed_attempt(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-expired@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await committed_conn.execute(
        "UPDATE mock_acquirer_payment SET expires_at = now() - interval '1 second' WHERE id = $1",
        payment_id,
    )

    assert (await service.sync_payment(booking_id, conn=committed_conn))["state"] == "failed"
    assert await _statuses(committed_conn, booking_id) == ["pending", "failed"]
    assert await _open(committed_conn, booking_id) != payment_id


@pytest.mark.asyncio
async def test_sync_reports_a_payment_that_is_still_open(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-open-sync@example.com")
    booking_id = await _hold(committed_conn, ut)
    assert await service.sync_payment(booking_id, conn=committed_conn) == {
        "state": "none",
        "payment_url": None,
        "booking": None,
    }
    session = await service.start_payment(booking_id, conn=committed_conn)

    answer = await service.sync_payment(booking_id, conn=committed_conn)

    assert answer == {"state": "pending", "payment_url": session["payment_url"], "booking": None}


@pytest.mark.asyncio
async def test_a_payment_the_provider_has_forgotten_stays_open(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-forgotten@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await committed_conn.execute("DELETE FROM mock_acquirer_payment WHERE id = $1", payment_id)

    assert (await service.sync_payment(booking_id, conn=committed_conn))["state"] == "pending"
    assert await _booking_status(committed_conn, booking_id) == "hold"


# ---------------------------------------------------------------- the result never arrives


@pytest.mark.asyncio
async def test_sync_finds_a_payment_whose_notification_was_lost(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-sync@example.com")
    booking_id = await _hold(committed_conn, ut)
    await _bank(committed_conn, await _open(committed_conn, booking_id), "CONFIRMED")

    answer = await service.sync_payment(booking_id, conn=committed_conn)

    assert answer["state"] == "confirmed"
    assert await _booking_status(committed_conn, booking_id) == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded"]


@pytest.mark.asyncio
async def test_the_sweep_finds_a_payment_whose_notification_was_lost(
    acquirer, committed_conn
) -> None:
    ut = await _seed_unit(committed_conn, "acq-sweep@example.com")
    booking_id = await _hold(committed_conn, ut)
    await _bank(committed_conn, await _open(committed_conn, booking_id), "CONFIRMED")

    assert await service.reconcile_pending(min_age_sec=0) == 1

    assert await _booking_status(committed_conn, booking_id) == "confirmed"
    assert await service.reconcile_pending(min_age_sec=0) == 0, (
        "a settled payment is not asked again"
    )


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_fresh_payment_to_its_notification(
    acquirer, committed_conn
) -> None:
    ut = await _seed_unit(committed_conn, "acq-fresh@example.com")
    booking_id = await _hold(committed_conn, ut)
    await _bank(committed_conn, await _open(committed_conn, booking_id), "CONFIRMED")

    assert await service.reconcile_pending() == 0

    assert await _booking_status(committed_conn, booking_id) == "hold"


@pytest.mark.asyncio
async def test_the_sweep_closes_a_session_nobody_paid(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-abandoned@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await committed_conn.execute(
        "UPDATE mock_acquirer_payment SET expires_at = now() - interval '1 second' WHERE id = $1",
        payment_id,
    )

    assert await service.reconcile_pending(min_age_sec=0) == 1

    assert await _statuses(committed_conn, booking_id) == ["pending", "failed"]


@pytest.mark.asyncio
async def test_the_sweep_finishes_a_charge_the_process_died_on(acquirer, committed_conn) -> None:
    """Paid and written down, then the process died before it confirmed the booking."""
    ut = await _seed_unit(committed_conn, "acq-crashed@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    await committed_conn.execute(
        "INSERT INTO payment (booking_id, provider, amount, status, external_id) "
        "SELECT booking_id, provider, amount, 'succeeded', external_id "
        "FROM payment WHERE booking_id = $1",
        booking_id,
    )
    assert await _booking_status(committed_conn, booking_id) == "hold"

    assert await service.reconcile_pending(min_age_sec=0) == 1

    assert await _booking_status(committed_conn, booking_id) == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded"]
    assert await service.reconcile_pending(min_age_sec=0) == 0


@pytest.mark.asyncio
async def test_the_sweep_does_nothing_under_an_instant_provider(committed_conn) -> None:
    assert settings.payment_mode == "stub"

    assert await service.reconcile_pending(min_age_sec=0) == 0


# ---------------------------------------------------------------- the money arrives too late


@pytest.mark.asyncio
async def test_a_payment_after_the_booking_was_released_is_returned(
    acquirer, committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "acq-late@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    async with committed_conn.transaction():  # the reaper, while the guest is on the page
        await booking_service.release_hold(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    refunds = []
    real = MockAcquiringProvider.refund_charge

    async def counted(self, booking_id, amount, charge_id):
        refunds.append(charge_id)
        return await real(self, booking_id, amount, charge_id)

    monkeypatch.setattr(MockAcquiringProvider, "refund_charge", counted)

    answer = await service.sync_payment(booking_id, conn=committed_conn)

    assert answer["state"] == "returned"
    assert await _booking_status(committed_conn, booking_id) == "cancelled"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded", "refunded"]
    bank = await committed_conn.fetchrow(
        "SELECT refunded::float8 AS refunded, amount::float8 AS amount, refund_id "
        "FROM mock_acquirer_payment WHERE id = $1",
        payment_id,
    )
    assert bank["refunded"] == bank["amount"] and bank["refund_id"]
    assert await _nights(committed_conn, ut) == (0, 0)
    assert await _confirmations(committed_conn, booking_id) == 0

    again = await service.sync_payment(booking_id, conn=committed_conn)
    body, headers = _signed(payment_id)
    await service.receive_notification("mock_acquiring", body, headers)

    assert again["state"] == "returned"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded", "refunded"]
    assert refunds == [payment_id], "a charge is paid back once, however often the news repeats"


@pytest.mark.asyncio
async def test_a_payment_after_the_hold_ran_out_but_before_the_reaper_is_honoured(
    acquirer, committed_conn
) -> None:
    ut = await _seed_unit(committed_conn, "acq-grace@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await committed_conn.execute(
        "UPDATE booking SET hold_expires_at = now() - interval '1 minute' WHERE id = $1",
        booking_id,
    )
    await _bank(committed_conn, payment_id, "CONFIRMED")

    answer = await service.sync_payment(booking_id, conn=committed_conn)

    assert answer["state"] == "confirmed"
    assert await _nights(committed_conn, ut) == (0, 2)


@pytest.mark.asyncio
async def test_a_charge_of_the_wrong_amount_is_returned_and_the_hold_stays(
    acquirer, committed_conn
) -> None:
    ut = await _seed_unit(committed_conn, "acq-amount@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await committed_conn.execute(
        "UPDATE mock_acquirer_payment SET amount = amount - 100, status = 'CONFIRMED' WHERE id = $1",
        payment_id,
    )

    assert (await service.sync_payment(booking_id, conn=committed_conn))["state"] == "returned"

    assert await _booking_status(committed_conn, booking_id) == "hold"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded", "refunded"]
    assert await _confirmations(committed_conn, booking_id) == 0
    retry = await service.start_payment(booking_id, conn=committed_conn)  # may pay again
    assert retry["status"] == "pending" and retry["payment_id"] != payment_id


@pytest.mark.asyncio
async def test_a_conflict_after_the_charge_returns_it_and_closes_the_hold(
    acquirer, committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "acq-conflict@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    _conflict_on_confirm(monkeypatch)

    answer = await service.sync_payment(booking_id, conn=committed_conn)

    assert answer["state"] == "returned"
    assert await _booking_status(committed_conn, booking_id) == "cancelled"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded", "refunded"]
    assert await _nights(committed_conn, ut) == (0, 0)
    with pytest.raises(service.PaymentError, match="not holdable"):
        await service.start_payment(booking_id, conn=committed_conn)


@pytest.mark.asyncio
async def test_a_charge_that_cannot_be_returned_is_left_for_the_sweep(
    acquirer, committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "acq-stuck@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    async with committed_conn.transaction():
        await booking_service.release_hold(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    real = MockAcquiringProvider.refund_charge

    async def bank_is_down(self, booking_id, amount, charge_id):
        raise RuntimeError("the bank is down")

    monkeypatch.setattr(MockAcquiringProvider, "refund_charge", bank_is_down)
    assert (await service.sync_payment(booking_id, conn=committed_conn))["state"] == "stuck"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded"]

    monkeypatch.setattr(MockAcquiringProvider, "refund_charge", real)
    assert await service.reconcile_pending(min_age_sec=0) == 1

    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded", "refunded"]


@pytest.mark.asyncio
async def test_news_arriving_together_confirms_the_booking_once(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-race@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    body, headers = _signed(payment_id)

    await asyncio.gather(
        service.sync_payment(booking_id),
        service.sync_payment(booking_id),
        service.receive_notification("mock_acquiring", body, headers),
        service.reconcile_pending(min_age_sec=0),
    )

    assert await _booking_status(committed_conn, booking_id) == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded"]
    assert await _confirmations(committed_conn, booking_id) == 1
    assert await _nights(committed_conn, ut) == (0, 2)


# ---------------------------------------------------------------- money going back


@pytest.mark.asyncio
async def test_a_confirmed_booking_is_refunded_by_the_charge_id(acquirer, committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "acq-refund@example.com")
    booking_id = await _hold(committed_conn, ut)  # checks in today: past the free window
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    await service.sync_payment(booking_id, conn=committed_conn)
    total = await committed_conn.fetchval(
        "SELECT total_amount::float8 FROM booking WHERE id = $1", booking_id
    )

    out = await service.refund_booking(booking_id, conn=committed_conn)

    expected = round(total * (1 - legal.CANCELLATION_PENALTY_RATE), 2)
    assert out["status"] == "refunded" and out["refunded_amount"] == expected
    bank = await committed_conn.fetchrow(
        "SELECT refunded::float8 AS refunded, refund_id FROM mock_acquirer_payment WHERE id = $1",
        payment_id,
    )
    assert bank["refunded"] == expected and bank["refund_id"]
    assert await _statuses(committed_conn, booking_id) == ["pending", "succeeded", "refunded"]
    assert await _nights(committed_conn, ut) == (0, 0)


@pytest.mark.asyncio
async def test_the_mock_bank_answers_a_repeated_refund_with_the_first(
    acquirer, committed_conn
) -> None:
    ut = await _seed_unit(committed_conn, "acq-refund-twice@example.com")
    booking_id = await _hold(committed_conn, ut)
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    bank = get_payment_provider()

    first = await bank.refund_charge(booking_id, 100.0, payment_id)
    again = await bank.refund_charge(booking_id, 100.0, payment_id)
    too_much = await bank.refund_charge(booking_id, 1e6, "mock_unknown")
    unnamed = await bank.refund_charge(booking_id, 100.0, None)

    assert (first.status, again.status) == ("refunded", "refunded")
    assert first.external_id == again.external_id
    assert (first.amount, again.amount) == (100.0, 100.0)
    assert (
        await committed_conn.fetchval(
            "SELECT refunded::float8 FROM mock_acquirer_payment WHERE id = $1", payment_id
        )
        == 100.0
    )
    assert (too_much.status, unnamed.status) == ("failed", "failed")


@pytest.mark.asyncio
async def test_a_repeated_refund_records_what_the_bank_really_paid_back(
    acquirer, committed_conn
) -> None:
    """The process died after the bank paid everything back, before the shop wrote it down.

    By the time the guest tries again the free window is over, so today's rule would return half;
    the bank has already returned the whole amount, and that is what the books must say."""
    ut = await _seed_unit(committed_conn, "acq-refund-retry@example.com")
    booking_id = await _hold(committed_conn, ut)  # checks in today: a late cancellation
    payment_id = await _open(committed_conn, booking_id)
    await _bank(committed_conn, payment_id, "CONFIRMED")
    await service.sync_payment(booking_id, conn=committed_conn)
    total = await committed_conn.fetchval(
        "SELECT total_amount::float8 FROM booking WHERE id = $1", booking_id
    )
    paid_back = await get_payment_provider().refund_charge(booking_id, total, payment_id)
    assert paid_back.amount == total

    out = await service.refund_booking(booking_id, conn=committed_conn)

    assert out["refunded_amount"] == total and out["free_cancelled"] is True
    booked = await committed_conn.fetchrow(
        "SELECT status, kept_amount::float8 AS kept FROM booking WHERE id = $1", booking_id
    )
    assert (booked["status"], booked["kept"]) == ("refunded", 0.0)
    recorded = await committed_conn.fetchval(
        "SELECT amount::float8 FROM payment WHERE booking_id = $1 AND status = 'refunded'",
        booking_id,
    )
    assert recorded == total


@pytest.mark.asyncio
async def test_a_booking_is_refunded_by_the_provider_that_took_the_money(
    acquirer, committed_conn, monkeypatch
) -> None:
    """Switching PAYMENT_MODE must not strand the bookings paid before the switch."""
    ut = await _seed_unit(committed_conn, "acq-switch@example.com", total_units=2)
    monkeypatch.setattr(settings, "payment_mode", "stub")
    by_stub = await _hold(committed_conn, ut)
    await service.pay_and_confirm(by_stub, conn=committed_conn)
    monkeypatch.setattr(settings, "payment_mode", "mock_acquiring")
    by_mock = await _hold(committed_conn, ut)
    await _bank(committed_conn, await _open(committed_conn, by_mock), "CONFIRMED")
    await service.sync_payment(by_mock, conn=committed_conn)

    # Now the mode is the mock: the stub's booking still goes back through the stub...
    assert (await service.refund_booking(by_stub, conn=committed_conn))["status"] == "refunded"
    # ...and after a switch back to the stub, the mock's goes back through the mock.
    monkeypatch.setattr(settings, "payment_mode", "stub")
    assert (await service.refund_booking(by_mock, conn=committed_conn))["status"] == "refunded"

    refunds = await committed_conn.fetch(
        "SELECT booking_id::text AS booking_id, provider FROM payment WHERE status = 'refunded'"
    )
    assert {r["booking_id"]: r["provider"] for r in refunds} == {
        by_stub: "stub",
        by_mock: "mock_acquiring",
    }
    assert (
        await committed_conn.fetchval(
            "SELECT count(*) FROM mock_acquirer_payment WHERE refund_id IS NOT NULL"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_a_charge_of_a_provider_that_cannot_be_reached_is_not_refunded_by_another(
    acquirer, committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "acq-gone@example.com")
    monkeypatch.setattr(settings, "payment_mode", "stub")
    booking_id = await _hold(committed_conn, ut)
    await service.pay_and_confirm(booking_id, conn=committed_conn)
    await committed_conn.execute(
        "UPDATE payment SET provider = 'tinkoff' WHERE booking_id = $1", booking_id
    )

    with pytest.raises(service.PaymentError, match="cannot refund it now"):
        await service.refund_booking(booking_id, conn=committed_conn)

    assert await _booking_status(committed_conn, booking_id) == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["succeeded"]


@pytest.mark.asyncio
async def test_a_channel_booking_cannot_open_a_payment_page(acquirer, committed_conn) -> None:
    """The OTA collected the money; even a holder of the booking token gets no page for it."""
    ut = await _seed_unit(committed_conn, "acq-channel-page@example.com")
    hold = await booking_service.create_hold(
        committed_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        origin="channel",
        **_guest(),
    )

    with pytest.raises(service.PaymentError, match="settled by its channel"):
        await service.start_payment(hold["id"], conn=committed_conn)

    assert await committed_conn.fetchval("SELECT count(*) FROM mock_acquirer_payment") == 0
    assert await _statuses(committed_conn, hold["id"]) == []


@pytest.mark.asyncio
async def test_a_script_with_no_browser_can_pay_at_once(acquirer, committed_conn) -> None:
    """The demo seed pays through `pay_and_confirm`; the mock lets it, and a refund finds the charge."""
    ut = await _seed_unit(committed_conn, "acq-seed@example.com")
    booking_id = await _hold(committed_conn, ut)

    out = await service.pay_and_confirm(booking_id, conn=committed_conn)

    assert out["status"] == "confirmed"
    assert await _statuses(committed_conn, booking_id) == ["succeeded"]
    charge = await committed_conn.fetchval(
        "SELECT external_id FROM payment WHERE booking_id = $1", booking_id
    )
    assert (
        await committed_conn.fetchval(
            "SELECT status FROM mock_acquirer_payment WHERE id = $1", charge
        )
        == "CONFIRMED"
    )
    again = await get_payment_provider().pay(booking_id, 6000.0)
    assert again.external_id == charge, "idempotent on the booking, as the interface asks"

    refunded = await service.refund_booking(booking_id, conn=committed_conn)

    assert refunded["status"] == "refunded"
    assert await committed_conn.fetchval(
        "SELECT refund_id IS NOT NULL FROM mock_acquirer_payment WHERE id = $1", charge
    )


@pytest.mark.asyncio
async def test_a_channel_booking_never_meets_the_acquirer(acquirer, committed_conn) -> None:
    """The OTA collected the money: no page, no charge, whatever the web checkout uses."""
    ut = await _seed_unit(committed_conn, "acq-channel@example.com")
    hold = await booking_service.create_hold(
        committed_conn,
        unit_type_id=ut,
        checkin=TODAY,
        checkout=TODAY + dt.timedelta(days=2),
        origin="channel",
        **_guest(),
    )

    confirmed = await service.pay_and_confirm(hold["id"], conn=committed_conn)
    refunded = await service.refund_booking(hold["id"], conn=committed_conn)

    assert confirmed["status"] == "confirmed" and refunded["status"] == "refunded"
    providers = await committed_conn.fetch(
        "SELECT DISTINCT provider FROM payment WHERE booking_id = $1", hold["id"]
    )
    assert [r["provider"] for r in providers] == ["stub"]
    assert await committed_conn.fetchval("SELECT count(*) FROM mock_acquirer_payment") == 0


@pytest.mark.asyncio
async def test_the_mode_picks_the_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "payment_mode", "mock_acquiring")
    assert isinstance(get_payment_provider(), MockAcquiringProvider)
    assert service.pays_on_provider_page() is True

    monkeypatch.setattr(settings, "payment_mode", "stub")
    assert service.pays_on_provider_page() is False
    with pytest.raises(service.PaymentError, match="not paid on a payment page"):
        await service.start_payment("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------- over HTTP


def _guest_hold(client: TestClient, ut: str) -> dict:
    guest = _guest()
    response = client.post(
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
    )
    assert response.status_code == 201, response.text
    return response.json()


def _token(hold: dict) -> dict[str, str]:
    return {access.HEADER: hold["access_token"]}


def _page(opened: dict) -> str:
    """The provider's page as a path: the test client talks to the app, not to a host."""
    return urlsplit(opened["payment_url"]).path


@pytest.fixture
def mock_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, "payment_mode", "mock_acquiring")


@pytest.mark.asyncio
async def test_the_guest_pays_on_the_page_and_comes_back_confirmed(
    committed_conn, mock_mode
) -> None:
    ut = await _seed_unit(committed_conn, "acq-http@example.com", published=True)

    with TestClient(create_app()) as client:
        hold = _guest_hold(client, ut)
        pay = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold))
        opened = pay.json()
        page = client.get(_page(opened))
        paid = client.post(_page(opened), data={"action": "pay"}, follow_redirects=False)
        booking = client.get(f"/v1/bookings/{hold['id']}").json()
        synced = client.post(f"/v1/bookings/{hold['id']}/payment/sync", headers=_token(hold))

    assert pay.status_code == 202, pay.text
    assert opened["status"] == "pending"
    assert page.status_code == 200 and "Оплатить" in page.text
    assert "деньги не списываются" in page.text
    assert paid.status_code == 303
    assert paid.headers["location"].endswith(f"/checkout/{hold['id']}/return?result=success")
    assert booking["status"] == "confirmed", (
        "the notification confirmed it before the guest came back"
    )
    assert synced.json()["state"] == "confirmed" and synced.json()["payment_url"] is None
    assert synced.json()["booking"]["status"] == "confirmed", "the checkout needs no second request"
    assert synced.json()["booking"]["code"] == hold["code"]
    assert await _statuses(committed_conn, hold["id"]) == ["pending", "succeeded"]
    assert await _confirmations(committed_conn, hold["id"]) == 1


@pytest.mark.asyncio
async def test_a_second_click_returns_the_open_page(committed_conn, mock_mode) -> None:
    ut = await _seed_unit(committed_conn, "acq-http-twice@example.com", published=True)

    with TestClient(create_app()) as client:
        hold = _guest_hold(client, ut)
        first = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold))
        second = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold))

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json() == second.json()
    assert await committed_conn.fetchval("SELECT count(*) FROM mock_acquirer_payment") == 1


@pytest.mark.asyncio
async def test_a_declined_payment_goes_back_to_the_shop_failed(committed_conn, mock_mode) -> None:
    ut = await _seed_unit(committed_conn, "acq-http-decline@example.com", published=True)

    with TestClient(create_app()) as client:
        hold = _guest_hold(client, ut)
        opened = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold)).json()
        declined = client.post(_page(opened), data={"action": "decline"}, follow_redirects=False)
        status = client.get(f"/v1/bookings/{hold['id']}").json()["status"]
        synced = client.post(f"/v1/bookings/{hold['id']}/payment/sync", headers=_token(hold))
        retried = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold))
        late = client.post(_page(opened), data={"action": "pay"}, follow_redirects=False)
        shown = client.get(_page(opened))

    assert declined.headers["location"].endswith(f"/checkout/{hold['id']}/return?result=fail")
    assert status == "hold"
    assert synced.json() == {"state": "failed", "payment_url": None, "booking": None}
    assert retried.status_code == 202 and retried.json()["payment_id"] != opened["payment_id"]
    assert late.headers["location"].endswith("result=fail"), "a decided payment cannot be changed"
    assert "Платёж отклонён" in shown.text


@pytest.mark.asyncio
async def test_a_payment_whose_notification_is_lost_is_found_when_the_guest_returns(
    committed_conn, mock_mode
) -> None:
    ut = await _seed_unit(committed_conn, "acq-http-silent@example.com", published=True)

    with TestClient(create_app()) as client:
        hold = _guest_hold(client, ut)
        opened = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold)).json()
        silent = client.post(_page(opened), data={"action": "pay_silently"}, follow_redirects=False)
        before = client.get(f"/v1/bookings/{hold['id']}").json()["status"]
        synced = client.post(f"/v1/bookings/{hold['id']}/payment/sync", headers=_token(hold))
        after = client.get(f"/v1/bookings/{hold['id']}").json()["status"]

    assert silent.headers["location"].endswith("result=success")
    assert (before, after) == ("hold", "confirmed")
    assert synced.json()["state"] == "confirmed"


@pytest.mark.asyncio
async def test_the_notification_address_checks_the_signature(committed_conn, mock_mode) -> None:
    ut = await _seed_unit(committed_conn, "acq-http-notify@example.com", published=True)
    path = mock_acquirer.NOTIFICATION_PATH

    with TestClient(create_app()) as client:
        hold = _guest_hold(client, ut)
        opened = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold)).json()
        await_bank = opened["payment_id"]
        body, headers = _signed(await_bank)
        forged = client.post(path, content=body, headers={SIGNATURE: "0" * 64})
        unsigned = client.post(path, content=body)
        genuine = client.post(path, content=body, headers=headers)
        elsewhere = client.post(
            "/v1/payments/notifications/yookassa", content=body, headers=headers
        )
        status = client.get(f"/v1/bookings/{hold['id']}").json()["status"]

    assert (forged.status_code, unsigned.status_code) == (400, 400)
    assert genuine.status_code == 200 and genuine.json() == {"ok": True}
    assert elsewhere.status_code == 404
    assert status == "hold", "the bank had not paid: a genuine notification alone changes nothing"


@pytest.mark.asyncio
async def test_syncing_a_payment_needs_the_booking_token(committed_conn, mock_mode) -> None:
    ut = await _seed_unit(
        committed_conn, "acq-http-token@example.com", total_units=2, published=True
    )

    with TestClient(create_app()) as client:
        mine = _guest_hold(client, ut)
        theirs = _guest_hold(client, ut)
        bare = client.post(f"/v1/bookings/{mine['id']}/payment/sync")
        other = client.post(f"/v1/bookings/{mine['id']}/payment/sync", headers=_token(theirs))
        own = client.post(f"/v1/bookings/{mine['id']}/payment/sync", headers=_token(mine))

    assert bare.status_code == 422
    assert other.status_code == 404
    assert own.status_code == 200 and own.json()["state"] == "none"


@pytest.mark.asyncio
async def test_the_provider_pages_exist_only_for_the_mock(committed_conn, monkeypatch) -> None:
    ut = await _seed_unit(committed_conn, "acq-http-pages@example.com", published=True)
    path = "/mock-acquirer/pay/mock_nothing"

    with TestClient(create_app()) as client:
        monkeypatch.setattr(settings, "payment_mode", "stub")
        asleep = client.get(path), client.post(path, data={"action": "pay"})
        monkeypatch.setattr(settings, "payment_mode", "mock_acquiring")
        unknown = client.get(path)
        hold = _guest_hold(client, ut)
        opened = client.post(f"/v1/bookings/{hold['id']}/pay", headers=_token(hold)).json()
        odd = client.post(_page(opened), data={"action": "keep-the-change"}, follow_redirects=False)

    assert [r.status_code for r in asleep] == [404, 404]
    assert unknown.status_code == 404
    assert odd.status_code == 400


def test_the_checkout_learns_whether_money_moves(monkeypatch) -> None:
    client = TestClient(create_app())  # no lifespan: the answer needs no database
    for mode, test_mode in [("stub", True), ("mock_acquiring", True), ("tinkoff", False)]:
        monkeypatch.setattr(settings, "payment_mode", mode)
        assert client.get("/v1/config").json()["payment_test_mode"] is test_mode
