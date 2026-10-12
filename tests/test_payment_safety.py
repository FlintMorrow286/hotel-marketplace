"""Money safety: a charge never outlives its booking, a refund is never made twice.

The provider is faked with a recorder so the tests see exactly which calls the
booking core made, and in which order.
"""

from __future__ import annotations

import datetime as dt

import asyncpg
import pytest

from app.config import legal
from app.modules.admin.report import commission_report
from app.modules.booking import service as booking_service
from app.modules.payment import service
from app.modules.payment.provider import PaymentProvider, PaymentResult
from tests.test_payment import TODAY, _guest, _hold, _seed_unit

FAR = 30  # nights ahead: well before the free-cancellation deadline


class RecordingProvider(PaymentProvider):
    """Records every call; can be told to misbehave."""

    def __init__(self, *, on_pay=None, refund_raises: bool = False) -> None:
        self.calls: list[tuple[str, float]] = []
        self._on_pay = on_pay
        self._refund_raises = refund_raises

    @property
    def name(self) -> str:
        return "stub"  # payment.provider only admits known names

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        self.calls.append(("pay", amount))
        if self._on_pay is not None:
            await self._on_pay()
        return PaymentResult("succeeded", external_id="rec-pay")

    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        self.calls.append(("refund", amount))
        if self._refund_raises:
            raise RuntimeError("provider is down")
        return PaymentResult("refunded", external_id="rec-refund")

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


def _use(monkeypatch, provider: PaymentProvider) -> None:
    monkeypatch.setattr(service, "get_payment_provider", lambda: provider)


async def _statuses(conn: asyncpg.Connection, booking_id: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT status FROM payment WHERE booking_id = $1 ORDER BY created_at", booking_id
    )
    return [r["status"] for r in rows]


async def _far_booking(conn: asyncpg.Connection, email: str, tz: str | None = None) -> str:
    """A confirmed booking whose check-in is far enough ahead to cancel for free."""
    ut = await _seed_unit(conn, email)
    if tz is not None:
        await conn.execute(
            "UPDATE property SET timezone = $2 "
            "WHERE id = (SELECT property_id FROM unit_type WHERE id = $1)",
            ut,
            tz,
        )
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series(($2::date + 29)::date,"
        " ($3::date - 1)::date, '1 day') AS d(date)"
        " ON CONFLICT (unit_type_id, date) DO NOTHING",
        ut,
        TODAY,
        TODAY + dt.timedelta(days=FAR + 3),
    )
    booking = await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=TODAY + dt.timedelta(days=FAR),
        checkout=TODAY + dt.timedelta(days=FAR + 2),
        idempotency_key=f"far-{email}",
        **_guest(),
    )
    await service.pay_and_confirm(booking["id"], conn=conn)
    return booking["id"]


# ---------------------------------------------------------------- a charge always has a booking


@pytest.mark.asyncio
async def test_charge_is_returned_when_the_booking_cannot_be_confirmed(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "safe1@example.com")
    booking_id = await _hold(committed_conn, ut)

    async def reaped_while_the_bank_answers() -> None:
        async with committed_conn.transaction():
            await booking_service.release_hold(committed_conn, booking_id)

    provider = RecordingProvider(on_pay=reaped_while_the_bank_answers)
    _use(monkeypatch, provider)

    with pytest.raises(service.PaymentError, match="your payment was returned"):
        await service.pay_and_confirm(booking_id, conn=committed_conn)

    assert provider.verbs == ["pay", "refund"]
    assert provider.calls[0][1] == provider.calls[1][1]
    assert await _statuses(committed_conn, booking_id) == ["succeeded", "refunded"]
    status = await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    assert status == "cancelled"


@pytest.mark.asyncio
async def test_a_charge_that_cannot_be_returned_stays_on_record(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "safe2@example.com")
    booking_id = await _hold(committed_conn, ut)

    async def reaped_while_the_bank_answers() -> None:
        async with committed_conn.transaction():
            await booking_service.release_hold(committed_conn, booking_id)

    provider = RecordingProvider(on_pay=reaped_while_the_bank_answers, refund_raises=True)
    _use(monkeypatch, provider)

    with pytest.raises(service.PaymentError, match="support will refund"):
        await service.pay_and_confirm(booking_id, conn=committed_conn)

    # The row is how somebody finds the money: succeeded, nothing refunded after it.
    assert await _statuses(committed_conn, booking_id) == ["succeeded"]


def _conflict_on_confirm(monkeypatch) -> None:
    """Make the confirmation transaction, and only it, fail with a serialization conflict."""
    real = service.serializable

    async def conflict(conn, body, **kwargs):
        if getattr(body, "__name__", "") == "_confirm":
            raise asyncpg.SerializationError("could not serialize access")
        return await real(conn, body, **kwargs)

    monkeypatch.setattr(service, "serializable", conflict)


