"""Settlements with partners: payouts built from what each booking earns the partner.

The platform collects the guest's money (MONEY_FLOW = agent_collect) and owes the partner what is
left after its commission. A payout is a document an operator reviews, approves and marks as paid
once the bank transfer was made outside the system.

What a booking earns the partner is read from the booking itself, never recomputed from rules:

    confirmed / paid   total_amount
    refunded           kept_amount  (what the partner keeps after a late cancellation; 0 when free)
    commission         commission_amt while commission_status is accrued or settled, else 0

Only web bookings count: a channel booking was paid on the OTA's side and the platform holds no
money for it.

A payout line is the DIFFERENCE between that and what the approved and paid payouts already gave
the partner for the booking. So a booking is paid out once, whenever the rule makes it payable; and
a refund that comes after a payout (possible when the rule pays before the guest's stay is over)
turns into a negative adjustment line in the next payout instead of an overpayment nobody notices.
A partner whose lines add up to nothing, or less, gets no payout; the shortfall waits for the next
bookings.

A draft is a workspace (one per partner and currency), rebuilt by `generate` and counted by
nothing; approving freezes its lines. Every write runs in a SERIALIZABLE transaction with the
repository's retry. The module takes no lock on bookings or inventory: it only reads booking rows,
and nothing the booking core reads is written here, so it cannot make a booking conflict or wait.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.config import legal
from app.db.tx import serializable
from app.utils.logger import get_logger

log = get_logger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
ELIGIBLE_FROM = ("payment", "checkin", "checkout")
# Payout writes are rare and run by an operator, so they may retry a conflict more than a booking.
_ATTEMPTS = 8
# Two writers that both found no draft and both inserted one: the loser starts over.
_RACE_RETRIES = 3


class PayoutError(Exception):
    """Base of what this module refuses to do."""


class NotFound(PayoutError):
    pass


class Conflict(PayoutError):
    """The payout is not in a state that allows this, or its draft is out of date."""


class NotConfigured(PayoutError):
    """The payout rule in app.config.legal is one this module cannot run."""


# ---------------------------------------------------------------- the rule


@dataclass(frozen=True)
class Rule:
    eligible_from: str
    delay_days: int
    min_amount: Decimal

    @property
    def minimum(self) -> Decimal:
        """The smallest net a payout may have: never zero or less, whatever the setting."""
        return max(self.min_amount, CENT)


def rule_problems() -> list[str]:
    """What is wrong with the payout constants, all at once (the app checks them at boot)."""
    problems: list[str] = []
    if legal.PAYOUT_ELIGIBLE_FROM not in ELIGIBLE_FROM:
        problems.append(
            f"PAYOUT_ELIGIBLE_FROM={legal.PAYOUT_ELIGIBLE_FROM!r} is not one of {list(ELIGIBLE_FROM)}"
        )
    delay = legal.PAYOUT_DELAY_DAYS
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
        problems.append(f"PAYOUT_DELAY_DAYS={delay!r} must be a whole number of days, 0 or more")
    try:
        minimum = Decimal(str(legal.PAYOUT_MIN_AMOUNT))
        if not minimum.is_finite() or minimum < 0:
            raise InvalidOperation
    except InvalidOperation:
        problems.append(
            f"PAYOUT_MIN_AMOUNT={legal.PAYOUT_MIN_AMOUNT!r} must be a number, 0 or more"
        )
    return problems


def current_rule() -> Rule:
    """The payout rule from app.config.legal, or NotConfigured when this module cannot run it."""
    if legal.MONEY_FLOW != "agent_collect":
        raise NotConfigured(
            f"MONEY_FLOW={legal.MONEY_FLOW!r}: payouts exist only for agent_collect, where the "
            "platform holds the money. Under direct_to_partner the partner holds it and the "
            "platform invoices its commission, which is a different document."
        )
    problems = rule_problems()
    if problems:
        raise NotConfigured("; ".join(problems))
    return Rule(
        legal.PAYOUT_ELIGIBLE_FROM, legal.PAYOUT_DELAY_DAYS, Decimal(str(legal.PAYOUT_MIN_AMOUNT))
    )


def rule_info(rule: Rule) -> dict[str, Any]:
    return {
        "eligible_from": rule.eligible_from,
        "delay_days": rule.delay_days,
        "min_amount": float(rule.minimum),
        "money_flow": legal.MONEY_FLOW,
    }


# ---------------------------------------------------------------- the ledger

# One row per web booking whose earnings differ from what approved and paid payouts gave for it.
# $1 partner (NULL: all), $2 eligible_from, $3 delay_days.
_LEDGER_SQL = """
SELECT booking_id::text, partner_id::text, currency, code, status, checkin_date, checkout_date,
       gross - incl_gross AS d_gross, commission - incl_commission AS d_commission,
       was_included, payable_from,
       coalesce(payable_from <= today, false) AS eligible
