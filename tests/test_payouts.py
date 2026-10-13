"""Settlements with partners: what a booking earns, when it is payable, and the payout's life.

The money rules are written out by hand (10 000 with 12% commission is 8 800 to the partner) so that
a wrong formula cannot hide behind a test that reads the answer from the code under test. The
last test checks the whole ledger against an independent computation over random sequences of
bookings, refunds and payouts.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.config import legal
from app.config.checks import ConfigError, check_config
from app.config.settings import settings
from app.modules.admin import report
from app.modules.booking import service as booking_service
from app.modules.payment import service as payment_service
from app.modules.payout import service
from app.utils.tz import today_at
from tests._payouts import (
    ADMIN,
    add_property,
    booking,
    lines_of,
    partner_with_property,
    payout_count,
    payout_row,
    refund_free,
    refund_late,
)
from tests.test_startup_checks import _settings

D = Decimal


@pytest.fixture
def rule(monkeypatch):
    """Set the payout rule: rule("payment", delay=1, minimum=5000)."""

    def set_rule(eligible_from: str = "checkout", delay: int = 0, minimum: float = 0) -> None:
        monkeypatch.setattr(legal, "PAYOUT_ELIGIBLE_FROM", eligible_from)
        monkeypatch.setattr(legal, "PAYOUT_DELAY_DAYS", delay)
        monkeypatch.setattr(legal, "PAYOUT_MIN_AMOUNT", minimum)

    return set_rule


async def _real_booking(
    conn: asyncpg.Connection, p: dict, *, start_in: int = 0, nights: int = 2
) -> str:
    """A web booking made and paid through the booking code (stub payment), 3000 a night."""
    checkin = today_at(p["tz"]) + dt.timedelta(days=start_in)
    held = await booking_service.create_hold(
        conn,
        unit_type_id=p["unit_type_id"],
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=nights),
        guest_name="Иван Гость",
        guest_email=f"g{uuid.uuid4().hex[:8]}@example.com",
        guest_phone="+79991234567",
        idempotency_key=f"po-{uuid.uuid4()}",
    )
    await payment_service.pay_and_confirm(held["id"], conn=conn)
    return held["id"]


async def _pay_out(conn: asyncpg.Connection, payout_id: str, reference: str = "PP-1") -> None:
    await service.approve(conn, payout_id, ADMIN)
    await service.mark_paid(conn, payout_id, reference, ADMIN)


async def _only_balance(conn: asyncpg.Connection, partner_id: str) -> dict:
    (row,) = await service.balances(conn, partner_id)
    return row


# ---------------------------------------------------------------- what a booking earns


@pytest.mark.asyncio
async def test_a_confirmed_web_booking_earns_the_total_minus_the_commission(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-basic@example.com")
    b = await booking(committed_conn, p, total="10000.00", rate="0.12")

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    (line,) = await lines_of(committed_conn, draft_id)
    assert (line["booking_id"], line["kind"]) == (b, "booking")
    assert (line["gross_amount"], line["commission_amount"], line["net_amount"]) == (
        D("10000.00"),
        D("1200.00"),
        D("8800.00"),
    )
    row = await payout_row(committed_conn, draft_id)
    assert (row["status"], row["currency"], row["line_count"]) == ("draft", "RUB", 1)
    assert (row["gross_amount"], row["commission_amount"], row["net_amount"]) == (
        D("10000.00"),
        D("1200.00"),
        D("8800.00"),
    )


@pytest.mark.asyncio
async def test_a_paid_status_earns_like_a_confirmed_one(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-paidstatus@example.com")
    await booking(committed_conn, p, status="paid", total="2000.00", rate="0.10")

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    assert (await payout_row(committed_conn, draft_id))["net_amount"] == D("1800.00")


@pytest.mark.asyncio
async def test_kopecks_are_kept_exactly(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-kopecks@example.com")
    await booking(committed_conn, p, total="3333.33", commission="399.99")
    await booking(committed_conn, p, total="0.07", commission="0.01")

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    row = await payout_row(committed_conn, draft_id)
    assert (row["gross_amount"], row["commission_amount"], row["net_amount"]) == (
        D("3333.40"),
        D("400.00"),
        D("2933.40"),
    )
    detail = await service.payout_detail(committed_conn, draft_id)
    assert detail["net_amount"] == 2933.4 and sorted(
        ln["net_amount"] for ln in detail["lines"]
    ) == [
        0.06,
        2933.34,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["hold", "cancelled", "failed", "conflict", "no_show"])
async def test_a_booking_that_was_never_paid_earns_nothing(committed_conn, status) -> None:
    p = await partner_with_property(committed_conn, f"po-never-{status}@example.com")
    await booking(committed_conn, p, status=status, paid=False)

    assert await service.generate(committed_conn, p["partner_id"]) == []
    assert await service.balances(committed_conn, p["partner_id"]) == []


@pytest.mark.asyncio
async def test_a_channel_booking_earns_nothing_the_ota_collected_the_money(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-channel@example.com")
    await booking(committed_conn, p, origin="channel")

    assert await service.generate(committed_conn, p["partner_id"]) == []
    assert await service.balances(committed_conn, p["partner_id"]) == []


@pytest.mark.asyncio
async def test_a_free_cancellation_earns_nothing(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-free@example.com")
    b = await booking(committed_conn, p)
    await refund_free(committed_conn, b)

    assert await service.generate(committed_conn, p["partner_id"]) == []


@pytest.mark.asyncio
async def test_a_late_cancellation_earns_the_kept_part_less_its_commission(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-late@example.com")
    b = await booking(committed_conn, p, total="10000.00", rate="0.12")  # commission 1 200
    await refund_late(committed_conn, b, "5000.00")  # commission follows: 600

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    (line,) = await lines_of(committed_conn, draft_id)
    assert (line["gross_amount"], line["commission_amount"], line["net_amount"]) == (
        D("5000.00"),
        D("600.00"),
        D("4400.00"),
    )


@pytest.mark.asyncio
async def test_a_late_cancellation_without_commission_on_the_penalty_pays_all_of_it(
    committed_conn,
) -> None:
    """COMMISSION_ON_PENALTY off: refund_booking marks the commission void and keeps the amount."""
    p = await partner_with_property(committed_conn, "po-late-nofee@example.com")
    await booking(committed_conn, p, status="refunded", kept="5000.00", commission_status="void")

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    (line,) = await lines_of(committed_conn, draft_id)
    assert (line["gross_amount"], line["commission_amount"], line["net_amount"]) == (
        D("5000.00"),
        D("0.00"),
        D("5000.00"),
    )


@pytest.mark.asyncio
async def test_the_ledger_agrees_with_what_the_real_refund_writes(committed_conn, rule) -> None:
    """Not the fixture's idea of a late cancellation: the real refund, the real commission rescale."""
    rule("payment")
    p = await partner_with_property(committed_conn, "po-real-refund@example.com")
    b = await _real_booking(committed_conn, p)  # 2 nights x 3000, commission 12%: 720
    out = await payment_service.refund_booking(b, conn=committed_conn)  # checks in today: late
    assert out["free_cancelled"] is False and out["refunded_amount"] == 3000.0

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    (line,) = await lines_of(committed_conn, draft_id)
    assert (line["gross_amount"], line["commission_amount"], line["net_amount"]) == (
        D("3000.00"),
        D("360.00"),
        D("2640.00"),
    )