@pytest.mark.asyncio
async def test_a_conflict_after_the_charge_returns_it_and_closes_the_hold(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "safe3@example.com")
    booking_id = await _hold(committed_conn, ut)
    provider = RecordingProvider()
    _use(monkeypatch, provider)
    _conflict_on_confirm(monkeypatch)

    with pytest.raises(service.PaymentError, match="your payment was returned"):
        await service.pay_and_confirm(booking_id, conn=committed_conn)

    assert provider.verbs == ["pay", "refund"]
    status = await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    assert status == "cancelled"
    held = await committed_conn.fetchval(
        "SELECT hold FROM inventory_day WHERE unit_type_id = $1 AND date = $2", ut, TODAY
    )
    assert held == 0


@pytest.mark.asyncio
async def test_a_returned_charge_cannot_be_followed_by_a_second_one_on_the_same_booking(
    committed_conn, monkeypatch
) -> None:
    """A provider idempotent on booking_id would answer the returned charge's success again."""
    ut = await _seed_unit(committed_conn, "safe3b@example.com")
    booking_id = await _hold(committed_conn, ut)
    provider = RecordingProvider()
    _use(monkeypatch, provider)
    _conflict_on_confirm(monkeypatch)
    with pytest.raises(service.PaymentError):
        await service.pay_and_confirm(booking_id, conn=committed_conn)

    with pytest.raises(service.PaymentError, match="not holdable"):
        await service.pay_and_confirm(booking_id, conn=committed_conn)

    assert provider.verbs == ["pay", "refund"], "the provider must not be asked to charge again"


@pytest.mark.asyncio
async def test_a_charge_that_cannot_be_written_down_is_still_returned(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "safe3c@example.com")
    booking_id = await _hold(committed_conn, ut)
    provider = RecordingProvider()
    _use(monkeypatch, provider)
    real_record = service._record_payment

    async def ledger_down_for_charges(conn, booking, name, amount, status, external_id):
        if status == "succeeded":
            raise RuntimeError("ledger is down")
        return await real_record(conn, booking, name, amount, status, external_id)

    monkeypatch.setattr(service, "_record_payment", ledger_down_for_charges)

    with pytest.raises(service.PaymentError, match="your payment was returned"):
        await service.pay_and_confirm(booking_id, conn=committed_conn)

    assert provider.verbs == ["pay", "refund"]


# ---------------------------------------------------------------- a refund is made once


@pytest.mark.asyncio
async def test_a_refund_that_died_midway_is_finished_not_repeated(
    committed_conn, monkeypatch
) -> None:
    booking_id = await _far_booking(committed_conn, "safe4@example.com")
    provider = RecordingProvider()
    _use(monkeypatch, provider)

    real = service.serializable
    crash = {"pending": True}

    async def dies_once(conn, body, **kwargs):
        if crash["pending"]:
            crash["pending"] = False
            raise RuntimeError("connection lost after the provider answered")
        return await real(conn, body, **kwargs)

    monkeypatch.setattr(service, "serializable", dies_once)

    with pytest.raises(RuntimeError):
        await service.refund_booking(booking_id, conn=committed_conn)
    status = await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    assert status == "confirmed"
    assert provider.verbs == ["refund"]

    out = await service.refund_booking(booking_id, conn=committed_conn)

    assert out["status"] == "refunded"
    assert provider.verbs == ["refund"], "the provider must not be paid back twice"
    assert out["refunded_amount"] == provider.calls[0][1]
    assert await _statuses(committed_conn, booking_id) == ["succeeded", "refunded"]


@pytest.mark.asyncio
async def test_a_refund_on_record_must_be_newer_than_the_last_charge(committed_conn) -> None:
    booking_id = await _far_booking(committed_conn, "safe5@example.com")
    await committed_conn.execute("DELETE FROM payment WHERE booking_id = $1", booking_id)
    base = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

    async def row(status: str, minutes: int) -> None:
        await committed_conn.execute(
            "INSERT INTO payment (booking_id, provider, amount, status, created_at) "
            "VALUES ($1, 'stub', 100, $2, $3)",
            booking_id,
            status,
            base + dt.timedelta(minutes=minutes),
        )

    await row("succeeded", 0)
    await row("refunded", 1)
    await row("succeeded", 2)
    assert await service._refund_on_record(committed_conn, booking_id) is None

    await row("refunded", 3)
    found = await service._refund_on_record(committed_conn, booking_id)
    assert found is not None and found["amount"] == 100