FROM (
    SELECT bk.id AS booking_id, pr.partner_id, pr.currency, bk.code, bk.status,
           bk.checkin_date, bk.checkout_date,
           CASE WHEN bk.status IN ('confirmed', 'paid') THEN bk.total_amount
                WHEN bk.status = 'refunded' THEN coalesce(bk.kept_amount, 0)
                ELSE 0 END AS gross,
           CASE WHEN bk.commission_status IN ('accrued', 'settled') THEN bk.commission_amt
                ELSE 0 END AS commission,
           coalesce(i.gross, 0) AS incl_gross,
           coalesce(i.commission, 0) AS incl_commission,
           i.booking_id IS NOT NULL AS was_included,
           CASE $2::text
                WHEN 'payment' THEN (bk.paid_at AT TIME ZONE pr.timezone)::date
                WHEN 'checkin' THEN bk.checkin_date
                ELSE bk.checkout_date END + $3::int AS payable_from,
           (now() AT TIME ZONE pr.timezone)::date AS today
    FROM booking bk
    JOIN property pr ON pr.id = bk.property_id
    LEFT JOIN (
        SELECT l.booking_id, sum(l.gross_amount) AS gross, sum(l.commission_amount) AS commission
        FROM payout_line l
        JOIN payout po ON po.id = l.payout_id
        WHERE po.status IN ('approved', 'paid')
        GROUP BY l.booking_id
    ) i ON i.booking_id = bk.id
    WHERE bk.origin = 'web'
      AND ($1::uuid IS NULL OR pr.partner_id = $1)
      AND (bk.status IN ('confirmed', 'paid', 'refunded') OR i.booking_id IS NOT NULL)
) earnings
WHERE gross - incl_gross <> 0 OR commission - incl_commission <> 0
ORDER BY payable_from NULLS LAST, code
"""


async def _ledger(conn: asyncpg.Connection, partner_id: str | None, rule: Rule) -> list[dict]:
    rows = await conn.fetch(_LEDGER_SQL, partner_id, rule.eligible_from, rule.delay_days)
    out = []
    for r in rows:
        row = dict(r)
        row["d_net"] = row["d_gross"] - row["d_commission"]
        out.append(row)
    return out


def _payable(row: dict) -> bool:
    """Part of a payout: payable by the rule, or a correction to money already paid out."""
    return bool(row["eligible"] or row["was_included"])


def _signature(lines: list[tuple[str, Decimal, Decimal]]) -> list[tuple[str, Decimal, Decimal]]:
    return sorted(lines)


@asynccontextmanager
async def _snapshot(conn: asyncpg.Connection) -> AsyncIterator[None]:
    """A read-only, consistent view: the figures of one screen come from one moment."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        yield


# ---------------------------------------------------------------- drafts


async def generate(conn: asyncpg.Connection, partner_id: str) -> list[str]:
    """Build (or rebuild) the partner's draft payouts from the ledger; return their ids.

    One draft per currency. A draft whose money is gone (or below the minimum) is deleted.
    Idempotent: running it again on unchanged data yields the same lines.
    """
    rule = current_rule()
    for _ in range(_RACE_RETRIES):
        try:
            return await serializable(
                conn, lambda: _generate(conn, partner_id, rule), attempts=_ATTEMPTS
            )
        except asyncpg.UniqueViolationError:
            continue
    raise Conflict("another request keeps creating the same draft; try again")


