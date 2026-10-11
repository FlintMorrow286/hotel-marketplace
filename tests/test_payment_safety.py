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


@pytest.mark.asyncio
async def test_a_conflict_after_the_charge_returns_it_and_leaves_the_hold(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "safe3@example.com")
    booking_id = await _hold(committed_conn, ut)
    provider = RecordingProvider()
    _use(monkeypatch, provider)

    async def conflict(conn, body, **kwargs):
        raise asyncpg.SerializationError("could not serialize access")

    monkeypatch.setattr(service, "serializable", conflict)
    with pytest.raises(asyncpg.SerializationError):
        await service.pay_and_confirm(booking_id, conn=committed_conn)
    monkeypatch.undo()

    assert provider.verbs == ["pay", "refund"]
    status = await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
    assert status == "hold"


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
async def test_a_returned_earlier_charge_does_not_stand_in_for_the_refund(
    committed_conn, monkeypatch
) -> None:
    ut = await _seed_unit(committed_conn, "safe5@example.com")
    booking_id = await _hold(committed_conn, ut)
    provider = RecordingProvider()
    _use(monkeypatch, provider)

    # First attempt: charged, then the confirmation conflicts and the charge is returned.
    real = service.serializable

    async def conflict(conn, body, **kwargs):
        raise asyncpg.SerializationError("could not serialize access")

    monkeypatch.setattr(service, "serializable", conflict)
    with pytest.raises(asyncpg.SerializationError):
        await service.pay_and_confirm(booking_id, conn=committed_conn)
    monkeypatch.setattr(service, "serializable", real)

    # Second attempt goes through; cancelling it later must refund this charge.
    await service.pay_and_confirm(booking_id, conn=committed_conn)
    await service.refund_booking(booking_id, conn=committed_conn)

    assert provider.verbs == ["pay", "refund", "pay", "refund"]
    assert await _statuses(committed_conn, booking_id) == [
        "succeeded",
        "refunded",
        "succeeded",
        "refunded",
    ]


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