@pytest.mark.asyncio
async def test_a_payout_agrees_with_the_commission_report(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-report@example.com")
    await booking(committed_conn, p, total="10000.00", rate="0.12")
    late = await booking(committed_conn, p, total="8000.00", rate="0.10")
    await refund_late(committed_conn, late, "4000.00")
    free = await booking(committed_conn, p, total="5000.00")
    await refund_free(committed_conn, free)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)

    shown = await report.commission_report(committed_conn)

    (mine,) = [r for r in shown["partners"] if r["partner_id"] == p["partner_id"]]
    row = await payout_row(committed_conn, draft_id)
    assert (float(row["gross_amount"]), float(row["commission_amount"])) == (
        mine["gross"],
        mine["commission"],
    )
    assert (mine["gross"], mine["commission"]) == (14000.0, 1600.0)


# ---------------------------------------------------------------- when it is payable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("eligible_from", "delay", "checkout_in", "payable"),
    [
        ("checkout", 0, 0, True),  # the day of the checkout itself
        ("checkout", 0, 1, False),
        ("checkout", 2, -1, False),
        ("checkout", 2, -2, True),
        ("checkin", 0, 2, True),  # checkin is today
        ("checkin", 0, 3, False),
        ("checkin", 1, 2, False),
        ("checkin", 1, 1, True),
        ("payment", 0, 5, True),  # paid today
        ("payment", 1, 5, False),
    ],
)
async def test_the_rule_decides_when_a_booking_is_payable(
    committed_conn, rule, eligible_from, delay, checkout_in, payable
) -> None:
    rule(eligible_from, delay)
    p = await partner_with_property(committed_conn, f"po-rule-{uuid.uuid4().hex[:6]}@example.com")
    await booking(committed_conn, p, checkout_in=checkout_in, nights=2)

    balance = await _only_balance(committed_conn, p["partner_id"])
    made = await service.generate(committed_conn, p["partner_id"])

    assert (balance["available"], balance["upcoming"]) == (
        (8800.0, 0.0) if payable else (0.0, 8800.0)
    )
    assert bool(made) is payable


@pytest.mark.asyncio
async def test_the_property_calendar_decides_not_the_servers(committed_conn) -> None:
    """25 hours apart: two properties never share a date. The same checkout day is past at one."""
    ahead = await partner_with_property(
        committed_conn,
        "po-tz-ahead@example.com",
        tz="Pacific/Kiritimati",  # UTC+14
    )
    behind = await partner_with_property(
        committed_conn,
        "po-tz-behind@example.com",
        tz="Pacific/Pago_Pago",  # UTC-11
    )
    day = today_at("Pacific/Pago_Pago") + dt.timedelta(days=1)
    for p in (ahead, behind):
        b = await booking(committed_conn, p)
        await committed_conn.execute(
            "UPDATE booking SET checkout_date = $2::date, checkin_date = $2::date - 2 WHERE id = $1",
            b,
            day,
        )

    ahead_balance = await _only_balance(committed_conn, ahead["partner_id"])
    behind_balance = await _only_balance(committed_conn, behind["partner_id"])

    assert today_at("Pacific/Kiritimati") >= day > today_at("Pacific/Pago_Pago")
    assert (ahead_balance["available"], ahead_balance["upcoming"]) == (8800.0, 0.0)
    assert (behind_balance["available"], behind_balance["upcoming"]) == (0.0, 8800.0)


@pytest.mark.asyncio
async def test_a_booking_becomes_payable_as_its_date_passes_without_anyone_touching_it(
    committed_conn,
) -> None:
    p = await partner_with_property(committed_conn, "po-time@example.com")
    b = await booking(committed_conn, p, checkout_in=2)
    assert await service.generate(committed_conn, p["partner_id"]) == []

    await committed_conn.execute(
        "UPDATE booking SET checkin_date = checkin_date - 3, checkout_date = checkout_date - 3 "
        "WHERE id = $1",
        b,
    )

    assert len(await service.generate(committed_conn, p["partner_id"])) == 1