async def _generate(conn: asyncpg.Connection, partner_id: str, rule: Rule) -> list[str]:
    rows = [r for r in await _ledger(conn, partner_id, rule) if _payable(r)]
    by_currency: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_currency[r["currency"]].append(r)
    drafts = {
        d["currency"]: d["id"]
        for d in await conn.fetch(
            "SELECT id::text, currency FROM payout WHERE partner_id = $1 AND status = 'draft' "
            "FOR UPDATE",
            partner_id,
        )
    }
    made: list[str] = []
    for currency in sorted(set(by_currency) | set(drafts)):
        lines = by_currency.get(currency, [])
        net = sum((ln["d_net"] for ln in lines), ZERO)
        draft_id = drafts.get(currency)
        if not lines or net < rule.minimum:
            if draft_id is not None:
                await conn.execute("DELETE FROM payout WHERE id = $1", draft_id)  # lines cascade
            continue
        if draft_id is None:
            draft_id = await conn.fetchval(
                "INSERT INTO payout (partner_id, currency) VALUES ($1, $2) RETURNING id::text",
                partner_id,
                currency,
            )
        else:
            await conn.execute("DELETE FROM payout_line WHERE payout_id = $1", draft_id)
        await conn.executemany(
            """
            INSERT INTO payout_line
                (payout_id, booking_id, kind, gross_amount, commission_amount, net_amount)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            [
                (
                    draft_id,
                    ln["booking_id"],
                    "adjustment" if ln["was_included"] else "booking",
                    ln["d_gross"],
                    ln["d_commission"],
                    ln["d_net"],
                )
                for ln in lines
            ],
        )
        await conn.execute(
            """
            UPDATE payout
            SET gross_amount = $2, commission_amount = $3, net_amount = $4, line_count = $5
            WHERE id = $1
            """,
            draft_id,
            sum((ln["d_gross"] for ln in lines), ZERO),
            sum((ln["d_commission"] for ln in lines), ZERO),
            net,
            len(lines),
        )
        made.append(draft_id)
    return made


async def generate_all(conn: asyncpg.Connection) -> list[str]:
    """`generate` for every partner with something payable or a draft to refresh."""
    rule = current_rule()
    async with _snapshot(conn):
        owed = {r["partner_id"] for r in await _ledger(conn, None, rule) if _payable(r)}
        drafted = {
            r["partner_id"]
            for r in await conn.fetch(
                "SELECT DISTINCT partner_id::text FROM payout WHERE status = 'draft'"
            )
        }
    made: list[str] = []
    for partner_id in sorted(owed | drafted):
        made += await generate(conn, partner_id)
    return made


# ---------------------------------------------------------------- the document's life


async def approve(conn: asyncpg.Connection, payout_id: str, admin: str) -> None:
    """Freeze a draft. Refused when bookings changed since it was made: regenerate it first."""
    rule = current_rule()

    async def body() -> None:
        po = await conn.fetchrow("SELECT * FROM payout WHERE id = $1 FOR UPDATE", payout_id)
        if po is None:
            raise NotFound("payout not found")
        if po["status"] != "draft":
            raise Conflict(f"payout is {po['status']}, only a draft can be approved")
        partner_id = str(po["partner_id"])
        fresh = [
            (r["booking_id"], r["d_gross"], r["d_commission"])
            for r in await _ledger(conn, partner_id, rule)
            if _payable(r) and r["currency"] == po["currency"]
        ]
        stored = [
            (r["booking_id"], r["gross_amount"], r["commission_amount"])
            for r in await conn.fetch(
                "SELECT booking_id::text, gross_amount, commission_amount "
                "FROM payout_line WHERE payout_id = $1",
                payout_id,
            )
        ]
        if _signature(fresh) != _signature(stored):
            raise Conflict(
                "the draft is out of date: bookings changed since it was made; rebuild it"
            )
        if po["net_amount"] < rule.minimum:
            raise Conflict(f"the payout is below the minimum of {rule.minimum}")
        await conn.execute(
            "UPDATE payout SET status = 'approved', approved_at = now(), approved_by = $2 "
            "WHERE id = $1",
            payout_id,
            admin,
        )

    await serializable(conn, body, attempts=_ATTEMPTS)
    log.info("payout-approved", payout_id=payout_id)


async def mark_paid(conn: asyncpg.Connection, payout_id: str, reference: str, admin: str) -> None:
    """Record that the money was transferred, with the bank's payment reference."""
    row = await conn.fetchrow(
        """
        UPDATE payout
        SET status = 'paid', paid_at = now(), paid_by = $3, reference = $2
        WHERE id = $1 AND status = 'approved'
        RETURNING id
        """,
        payout_id,
        reference,
        admin,
    )
    if row is None:
        status = await conn.fetchval("SELECT status FROM payout WHERE id = $1", payout_id)
        if status is None:
            raise NotFound("payout not found")
        raise Conflict(f"payout is {status}, only an approved payout can be marked paid")
    log.info("payout-paid", payout_id=payout_id)


async def cancel(
    conn: asyncpg.Connection, payout_id: str, admin: str, expect: str | None = None
) -> str:
    """Throw a draft away, or cancel an approved payout that was not paid. Returns the new state.

    Cancelling an approved payout gives its bookings back to the ledger (they are payable again).
    A paid payout cannot be cancelled: money that left needs its own correction.

    `expect` is what the caller believes the payout is ("draft" or "approved"). The two outcomes
    are very different (a draft is deleted, an approved payout is cancelled), so a screen that
    showed a draft must not cancel the payout another operator approved in the meantime: when the
    state is not the expected one nothing is changed.
    """

    async def body() -> str:
        po = await conn.fetchrow("SELECT status FROM payout WHERE id = $1 FOR UPDATE", payout_id)
        if po is None:
            raise NotFound("payout not found")
        if expect is not None and po["status"] != expect:
            raise Conflict(f"payout is {po['status']}, not {expect}: nothing was changed")
        if po["status"] == "draft":
            await conn.execute("DELETE FROM payout WHERE id = $1", payout_id)
            return "discarded"
        if po["status"] == "approved":
            await conn.execute(
                "UPDATE payout SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2 "
                "WHERE id = $1",
                payout_id,
                admin,
            )
            return "cancelled"
        raise Conflict(
            f"payout is {po['status']}, only a draft or an approved payout can be cancelled"
        )

    outcome = await serializable(conn, body, attempts=_ATTEMPTS)
    log.info("payout-cancelled", payout_id=payout_id, outcome=outcome)
    return outcome


# ---------------------------------------------------------------- reading


def _f(value: Decimal | None) -> float:
    return float(value if value is not None else ZERO)


def _iso(value: dt.datetime | dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _payout_out(row: asyncpg.Record, *, admin: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row["id"],
        "number": row["number"],
        "partner_id": row["partner_id"],
        "partner_email": row["partner_email"],
        "partner_name": row["partner_name"],
        "currency": row["currency"],
        "status": row["status"],
        "gross_amount": _f(row["gross_amount"]),
        "commission_amount": _f(row["commission_amount"]),
        "net_amount": _f(row["net_amount"]),
        "line_count": row["line_count"],
        "reference": row["reference"],
        "created_at": _iso(row["created_at"]),
        "approved_at": _iso(row["approved_at"]),
        "paid_at": _iso(row["paid_at"]),
        "cancelled_at": _iso(row["cancelled_at"]),
    }
    if admin:
        out["approved_by"] = row["approved_by"]
        out["paid_by"] = row["paid_by"]
        out["cancelled_by"] = row["cancelled_by"]
    return out


_PAYOUT_SELECT = """
SELECT po.id::text, po.number, po.partner_id::text, pt.email::text AS partner_email,
       pt.name AS partner_name, po.currency, po.status, po.gross_amount, po.commission_amount,
       po.net_amount, po.line_count, po.reference, po.created_at, po.approved_at, po.approved_by,
       po.paid_at, po.paid_by, po.cancelled_at, po.cancelled_by
FROM payout po
JOIN partner pt ON pt.id = po.partner_id
"""

# What a partner may see of the documents: never a draft, the operator's workspace.
_PARTNER_VISIBLE = "(po.status IN ('approved', 'paid') OR (po.status = 'cancelled' AND po.approved_at IS NOT NULL))"


async def list_payouts(
    conn: asyncpg.Connection,
    *,
    partner_id: str | None = None,
    status: str | None = None,
    as_partner: bool = False,
    limit: int = 100,
) -> list[dict]:
    where = ["($1::uuid IS NULL OR po.partner_id = $1)", "($2::text IS NULL OR po.status = $2)"]
    if as_partner:
        where.append(_PARTNER_VISIBLE)
    rows = await conn.fetch(
        f"{_PAYOUT_SELECT} WHERE {' AND '.join(where)} ORDER BY po.number DESC LIMIT $3",
        partner_id,
        status,
        limit,
    )
    return [_payout_out(r, admin=not as_partner) for r in rows]


async def payout_summaries(conn: asyncpg.Connection, ids: list[str]) -> list[dict]:
    """The operator's view of some payouts, without their lines."""
    if not ids:
        return []
    rows = await conn.fetch(
        f"{_PAYOUT_SELECT} WHERE po.id = ANY($1::uuid[]) ORDER BY po.number", ids
    )
    return [_payout_out(r, admin=True) for r in rows]


async def payout_summary(conn: asyncpg.Connection, payout_id: str) -> dict:
    rows = await payout_summaries(conn, [payout_id])
    if not rows:
        raise NotFound("payout not found")
    return rows[0]


async def payout_detail(
    conn: asyncpg.Connection,
    payout_id: str,
    *,
    partner_id: str | None = None,
    as_partner: bool = False,
) -> dict:
    """One payout with its lines. A partner gets only their own, and never a draft (404)."""
    async with _snapshot(conn):
        where = "po.id = $1 AND ($2::uuid IS NULL OR po.partner_id = $2)"
        if as_partner:
            where += f" AND {_PARTNER_VISIBLE}"
        row = await conn.fetchrow(f"{_PAYOUT_SELECT} WHERE {where}", payout_id, partner_id)
        if row is None:
            raise NotFound("payout not found")
        lines = await conn.fetch(
            """
            SELECT l.booking_id::text, bk.code, pr.name AS property_name, bk.status AS booking_status,
                   bk.checkin_date, bk.checkout_date, l.kind,
                   l.gross_amount, l.commission_amount, l.net_amount
            FROM payout_line l
            JOIN booking bk ON bk.id = l.booking_id
            JOIN property pr ON pr.id = bk.property_id
            WHERE l.payout_id = $1
            ORDER BY bk.checkin_date, bk.code
            """,
            payout_id,
        )
    out = _payout_out(row, admin=not as_partner)
    out["lines"] = [
        {
            "booking_id": ln["booking_id"],
            "code": ln["code"],
            "property_name": ln["property_name"],
            "booking_status": ln["booking_status"],
            "checkin_date": ln["checkin_date"].isoformat(),
            "checkout_date": ln["checkout_date"].isoformat(),
            "kind": ln["kind"],
            "gross_amount": _f(ln["gross_amount"]),
            "commission_amount": _f(ln["commission_amount"]),
            "net_amount": _f(ln["net_amount"]),
        }
        for ln in lines
    ]
    return out


async def balances(conn: asyncpg.Connection, partner_id: str | None = None) -> list[dict]:
    """What each partner is owed, by currency, in the figures an operator needs.

    available   payable now and not yet in an approved or paid payout (negative: corrections for
                money already paid out that the next bookings will have to cover)
    upcoming    earned but not yet payable by the rule
    approved    in approved payouts, not yet paid
    paid        in paid payouts
    draft       the open draft, if any
    """
    rule = current_rule()
    async with _snapshot(conn):
        rows = await _ledger(conn, partner_id, rule)
        docs = await conn.fetch(
            """
            SELECT partner_id::text, currency, status, sum(net_amount) AS net,
                   (array_agg(id::text))[1] AS any_id, (array_agg(number))[1] AS any_number,
                   sum(line_count)::int AS lines
            FROM payout
            WHERE status IN ('draft', 'approved', 'paid') AND ($1::uuid IS NULL OR partner_id = $1)
            GROUP BY partner_id, currency, status
            """,
            partner_id,
        )
        partners = {
            r["id"]: r
            for r in await conn.fetch(
                "SELECT id::text, email::text AS email, name FROM partner "
                "WHERE ($1::uuid IS NULL OR id = $1)",
                partner_id,
            )
        }

    def blank() -> dict[str, Any]:
        return {
            "available": ZERO,
            "available_bookings": 0,
            "upcoming": ZERO,
            "upcoming_bookings": 0,
            "approved": ZERO,
            "paid": ZERO,
            "draft": None,
        }

    acc: dict[tuple[str, str], dict[str, Any]] = defaultdict(blank)
    for r in rows:
        slot = acc[(r["partner_id"], r["currency"])]
        if _payable(r):
            slot["available"] += r["d_net"]
            slot["available_bookings"] += 1
        else:
            slot["upcoming"] += r["d_net"]
            slot["upcoming_bookings"] += 1
    for d in docs:
        slot = acc[(d["partner_id"], d["currency"])]
        if d["status"] == "draft":
            slot["draft"] = {
                "id": d["any_id"],
                "number": d["any_number"],
                "net_amount": _f(d["net"]),
                "line_count": d["lines"],
            }
        else:
            slot[d["status"]] += d["net"]
    out = []
    for (pid, currency), slot in acc.items():
        who = partners.get(pid)
        out.append(
            {
                "partner_id": pid,
                "partner_email": who["email"] if who else None,
                "partner_name": who["name"] if who else None,
                "currency": currency,
                "available": _f(slot["available"]),
                "available_bookings": slot["available_bookings"],
                "upcoming": _f(slot["upcoming"]),
                "upcoming_bookings": slot["upcoming_bookings"],
                "approved": _f(slot["approved"]),
                "paid": _f(slot["paid"]),
                "draft": slot["draft"],
            }
        )
    out.sort(key=lambda b: (-b["available"], b["partner_email"] or "", b["currency"]))
    return out


async def pending_bookings(conn: asyncpg.Connection, partner_id: str, limit: int = 200) -> dict:
    """The bookings behind a partner's balance: what is payable, what is waiting, what is corrected.

    `{"items": [the first `limit`], "total": N}`: the page says how much of the list it is, so a
    screen can ask for more instead of leaving a balance that its breakdown does not add up to.
    """
    rule = current_rule()
    async with _snapshot(conn):
        rows = await _ledger(conn, partner_id, rule)
    out = []
    for r in rows[:limit]:
        state = (
            "correction" if r["was_included"] else ("available" if r["eligible"] else "upcoming")
        )
        out.append(
            {
                "booking_id": r["booking_id"],
                "code": r["code"],
                "booking_status": r["status"],
                "currency": r["currency"],
                "checkin_date": r["checkin_date"].isoformat(),
                "checkout_date": r["checkout_date"].isoformat(),
                "payable_from": _iso(r["payable_from"]),
                "state": state,
                "gross_amount": _f(r["d_gross"]),
                "commission_amount": _f(r["d_commission"]),
                "net_amount": _f(r["d_net"]),
            }
        )
    return {"items": out, "total": len(rows)}


# ---------------------------------------------------------------- export

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_STATUS_RU = {
    "draft": "Черновик",
    "approved": "Утверждена",
    "paid": "Выплачена",
    "cancelled": "Отменена",
}
_KIND_RU = {"booking": "Бронь", "adjustment": "Корректировка"}


def _text(value: str | None) -> str:
    """A text cell that a spreadsheet will not run as a formula (names are typed by users)."""
    text = value or ""
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def _num(value: float) -> str:
    """12345.6 -> 12345,60: the form a Russian spreadsheet reads as a number."""
    return f"{Decimal(str(value)).quantize(CENT):f}".replace(".", ",")


def _day(iso: str | None) -> str:
    return dt.date.fromisoformat(iso[:10]).strftime("%d.%m.%Y") if iso else ""


def payout_csv(detail: dict) -> bytes:
    """The payout as a file for the bank and the books: UTF-8 with a BOM, `;`-separated."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["Выплата", f"№ {detail['number']}"])
    w.writerow(["Партнёр", _text(f"{detail['partner_name']} <{detail['partner_email']}>")])
    w.writerow(["Статус", _STATUS_RU.get(detail["status"], detail["status"])])
    w.writerow(["Валюта", _text(detail["currency"])])
    if detail.get("paid_at"):
        w.writerow(["Выплачена", _day(detail["paid_at"])])
    if detail.get("reference"):
        w.writerow(["Платёжное поручение", _text(detail["reference"])])
    w.writerow([])
    w.writerow(
        ["Код брони", "Объект", "Заезд", "Выезд", "Строка", "Сумма", "Комиссия", "К выплате"]
    )
    for ln in detail["lines"]:
        w.writerow(
            [
                _text(ln["code"]),
                _text(ln["property_name"]),
                _day(ln["checkin_date"]),
                _day(ln["checkout_date"]),
                _KIND_RU.get(ln["kind"], ln["kind"]),
                _num(ln["gross_amount"]),
                _num(ln["commission_amount"]),
                _num(ln["net_amount"]),
            ]
        )
    w.writerow(
        [
            "Итого",
            "",
            "",
            "",
            "",
            _num(detail["gross_amount"]),
            _num(detail["commission_amount"]),
            _num(detail["net_amount"]),
        ]
    )
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")