@pytest.mark.asyncio
async def test_a_refund_that_cannot_be_written_down_still_closes_the_booking(
    committed_conn, monkeypatch
) -> None:
    """Left confirmed, the booking would invite a second refund call on the next try."""
    booking_id = await _far_booking(committed_conn, "safe5b@example.com")
    provider = RecordingProvider()
    _use(monkeypatch, provider)
    real_record = service._record_payment

    async def ledger_down_for_refunds(conn, booking, name, amount, status, external_id):
        if status == "refunded":
            raise RuntimeError("ledger is down")
        return await real_record(conn, booking, name, amount, status, external_id)

    monkeypatch.setattr(service, "_record_payment", ledger_down_for_refunds)

    out = await service.refund_booking(booking_id, conn=committed_conn)

    assert out["status"] == "refunded"
    with pytest.raises(service.PaymentError, match="status=refunded"):
        await service.refund_booking(booking_id, conn=committed_conn)
    assert provider.verbs == ["refund"]


@pytest.mark.asyncio
async def test_a_refund_record_that_fails_once_is_written_on_the_retry(
    committed_conn, monkeypatch
) -> None:
    booking_id = await _far_booking(committed_conn, "safe5c@example.com")
    provider = RecordingProvider()
    _use(monkeypatch, provider)
    real_record = service._record_payment
    seen = {"refunded": 0}

    async def blip(conn, booking, name, amount, status, external_id):
        if status == "refunded":
            seen["refunded"] += 1
            if seen["refunded"] == 1:
                raise RuntimeError("blip")
        return await real_record(conn, booking, name, amount, status, external_id)

    monkeypatch.setattr(service, "_record_payment", blip)

    await service.refund_booking(booking_id, conn=committed_conn)

    assert await _statuses(committed_conn, booking_id) == ["succeeded", "refunded"]


# ---------------------------------------------------------------- the deadline is the property's


def test_today_follows_the_calendar_of_the_property() -> None:
    assert service._today_at("Pacific/Kiritimati") > service._today_at("Pacific/Pago_Pago")


def test_an_unknown_zone_falls_back_to_utc() -> None:
    before = dt.datetime.now(dt.UTC).date()
    got = service._today_at("Nowhere/Land")
    assert got in {before, dt.datetime.now(dt.UTC).date()}


@pytest.mark.asyncio
async def test_the_free_window_is_read_at_the_property_not_at_the_server(
    committed_conn,
) -> None:
    """Same check-in date, two time zones 25 hours apart: only one is still free."""
    checkin = service._today_at("Pacific/Pago_Pago") + dt.timedelta(days=1)
    outcomes = {}
    for tz in ("Pacific/Pago_Pago", "Pacific/Kiritimati"):
        ut = await _seed_unit(committed_conn, f"tz-{tz[-4:].lower()}@example.com")
        await committed_conn.execute(
            "UPDATE property SET timezone = $2 "
            "WHERE id = (SELECT property_id FROM unit_type WHERE id = $1)",
            ut,
            tz,
        )
        booking = await booking_service.create_hold(
            committed_conn,
            unit_type_id=ut,
            checkin=checkin,
            checkout=checkin + dt.timedelta(days=1),
            idempotency_key=f"tz-{tz}",
            **_guest(),
        )
        await service.pay_and_confirm(booking["id"], conn=committed_conn)
        outcomes[tz] = (await service.refund_booking(booking["id"], conn=committed_conn))[
            "free_cancelled"
        ]

    assert outcomes == {"Pacific/Pago_Pago": True, "Pacific/Kiritimati": False}


# ---------------------------------------------------------------- a stay that is over is not refunded


async def _confirmed_stay(conn: asyncpg.Connection, email: str, first_night, nights: int) -> str:
    """A confirmed booking moved to the given dates (the stay itself is what matters here)."""
    ut = await _seed_unit(conn, email)
    booking_id = await _hold(conn, ut)
    await service.pay_and_confirm(booking_id, conn=conn)
    await conn.execute(
        "UPDATE booking SET checkin_date = $2, checkout_date = $3 WHERE id = $1",
        booking_id,
        first_night,
        first_night + dt.timedelta(days=nights),
    )
    return booking_id


@pytest.mark.asyncio
async def test_a_stay_that_is_over_cannot_be_refunded(committed_conn, monkeypatch) -> None:
    today = service._today_at("Europe/Simferopol")
    booking_id = await _confirmed_stay(
        committed_conn, "over1@example.com", today - dt.timedelta(days=5), 2
    )
    provider = RecordingProvider()
    _use(monkeypatch, provider)

    with pytest.raises(service.PaymentError, match="stay is already over"):
        await service.refund_booking(booking_id, conn=committed_conn)

    assert provider.calls == [], "the provider must not be asked to pay anything back"
    status = await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    assert status == "confirmed"