@pytest.mark.asyncio
async def test_the_rule_is_read_when_it_is_asked_not_when_the_module_loads(
    committed_conn, rule
) -> None:
    p = await partner_with_property(committed_conn, "po-liverule@example.com")
    await booking(committed_conn, p, checkout_in=2)  # checks in today, leaves in two days
    assert (await _only_balance(committed_conn, p["partner_id"]))["upcoming"] == 8800.0

    rule("checkin")

    assert (await _only_balance(committed_conn, p["partner_id"]))["available"] == 8800.0


# ---------------------------------------------------------------- building drafts


@pytest.mark.asyncio
async def test_a_draft_holds_the_payable_bookings_and_leaves_the_rest(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-draft@example.com")
    due1 = await booking(committed_conn, p, total="10000.00")
    due2 = await booking(committed_conn, p, total="6000.00", rate="0.10")
    await booking(committed_conn, p, checkout_in=4)  # still to come

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    assert {ln["booking_id"] for ln in await lines_of(committed_conn, draft_id)} == {due1, due2}
    row = await payout_row(committed_conn, draft_id)
    assert (row["net_amount"], row["line_count"]) == (D("8800.00") + D("5400.00"), 2)


@pytest.mark.asyncio
async def test_generating_twice_changes_nothing(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-idem@example.com")
    await booking(committed_conn, p)
    await booking(committed_conn, p, total="4000.00")

    (first,) = await service.generate(committed_conn, p["partner_id"])
    before = [tuple(r) for r in await lines_of(committed_conn, first)]
    (second,) = await service.generate(committed_conn, p["partner_id"])

    assert second == first, "the draft is rebuilt in place, its address stays"
    assert [tuple(r) for r in await lines_of(committed_conn, second)] == before
    assert await payout_count(committed_conn) == 1


@pytest.mark.asyncio
async def test_a_draft_is_rebuilt_not_added_to(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-rebuild@example.com")
    await booking(committed_conn, p, total="10000.00")
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    await booking(committed_conn, p, total="2000.00", rate="0.10")
    (again,) = await service.generate(committed_conn, p["partner_id"])

    assert again == draft_id
    row = await payout_row(committed_conn, draft_id)
    assert (row["net_amount"], row["line_count"]) == (D("10600.00"), 2)
    assert len(await lines_of(committed_conn, draft_id)) == 2


@pytest.mark.asyncio
async def test_a_draft_whose_money_is_gone_is_deleted(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-gone@example.com")
    b = await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await refund_free(committed_conn, b)

    assert await service.generate(committed_conn, p["partner_id"]) == []

    assert await committed_conn.fetchval("SELECT count(*) FROM payout WHERE id = $1", draft_id) == 0
    assert await committed_conn.fetchval("SELECT count(*) FROM payout_line") == 0


@pytest.mark.asyncio
async def test_nothing_payable_makes_no_draft(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-nothing@example.com")

    assert await service.generate(committed_conn, p["partner_id"]) == []
    assert await service.generate_all(committed_conn) == []
    assert await payout_count(committed_conn) == 0


@pytest.mark.asyncio
async def test_a_payout_below_the_minimum_waits_for_more_bookings(committed_conn, rule) -> None:
    rule(minimum=5000)
    p = await partner_with_property(committed_conn, "po-min@example.com")
    await booking(committed_conn, p, total="2000.00", rate="0.12")  # 1 760

    assert await service.generate(committed_conn, p["partner_id"]) == []
    assert (await _only_balance(committed_conn, p["partner_id"]))["available"] == 1760.0

    await booking(committed_conn, p, total="4000.00", rate="0.12")  # + 3 520 = 5 280

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    assert (await payout_row(committed_conn, draft_id))["net_amount"] == D("5280.00")


@pytest.mark.asyncio
async def test_a_zero_minimum_still_never_makes_an_empty_payout(committed_conn, rule) -> None:
    rule(minimum=0)
    p = await partner_with_property(committed_conn, "po-zero@example.com")
    b = await booking(committed_conn, p, total="100.00", commission="100.00")  # all commission

    assert await service.generate(committed_conn, p["partner_id"]) == []
    assert b


@pytest.mark.asyncio
async def test_currencies_are_never_added_together(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-currency@example.com")
    usd = await add_property(
        committed_conn, p, tz="Europe/Simferopol", currency="USD", name="Villa"
    )
    await booking(committed_conn, p, total="10000.00")  # RUB
    await booking(committed_conn, usd, total="200.00", rate="0.12")  # USD, commission 24

    made = await service.generate(committed_conn, p["partner_id"])

    assert len(made) == 2
    by_currency = {}
    for payout_id in made:
        row = await payout_row(committed_conn, payout_id)
        by_currency[row["currency"]] = (row["gross_amount"], row["net_amount"], row["line_count"])
    assert by_currency == {
        "RUB": (D("10000.00"), D("8800.00"), 1),
        "USD": (D("200.00"), D("176.00"), 1),
    }
    balances = {
        b["currency"]: b["available"]
        for b in await service.balances(committed_conn, p["partner_id"])
    }
    assert balances == {"RUB": 8800.0, "USD": 176.0}


@pytest.mark.asyncio
async def test_generate_all_serves_every_partner_and_refreshes_stale_drafts(committed_conn) -> None:
    a = await partner_with_property(committed_conn, "po-all-a@example.com")
    b = await partner_with_property(committed_conn, "po-all-b@example.com")
    quiet = await partner_with_property(committed_conn, "po-all-quiet@example.com")
    stale = await partner_with_property(committed_conn, "po-all-stale@example.com")
    await booking(committed_conn, a, total="1000.00")
    await booking(committed_conn, b, total="2000.00")
    gone = await booking(committed_conn, stale, total="3000.00")
    await service.generate(committed_conn, stale["partner_id"])
    await refund_free(committed_conn, gone)

    made = await service.generate_all(committed_conn)

    rows = [await payout_row(committed_conn, i) for i in made]
    assert sorted(str(r["partner_id"]) for r in rows) == sorted([a["partner_id"], b["partner_id"]])
    assert await payout_count(committed_conn, "draft") == 2, "the stale draft is gone"
    assert quiet["partner_id"]


@pytest.mark.asyncio
async def test_three_generates_at_once_make_one_draft(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-race-gen@example.com")
    for total in ("1000.00", "2000.00", "3000.00"):
        await booking(committed_conn, p, total=total)
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=1, max_size=4)

    async def once() -> list[str]:
        async with pool.acquire() as conn:
            return await service.generate(conn, p["partner_id"])

    try:
        results = await asyncio.gather(once(), once(), once())
    finally:
        await pool.close()

    assert await payout_count(committed_conn, "draft") == 1
    assert len({tuple(r) for r in results}) == 1, "all three ended on the same draft"
    (draft_id,) = results[0]
    assert (await payout_row(committed_conn, draft_id))["line_count"] == 3


# ---------------------------------------------------------------- the document's life


@pytest.mark.asyncio
async def test_approving_freezes_the_lines_and_the_bookings_leave_the_ledger(
    committed_conn,
) -> None:
    p = await partner_with_property(committed_conn, "po-approve@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    await service.approve(committed_conn, draft_id, ADMIN)

    row = await payout_row(committed_conn, draft_id)
    assert (row["status"], row["approved_by"]) == ("approved", ADMIN) and row["approved_at"]
    assert await service.generate(committed_conn, p["partner_id"]) == [], "nothing is paid twice"
    balance = await _only_balance(committed_conn, p["partner_id"])
    assert (balance["available"], balance["approved"], balance["paid"]) == (0.0, 8800.0, 0.0)


@pytest.mark.asyncio
async def test_marking_paid_records_the_bank_reference(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-paid@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)

    await service.mark_paid(committed_conn, draft_id, "ПП № 4711 от 13.10", "cashier")

    row = await payout_row(committed_conn, draft_id)
    assert (row["status"], row["reference"], row["paid_by"]) == (
        "paid",
        "ПП № 4711 от 13.10",
        "cashier",
    )
    assert row["paid_at"] is not None
    balance = await _only_balance(committed_conn, p["partner_id"])
    assert (balance["approved"], balance["paid"]) == (0.0, 8800.0)


@pytest.mark.asyncio
async def test_a_payout_moves_forward_only(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-states@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    with pytest.raises(service.Conflict, match="only an approved payout"):
        await service.mark_paid(committed_conn, draft_id, "PP-1", ADMIN)  # not approved yet
    await service.approve(committed_conn, draft_id, ADMIN)
    with pytest.raises(service.Conflict, match="only a draft can be approved"):
        await service.approve(committed_conn, draft_id, ADMIN)  # twice
    await service.mark_paid(committed_conn, draft_id, "PP-1", ADMIN)
    with pytest.raises(service.Conflict, match="only an approved payout"):
        await service.mark_paid(committed_conn, draft_id, "PP-2", ADMIN)  # twice
    with pytest.raises(service.Conflict, match="only a draft or an approved"):
        await service.cancel(committed_conn, draft_id, ADMIN)  # money has left

    assert (await payout_row(committed_conn, draft_id))["reference"] == "PP-1"


@pytest.mark.asyncio
async def test_a_payout_that_does_not_exist_is_not_found(committed_conn) -> None:
    missing = str(uuid.uuid4())

    for action in (
        service.approve(committed_conn, missing, ADMIN),
        service.mark_paid(committed_conn, missing, "PP-1", ADMIN),
        service.cancel(committed_conn, missing, ADMIN),
        service.payout_detail(committed_conn, missing),
    ):
        with pytest.raises(service.NotFound):
            await action


@pytest.mark.asyncio
async def test_a_stale_draft_cannot_be_approved(committed_conn) -> None:
    """The operator saw 8 800; the guest cancelled for free meanwhile: approving would overpay."""
    p = await partner_with_property(committed_conn, "po-stale@example.com")
    b = await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await refund_free(committed_conn, b)

    with pytest.raises(service.Conflict, match="out of date"):
        await service.approve(committed_conn, draft_id, ADMIN)

    assert (await payout_row(committed_conn, draft_id))["status"] == "draft"
    assert await service.generate(committed_conn, p["partner_id"]) == []


@pytest.mark.asyncio
async def test_a_draft_that_gained_a_booking_cannot_be_approved_either(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-stale-more@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await booking(committed_conn, p, total="500.00")

    with pytest.raises(service.Conflict, match="out of date"):
        await service.approve(committed_conn, draft_id, ADMIN)

    (rebuilt,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, rebuilt, ADMIN)


@pytest.mark.asyncio
async def test_a_draft_below_a_raised_minimum_cannot_be_approved(committed_conn, rule) -> None:
    p = await partner_with_property(committed_conn, "po-raised-min@example.com")
    await booking(committed_conn, p, total="2000.00")
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    rule(minimum=10000)

    with pytest.raises(service.Conflict, match="minimum"):
        await service.approve(committed_conn, draft_id, ADMIN)


@pytest.mark.asyncio
async def test_cancelling_an_approved_payout_gives_its_bookings_back(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-cancel@example.com")
    b = await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)

    outcome = await service.cancel(committed_conn, draft_id, ADMIN)

    assert outcome == "cancelled"
    row = await payout_row(committed_conn, draft_id)
    assert (row["status"], row["cancelled_by"]) == ("cancelled", ADMIN) and row["cancelled_at"]
    (again,) = await service.generate(committed_conn, p["partner_id"])
    assert again != draft_id
    (line,) = await lines_of(committed_conn, again)
    assert (line["booking_id"], line["kind"], line["net_amount"]) == (b, "booking", D("8800.00"))
    balance = await _only_balance(committed_conn, p["partner_id"])
    assert (balance["available"], balance["approved"]) == (8800.0, 0.0), (
        "the cancelled one counts for nothing"
    )


@pytest.mark.asyncio
async def test_a_screen_that_saw_a_draft_cannot_cancel_what_became_an_approved_payout(
    committed_conn,
) -> None:
    """One operator opens "delete the draft"; another approves it; the first confirms. The delete
    must not turn into the cancellation of an approved payout."""
    p = await partner_with_property(committed_conn, "po-expect@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)

    with pytest.raises(service.Conflict, match="is approved, not draft: nothing was changed"):
        await service.cancel(committed_conn, draft_id, ADMIN, expect="draft")

    row = await payout_row(committed_conn, draft_id)
    assert (row["status"], row["cancelled_at"]) == ("approved", None)
    balance = await _only_balance(committed_conn, p["partner_id"])
    assert balance["approved"] == 8800.0, "its bookings are still with it"


@pytest.mark.asyncio
async def test_the_other_way_round_an_approved_payout_that_went_back_is_not_deleted(
    committed_conn,
) -> None:
    p = await partner_with_property(committed_conn, "po-expect2@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    with pytest.raises(service.Conflict, match="is draft, not approved"):
        await service.cancel(committed_conn, draft_id, ADMIN, expect="approved")

    assert (await payout_row(committed_conn, draft_id))["status"] == "draft"
    assert await service.cancel(committed_conn, draft_id, ADMIN, expect="draft") == "discarded"


@pytest.mark.asyncio
async def test_discarding_a_draft_deletes_it_and_the_next_one_is_a_new_document(
    committed_conn,
) -> None:
    p = await partner_with_property(committed_conn, "po-discard@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    assert await service.cancel(committed_conn, draft_id, ADMIN) == "discarded"

    assert await payout_count(committed_conn) == 0
    (again,) = await service.generate(committed_conn, p["partner_id"])
    assert again != draft_id


@pytest.mark.asyncio
async def test_two_approvals_at_once_approve_once(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-race-approve@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=1, max_size=3)

    async def once() -> None:
        async with pool.acquire() as conn:
            await service.approve(conn, draft_id, ADMIN)

    try:
        results = await asyncio.gather(once(), once(), return_exceptions=True)
    finally:
        await pool.close()

    assert sorted(type(r).__name__ for r in results) == ["Conflict", "NoneType"]
    assert (await payout_row(committed_conn, draft_id))["status"] == "approved"


@pytest.mark.asyncio
async def test_generating_while_approving_never_pays_a_booking_twice(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-race-mix@example.com")
    for total in ("1000.00", "2000.00"):
        await booking(committed_conn, p, total=total)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=1, max_size=4)

    async def approve() -> None:
        async with pool.acquire() as conn:
            await service.approve(conn, draft_id, ADMIN)

    async def generate() -> list[str]:
        async with pool.acquire() as conn:
            return await service.generate(conn, p["partner_id"])

    try:
        await asyncio.gather(approve(), generate(), generate(), return_exceptions=True)
    finally:
        await pool.close()

    rows = await committed_conn.fetch(
        "SELECT booking_id, count(*) AS n FROM payout_line l JOIN payout po ON po.id = l.payout_id "
        "WHERE po.status IN ('approved', 'paid') GROUP BY booking_id"
    )
    assert all(r["n"] == 1 for r in rows), "no booking is in two frozen payouts"
    assert (await _only_balance(committed_conn, p["partner_id"]))["available"] in (0.0, 2640.0)


@pytest.mark.asyncio
async def test_payouts_do_not_get_in_the_way_of_confirmations(committed_conn) -> None:
    """The module only reads bookings: confirming five holds while it runs fails none of them."""
    p = await partner_with_property(committed_conn, "po-core@example.com")
    held = []
    for _ in range(5):
        held.append(
            await booking_service.create_hold(
                committed_conn,
                unit_type_id=p["unit_type_id"],
                checkin=today_at(p["tz"]) + dt.timedelta(days=3),
                checkout=today_at(p["tz"]) + dt.timedelta(days=5),
                guest_name="Иван Гость",
                guest_email=f"g{uuid.uuid4().hex[:8]}@example.com",
                guest_phone="+79991234567",
                idempotency_key=f"po-core-{uuid.uuid4()}",
            )
        )
    await booking(committed_conn, p)
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=1, max_size=8)
    stop = asyncio.Event()

    async def keep_generating() -> int:
        runs = 0
        async with pool.acquire() as conn:
            while not stop.is_set():
                await service.generate(conn, p["partner_id"])
                await service.balances(conn, p["partner_id"])
                runs += 1
        return runs

    async def confirm(booking_id: str) -> dict:
        async with pool.acquire() as conn:
            return await payment_service.pay_and_confirm(booking_id, conn=conn)

    worker = asyncio.create_task(keep_generating())
    try:
        confirmed = await asyncio.gather(*(confirm(h["id"]) for h in held))
    finally:
        stop.set()
        runs = await worker
        await pool.close()

    assert [c["status"] for c in confirmed] == ["confirmed"] * 5
    assert runs >= 1


# ---------------------------------------------------------------- money that moves after a payout


@pytest.mark.asyncio
async def test_a_late_refund_after_the_payout_comes_back_as_a_negative_adjustment(
    committed_conn, rule
) -> None:
    rule("payment")  # pays before the stay: the case a refund can still follow
    p = await partner_with_property(committed_conn, "po-claw-late@example.com")
    b = await _real_booking(committed_conn, p)  # 6 000, commission 720, the partner is paid 5 280
    (first,) = await service.generate(committed_conn, p["partner_id"])
    await _pay_out(committed_conn, first)

    await payment_service.refund_booking(
        b, conn=committed_conn
    )  # late: keeps 3 000, commission 360

    assert await service.generate(committed_conn, p["partner_id"]) == [], (
        "the partner owes, no payout"
    )
    balance = await _only_balance(committed_conn, p["partner_id"])
    assert (balance["available"], balance["paid"]) == (-2640.0, 5280.0)
    (row,) = (await service.pending_bookings(committed_conn, p["partner_id"]))["items"]
    assert (row["state"], row["gross_amount"], row["commission_amount"], row["net_amount"]) == (
        "correction",
        -3000.0,
        -360.0,
        -2640.0,
    )


@pytest.mark.asyncio
async def test_a_correction_is_netted_against_the_next_bookings(committed_conn, rule) -> None:
    rule("payment")
    p = await partner_with_property(committed_conn, "po-claw-net@example.com")
    b = await _real_booking(committed_conn, p)
    (first,) = await service.generate(committed_conn, p["partner_id"])
    await _pay_out(committed_conn, first)
    await payment_service.refund_booking(b, conn=committed_conn)
    fresh = await booking(committed_conn, p, total="10000.00")  # + 8 800

    (second,) = await service.generate(committed_conn, p["partner_id"])

    lines = {ln["booking_id"]: ln for ln in await lines_of(committed_conn, second)}
    assert set(lines) == {b, fresh}
    assert (lines[b]["kind"], lines[b]["net_amount"]) == ("adjustment", D("-2640.00"))
    assert (lines[fresh]["kind"], lines[fresh]["net_amount"]) == ("booking", D("8800.00"))
    row = await payout_row(committed_conn, second)
    assert (row["gross_amount"], row["commission_amount"], row["net_amount"]) == (
        D("7000.00"),
        D("840.00"),
        D("6160.00"),
    )
    await _pay_out(committed_conn, second, "PP-2")
    paid_for_b = await committed_conn.fetchval(
        "SELECT sum(l.net_amount) FROM payout_line l JOIN payout po ON po.id = l.payout_id "
        "WHERE l.booking_id = $1 AND po.status = 'paid'",
        b,
    )
    assert paid_for_b == D("2640.00"), (
        "in the end the partner was paid exactly what the booking earned"
    )


@pytest.mark.asyncio
async def test_a_free_refund_after_the_payout_takes_all_of_it_back(committed_conn, rule) -> None:
    rule("payment")
    p = await partner_with_property(committed_conn, "po-claw-free@example.com")
    b = await _real_booking(committed_conn, p, start_in=20)  # far ahead: free to cancel
    (first,) = await service.generate(committed_conn, p["partner_id"])
    await _pay_out(committed_conn, first)

    out = await payment_service.refund_booking(b, conn=committed_conn)

    assert out["free_cancelled"] is True
    balance = await _only_balance(committed_conn, p["partner_id"])
    assert (balance["available"], balance["paid"]) == (-5280.0, 5280.0)
    assert await service.generate(committed_conn, p["partner_id"]) == []


@pytest.mark.asyncio
async def test_a_refund_before_the_payout_is_just_the_kept_part_no_adjustment(
    committed_conn, rule
) -> None:
    rule("payment")
    p = await partner_with_property(committed_conn, "po-claw-before@example.com")
    b = await _real_booking(committed_conn, p)
    await payment_service.refund_booking(b, conn=committed_conn)

    (draft_id,) = await service.generate(committed_conn, p["partner_id"])

    (line,) = await lines_of(committed_conn, draft_id)
    assert (line["kind"], line["net_amount"]) == ("booking", D("2640.00"))


@pytest.mark.asyncio
async def test_a_correction_is_made_even_when_the_booking_is_not_payable_by_the_rule_any_more(
    committed_conn, rule
) -> None:
    """A booking already paid out is reconciled whatever the rule says today."""
    rule("payment")
    p = await partner_with_property(committed_conn, "po-claw-rule@example.com")
    b = await _real_booking(committed_conn, p)
    (first,) = await service.generate(committed_conn, p["partner_id"])
    await _pay_out(committed_conn, first)
    await payment_service.refund_booking(b, conn=committed_conn)

    rule("checkout")  # the stay is still ahead: by this rule nothing is payable yet

    balance = await _only_balance(committed_conn, p["partner_id"])
    assert (balance["available"], balance["upcoming"]) == (-2640.0, 0.0)


# ---------------------------------------------------------------- reading


@pytest.mark.asyncio
async def test_balances_split_what_is_payable_waiting_in_processing_and_paid(
    committed_conn,
) -> None:
    p = await partner_with_property(committed_conn, "po-balances@example.com")
    await booking(committed_conn, p, total="10000.00")  # 8 800, paid out
    (first,) = await service.generate(committed_conn, p["partner_id"])
    await _pay_out(committed_conn, first)
    await booking(committed_conn, p, total="5000.00", rate="0.10")  # 4 500, approved, not paid
    (second,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, second, ADMIN)
    await booking(committed_conn, p, total="1000.00", rate="0.10")  # 900, a draft
    await service.generate(committed_conn, p["partner_id"])
    await booking(committed_conn, p, total="2000.00", rate="0.10", checkout_in=9)  # 1 800 to come

    balance = await _only_balance(committed_conn, p["partner_id"])

    assert (balance["paid"], balance["approved"]) == (8800.0, 4500.0)
    assert (balance["available"], balance["available_bookings"]) == (900.0, 1), (
        "a draft is not yet money paid"
    )
    assert (balance["upcoming"], balance["upcoming_bookings"]) == (1800.0, 1)
    assert balance["draft"]["net_amount"] == 900.0 and balance["draft"]["line_count"] == 1
    assert (balance["partner_email"], balance["currency"]) == ("po-balances@example.com", "RUB")


@pytest.mark.asyncio
async def test_pending_bookings_say_what_each_one_waits_for(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "po-pending@example.com")
    await booking(committed_conn, p, checkout_in=-2)
    await booking(committed_conn, p, checkout_in=6, total="5000.00")

    page = await service.pending_bookings(committed_conn, p["partner_id"])
    rows = page["items"]

    assert page["total"] == 2
    assert [(r["state"], r["net_amount"]) for r in rows] == [
        ("available", 8800.0),
        ("upcoming", 4400.0),
    ]
    assert rows[0]["payable_from"] == (today_at(p["tz"]) - dt.timedelta(days=2)).isoformat()
    assert rows[1]["payable_from"] == (today_at(p["tz"]) + dt.timedelta(days=6)).isoformat()


@pytest.mark.asyncio
async def test_a_long_list_of_pending_bookings_says_how_long_it_is(committed_conn) -> None:
    """A page is not the whole: the total lets the screen ask for the rest instead of guessing."""
    p = await partner_with_property(committed_conn, "po-pending-page@example.com")
    for day in range(5):
        await booking(committed_conn, p, checkout_in=day + 1, total="1000.00")

    first = await service.pending_bookings(committed_conn, p["partner_id"], limit=2)
    everything = await service.pending_bookings(committed_conn, p["partner_id"], limit=50)

    assert (len(first["items"]), first["total"]) == (2, 5)
    assert (len(everything["items"]), everything["total"]) == (5, 5)
    assert [r["code"] for r in first["items"]] == [r["code"] for r in everything["items"][:2]]
    assert sum(r["net_amount"] for r in everything["items"]) == 5 * 880.0


@pytest.mark.asyncio
async def test_a_partner_sees_only_their_own_money(committed_conn) -> None:
    a = await partner_with_property(committed_conn, "po-mine@example.com")
    b = await partner_with_property(committed_conn, "po-theirs@example.com")
    await booking(committed_conn, a, total="1000.00")
    await booking(committed_conn, b, total="9000.00")

    assert [x["available"] for x in await service.balances(committed_conn, a["partner_id"])] == [
        880.0
    ]
    assert [x["available"] for x in await service.balances(committed_conn, b["partner_id"])] == [
        7920.0
    ]
    everyone = await service.balances(committed_conn)
    assert [x["partner_email"] for x in everyone] == [
        "po-theirs@example.com",
        "po-mine@example.com",
    ]


# ---------------------------------------------------------------- the rule itself


@pytest.mark.parametrize(
    ("name", "value", "complaint"),
    [
        ("PAYOUT_ELIGIBLE_FROM", "whenever", "PAYOUT_ELIGIBLE_FROM"),
        ("PAYOUT_DELAY_DAYS", -1, "PAYOUT_DELAY_DAYS"),
        ("PAYOUT_DELAY_DAYS", 1.5, "PAYOUT_DELAY_DAYS"),
        ("PAYOUT_DELAY_DAYS", True, "PAYOUT_DELAY_DAYS"),
        ("PAYOUT_MIN_AMOUNT", -5, "PAYOUT_MIN_AMOUNT"),
        ("PAYOUT_MIN_AMOUNT", "a lot", "PAYOUT_MIN_AMOUNT"),
        ("PAYOUT_MIN_AMOUNT", float("nan"), "PAYOUT_MIN_AMOUNT"),
    ],
)
@pytest.mark.asyncio
async def test_a_rule_that_cannot_run_is_refused_at_boot_and_on_use(
    committed_conn, monkeypatch, name, value, complaint
) -> None:
    p = await partner_with_property(committed_conn, f"po-bad-{uuid.uuid4().hex[:6]}@example.com")
    monkeypatch.setattr(legal, name, value)

    with pytest.raises(ConfigError, match=complaint):
        check_config(_settings(app_env="local"))
    for use in (
        service.generate(committed_conn, p["partner_id"]),
        service.balances(committed_conn),
        service.approve(committed_conn, str(uuid.uuid4()), ADMIN),
    ):
        with pytest.raises(service.NotConfigured, match=complaint):
            await use


@pytest.mark.asyncio
async def test_payouts_exist_only_where_the_platform_holds_the_money(
    committed_conn, monkeypatch
) -> None:
    p = await partner_with_property(committed_conn, "po-flow@example.com")
    await booking(committed_conn, p)
    monkeypatch.setattr(legal, "MONEY_FLOW", "direct_to_partner")

    with pytest.raises(service.NotConfigured, match="direct_to_partner"):
        await service.generate(committed_conn, p["partner_id"])
    with pytest.raises(service.NotConfigured, match="invoices its commission"):
        await service.balances(committed_conn)
    check_config(_settings(app_env="local"))  # the rest of the app does not care


# ---------------------------------------------------------------- the whole ledger, checked from outside


def _owed_net(row: asyncpg.Record) -> Decimal:
    """What a booking earns the partner, computed from the raw columns, apart from the SQL."""
    if row["status"] in ("confirmed", "paid"):
        gross = row["total_amount"]
    elif row["status"] == "refunded":
        gross = row["kept_amount"] or D("0")
    else:
        gross = D("0")
    commission = (
        row["commission_amt"] if row["commission_status"] in ("accrued", "settled") else D("0")
    )
    return gross - commission


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(6))
async def test_random_histories_always_add_up(committed_conn, seed) -> None:
    """Bookings, refunds, time and every payout action in random order; then the books are audited.

    The conservation law: for the bookings the ledger covers (past their date, or already paid out),
    what they earn equals what approved and paid payouts already took plus what is still payable.
    The left side is computed here from the raw booking rows, the right from the payout tables.
    """
    rng = random.Random(seed)
    p = await partner_with_property(committed_conn, f"po-fuzz-{seed}@example.com")
    partner_id = p["partner_id"]
    made: list[str] = []

    async def pick(*statuses: str) -> str | None:
        rows = await committed_conn.fetch(
            "SELECT id::text FROM payout WHERE partner_id = $1 AND status = ANY($2::text[])",
            partner_id,
            list(statuses),
        )
        return rng.choice([r["id"] for r in rows]) if rows else None

    async def pick_booking(*statuses: str) -> str | None:
        rows = await committed_conn.fetch(
            "SELECT b.id::text FROM booking b WHERE b.property_id = $1 AND b.status = ANY($2::text[])",
            p["property_id"],
            list(statuses),
        )
        return rng.choice([r["id"] for r in rows]) if rows else None

    for _ in range(16):
        op = rng.choice(
            [
                "book",
                "book",
                "late",
                "free",
                "time",
                "generate",
                "generate",
                "approve",
                "pay",
                "cancel",
            ]
        )
        if op == "book":
            total = rng.choice(["1000.00", "3333.33", "7777.77", "12000.00", "0.50"])
            rate = rng.choice(["0.10", "0.12", "0.15", "0.333"])
            made.append(
                await booking(
                    committed_conn,
                    p,
                    total=total,
                    rate=rate,
                    checkout_in=rng.randint(-5, 4),
                    nights=rng.randint(1, 3),
                )
            )
        elif op in ("late", "free"):
            target = await pick_booking("confirmed")
            if target:
                if op == "free":
                    await refund_free(committed_conn, target)
                else:
                    total = await committed_conn.fetchval(
                        "SELECT total_amount FROM booking WHERE id = $1", target
                    )
                    await refund_late(committed_conn, target, str((total / 2).quantize(D("0.01"))))
        elif op == "time":
            await committed_conn.execute(
                "UPDATE booking SET checkin_date = checkin_date - 4, checkout_date = checkout_date - 4 "
                "WHERE property_id = $1",
                p["property_id"],
            )
        elif op == "generate":
            await service.generate(committed_conn, partner_id)
        elif op == "approve":
            target = await pick("draft")
            if target:
                try:
                    await service.approve(committed_conn, target, ADMIN)
                except service.Conflict:
                    pass  # stale: the operator would rebuild it
        elif op == "pay":
            target = await pick("approved")
            if target:
                await service.mark_paid(committed_conn, target, f"PP-{rng.randint(1, 999)}", ADMIN)
        else:
            target = await pick("draft", "approved")
            if target:
                await service.cancel(committed_conn, target, ADMIN)

    # Whatever happened, the operator can always bring the books to rest.
    for _ in range(3):
        for payout_id in await service.generate(committed_conn, partner_id):
            await service.approve(committed_conn, payout_id, ADMIN)
        for row in await committed_conn.fetch(
            "SELECT id::text FROM payout WHERE partner_id = $1 AND status = 'approved'", partner_id
        ):
            await service.mark_paid(committed_conn, row["id"], "PP-final", ADMIN)

    # 1. every payout is the sum of its lines, and frozen ones are worth paying
    for row in await committed_conn.fetch(
        """
        SELECT po.status, po.gross_amount, po.commission_amount, po.net_amount, po.line_count,
               coalesce(sum(l.gross_amount), 0) AS g, coalesce(sum(l.commission_amount), 0) AS c,
               coalesce(sum(l.net_amount), 0) AS n, count(l.id) AS k
        FROM payout po LEFT JOIN payout_line l ON l.payout_id = po.id
        WHERE po.partner_id = $1 GROUP BY po.id
        """,
        partner_id,
    ):
        assert (
            row["gross_amount"],
            row["commission_amount"],
            row["net_amount"],
            row["line_count"],
        ) == (
            row["g"],
            row["c"],
            row["n"],
            row["k"],
        )
        if row["status"] in ("approved", "paid"):
            assert row["net_amount"] > 0

    # 2. conservation, with the payable set decided by the rule, not by the code under test
    today = today_at(p["tz"])
    bookings = await committed_conn.fetch(
        "SELECT id::text, status, total_amount, kept_amount, commission_amt, commission_status, "
        "checkout_date FROM booking WHERE property_id = $1",
        p["property_id"],
    )
    included = {
        r["booking_id"]: r["net"]
        for r in await committed_conn.fetch(
            "SELECT l.booking_id::text, sum(l.net_amount) AS net FROM payout_line l "
            "JOIN payout po ON po.id = l.payout_id WHERE po.status IN ('approved','paid') "
            "GROUP BY l.booking_id"
        )
    }
    payable = [b for b in bookings if b["checkout_date"] <= today or b["id"] in included]
    owed_total = sum((_owed_net(b) for b in payable), D("0"))
    balances = await service.balances(committed_conn, partner_id)
    available = D(str(balances[0]["available"])) if balances else D("0")
    assert owed_total == sum(included.values(), D("0")) + available

    # 3. nothing that is payable and positive was left behind unless it is under the minimum
    assert available <= 0 or available < D("0.01"), "the final pass leaves nothing payable"