@pytest.mark.asyncio
async def test_the_checkout_day_is_the_end_of_the_stay(committed_conn, monkeypatch) -> None:
    today = service._today_at("Europe/Simferopol")
    last_night_was_yesterday = await _confirmed_stay(
        committed_conn, "over2@example.com", today - dt.timedelta(days=2), 2
    )
    last_night_is_tonight = await _confirmed_stay(
        committed_conn, "over3@example.com", today - dt.timedelta(days=1), 2
    )
    provider = RecordingProvider()
    _use(monkeypatch, provider)

    with pytest.raises(service.PaymentError, match="stay is already over"):
        await service.refund_booking(last_night_was_yesterday, conn=committed_conn)
    out = await service.refund_booking(last_night_is_tonight, conn=committed_conn)

    assert out["status"] == "refunded" and out["free_cancelled"] is False
    assert provider.verbs == ["refund"]


@pytest.mark.asyncio
async def test_a_refund_the_provider_already_made_is_finished_even_after_the_stay(
    committed_conn, monkeypatch
) -> None:
    """The call died after the provider paid back; by the retry the stay has ended."""
    today = service._today_at("Europe/Simferopol")
    booking_id = await _confirmed_stay(
        committed_conn, "over4@example.com", today - dt.timedelta(days=5), 2
    )
    await committed_conn.execute(
        "INSERT INTO payment (booking_id, provider, amount, status) VALUES ($1, 'stub', 3000, 'refunded')",
        booking_id,
    )
    provider = RecordingProvider()
    _use(monkeypatch, provider)

    out = await service.refund_booking(booking_id, conn=committed_conn)

    assert out["status"] == "refunded"
    assert out["refunded_amount"] == 3000
    assert provider.calls == []


# ---------------------------------------------------------------- commission follows the money


@pytest.mark.asyncio
async def test_late_cancellation_accrues_commission_on_the_kept_share(committed_conn) -> None:
    ut = await _seed_unit(committed_conn, "safe6@example.com")
    booking_id = await _hold(committed_conn, ut)  # starts today: past the free window
    await service.pay_and_confirm(booking_id, conn=committed_conn)
    before = await committed_conn.fetchrow(
        "SELECT total_amount::float8 AS total, commission_amt::float8 AS commission "
        "FROM booking WHERE id = $1",
        booking_id,
    )

    out = await service.refund_booking(booking_id, conn=committed_conn)

    row = await committed_conn.fetchrow(
        "SELECT kept_amount::float8 AS kept, commission_amt::float8 AS commission, "
        "commission_status FROM booking WHERE id = $1",
        booking_id,
    )
    assert out["free_cancelled"] is False
    assert row["kept"] == round(before["total"] - out["refunded_amount"], 2)
    assert row["commission_status"] == "accrued"
    assert row["commission"] == round(before["commission"] * row["kept"] / before["total"], 2)

    report = await commission_report(committed_conn)
    partner = next(p for p in report["partners"] if p["partner_email"] == "safe6@example.com")
    assert partner["gross"] == row["kept"]
    assert partner["commission"] == row["commission"]


@pytest.mark.asyncio
async def test_free_cancellation_voids_the_commission(committed_conn) -> None:
    booking_id = await _far_booking(committed_conn, "safe7@example.com")

    out = await service.refund_booking(booking_id, conn=committed_conn)

    row = await committed_conn.fetchrow(
        "SELECT kept_amount::float8 AS kept, commission_status FROM booking WHERE id = $1",
        booking_id,
    )
    assert out["free_cancelled"] is True
    assert row["kept"] == 0
    assert row["commission_status"] == "void"
    report = await commission_report(committed_conn)
    assert all(p["partner_email"] != "safe7@example.com" for p in report["partners"])


@pytest.mark.asyncio
async def test_penalty_without_commission_when_the_business_says_so(
    committed_conn, monkeypatch
) -> None:
    monkeypatch.setattr(legal, "COMMISSION_ON_PENALTY", False)
    ut = await _seed_unit(committed_conn, "safe8@example.com")
    booking_id = await _hold(committed_conn, ut)
    await service.pay_and_confirm(booking_id, conn=committed_conn)

    await service.refund_booking(booking_id, conn=committed_conn)

    row = await committed_conn.fetchrow(
        "SELECT kept_amount::float8 AS kept, commission_status FROM booking WHERE id = $1",
        booking_id,
    )
    assert row["kept"] > 0
    assert row["commission_status"] == "void"
