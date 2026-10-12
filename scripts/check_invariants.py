"""Read-only, snapshot-consistent booking integrity checks.

Invariants:
a. Per unit type and date, inventory hold/sold equal active booking-line counts and remain within availability.
b. Every hold, confirmed, or paid booking line has its inventory_day row.
c. In created_at, id order, confirmed/paid has an unrefunded latest success, refunded has a later refund, and hold/cancelled/failed returns every success; at most one refund belongs to each charge. Equal transaction timestamps use UUID id as a stable, non-causal tie-break.
d. Commission status matches booking status; kept_amount exists only on refunded bookings and stays within the booking total.
e. Confirmed/paid/refunded bookings have their lifecycle outbox events, and booking events never reference a missing booking.
f. Booking-line prices sum to total_amount and dates cover exactly checkin through checkout minus one day.
g. Holds expired by more than five minutes are warnings for a lagging reaper, not violations.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field

import asyncpg


@dataclass(frozen=True)
class Finding:
    invariant: str
    ids: tuple[str, ...]


@dataclass
class CheckReport:
    violations: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[dict[str, object]]]:
        return {
            "violations": [asdict(item) for item in self.violations],
            "warnings": [asdict(item) for item in self.warnings],
        }


def _add(report: CheckReport, target: list[Finding], invariant: str, ids: list[str]) -> None:
    unique = tuple(sorted(set(ids)))
    if unique:
        target.append(Finding(invariant, unique))


async def check_connection(conn: asyncpg.Connection) -> CheckReport:
    """Check every invariant in one read-only REPEATABLE READ snapshot."""
    report = CheckReport()
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        rows = await conn.fetch(
            """
            WITH active_counts AS (
                SELECT b.unit_type_id, bl.date,
                       count(*) FILTER (WHERE b.status = 'hold') AS expected_hold,
                       count(*) FILTER (WHERE b.status IN ('confirmed', 'paid')) AS expected_sold
                FROM booking_line bl
                JOIN booking b ON b.id = bl.booking_id
                WHERE b.status IN ('hold', 'confirmed', 'paid')
                GROUP BY b.unit_type_id, bl.date
            )
            SELECT i.unit_type_id::text || '@' || i.date::text AS item
            FROM inventory_day i
            LEFT JOIN active_counts c
              ON c.unit_type_id = i.unit_type_id AND c.date = i.date
            WHERE i.hold <> coalesce(c.expected_hold, 0)
               OR i.sold <> coalesce(c.expected_sold, 0)
               OR i.hold < 0
               OR i.sold < 0
               OR i.hold + i.sold > i.available
            """
        )
        _add(report, report.violations, "inventory (a)", [row["item"] for row in rows])

        rows = await conn.fetch(
            """
            SELECT b.id::text || '@' || bl.date::text AS item
            FROM booking b
            JOIN booking_line bl ON bl.booking_id = b.id
            LEFT JOIN inventory_day i
              ON i.unit_type_id = b.unit_type_id AND i.date = bl.date
            WHERE b.status IN ('hold', 'confirmed', 'paid')
              AND i.unit_type_id IS NULL
            """
        )
        _add(report, report.violations, "missing_inventory (b)", [row["item"] for row in rows])

        payment_rows = await conn.fetch(
            """
            SELECT p.booking_id::text AS booking_id, p.id::text AS payment_id,
                   p.status AS payment_status, b.status AS booking_status
            FROM payment p
            JOIN booking b ON b.id = p.booking_id
            ORDER BY p.booking_id, p.created_at, p.id
            """
        )
        histories: dict[str, list[asyncpg.Record]] = {}
        booking_statuses: dict[str, str] = {}
        for row in payment_rows:
            histories.setdefault(row["booking_id"], []).append(row)
            booking_statuses[row["booking_id"]] = row["booking_status"]

        money_bad: list[str] = []
        for booking_id, history in histories.items():
            # Refunds belong to the latest preceding charge, ending at the next
            # succeeded row. This prevents a later charge's refund counting twice.
            charges: list[list[object]] = []
            for event in history:
                if event["payment_status"] == "succeeded":
                    charges.append([event["payment_id"], 0])
                elif event["payment_status"] == "refunded" and charges:
                    charges[-1][1] = int(charges[-1][1]) + 1

            status = booking_statuses[booking_id]
            bad = any(int(charge[1]) > 1 for charge in charges)
            if status in ("confirmed", "paid"):
                bad = bad or not charges or int(charges[-1][1]) != 0
            elif status == "refunded":
                bad = bad or not charges or int(charges[-1][1]) < 1
            elif status in ("hold", "cancelled", "failed"):
                bad = bad or any(int(charge[1]) < 1 for charge in charges)
            if bad:
                money_bad.append(booking_id)
        _add(report, report.violations, "money (c)", money_bad)

        rows = await conn.fetch(
            """
            SELECT id::text AS item
            FROM booking
            WHERE (status IN ('confirmed', 'paid') AND commission_status <> 'accrued')
               OR (status IN ('hold', 'cancelled', 'failed') AND commission_status <> 'none')
               OR (status = 'refunded' AND NOT (
                       commission_status = 'void'
                       OR (commission_status = 'accrued'
                           AND kept_amount IS NOT NULL AND kept_amount > 0)
                  ))
               OR (kept_amount IS NOT NULL AND (
                       status <> 'refunded'
                       OR kept_amount < 0
                       OR kept_amount > total_amount
                  ))
            """
        )
        _add(report, report.violations, "commission (d)", [row["item"] for row in rows])

        rows = await conn.fetch(
            """
            WITH required AS (
                SELECT id, 'booking.confirmed'::text AS event_type
                FROM booking WHERE status IN ('confirmed', 'paid', 'refunded')
                UNION ALL
                SELECT id, 'booking.cancelled'::text
                FROM booking WHERE status = 'refunded'
            ),
            missing AS (
                SELECT r.id::text AS item
                FROM required r
                WHERE NOT EXISTS (
                    SELECT 1 FROM outbox_event e
                    WHERE e.aggregate = 'booking'
                      AND e.aggregate_id = r.id::text
                      AND e.event_type = r.event_type
                )
            ),
            orphaned AS (
                SELECT 'event:' || e.id::text || '->' || e.aggregate_id AS item
                FROM outbox_event e
                WHERE e.aggregate = 'booking'
                  AND NOT EXISTS (
                      SELECT 1 FROM booking b WHERE b.id::text = e.aggregate_id
                  )
            )
            SELECT item FROM missing
            UNION ALL
            SELECT item FROM orphaned
            """
        )
        _add(report, report.violations, "outbox (e)", [row["item"] for row in rows])

        rows = await conn.fetch(
            """
            WITH line_stats AS (
                SELECT booking_id, count(*) AS line_count,
                       count(DISTINCT date) AS distinct_dates,
                       min(date) AS first_date, max(date) AS last_date,
                       coalesce(sum(price), 0::numeric) AS line_total,
                       count(*) FILTER (
                           WHERE date < b.checkin_date OR date >= b.checkout_date
                       ) AS out_of_range
                FROM booking_line bl
                JOIN booking b ON b.id = bl.booking_id
                GROUP BY booking_id, b.checkin_date, b.checkout_date
            )
            SELECT b.id::text AS item
            FROM booking b
            LEFT JOIN line_stats s ON s.booking_id = b.id
            WHERE coalesce(s.line_total, 0::numeric) <> b.total_amount
               OR coalesce(s.line_count, 0) <> b.checkout_date - b.checkin_date
               OR coalesce(s.distinct_dates, 0) <> b.checkout_date - b.checkin_date
               OR s.first_date IS DISTINCT FROM b.checkin_date
               OR s.last_date IS DISTINCT FROM b.checkout_date - 1
               OR coalesce(s.out_of_range, 0) <> 0
            """
        )
        _add(report, report.violations, "booking_lines (f)", [row["item"] for row in rows])

        rows = await conn.fetch(
            """
            SELECT id::text AS item
            FROM booking
            WHERE status = 'hold'
              AND hold_expires_at < now() - interval '5 minutes'
            ORDER BY id
            """
        )
        _add(report, report.warnings, "hold_expiry (g)", [row["item"] for row in rows])

    return report


async def check_dsn(dsn: str) -> CheckReport:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await check_connection(conn)
    finally:
        await conn.close()


async def _self_test_seed(conn: asyncpg.Connection) -> tuple[dt.date, str, str, str]:
    unit_id = "00000000-0000-0000-0000-000000000001"
    hold_id = "00000000-0000-0000-0000-000000000002"
    confirmed_id = "00000000-0000-0000-0000-000000000003"
    refunded_id = "00000000-0000-0000-0000-000000000004"
    start = dt.date.today() + dt.timedelta(days=30)

    await conn.execute(
        """
        INSERT INTO inventory_day (unit_type_id, date, available, hold, sold)
        VALUES ($1, $2, 2, 1, 1), ($1, $2::date + 1, 2, 1, 1)
        """,
        unit_id,
        start,
    )
    await conn.execute(
        """
        INSERT INTO booking
            (id, status, unit_type_id, checkin_date, checkout_date, total_amount,
             commission_status, kept_amount, hold_expires_at)
        VALUES
            ($1, 'hold', $4, $5, $5::date + 2, 150, 'none', NULL, now() + interval '15 minutes'),
            ($2, 'confirmed', $4, $5, $5::date + 2, 150, 'accrued', NULL, NULL),
            ($3, 'refunded', $4, $5, $5::date + 2, 150, 'void', 0, NULL)
        """,
        hold_id,
        confirmed_id,
        refunded_id,
        unit_id,
        start,
    )
    await conn.execute(
        """
        INSERT INTO booking_line (id, booking_id, date, price)
        VALUES
            ('00000001-0000-0000-0000-000000000001', $1, $4, 75),
            ('00000001-0000-0000-0000-000000000002', $1, $4::date + 1, 75),
            ('00000001-0000-0000-0000-000000000003', $2, $4, 75),
            ('00000001-0000-0000-0000-000000000004', $2, $4::date + 1, 75),
            ('00000001-0000-0000-0000-000000000005', $3, $4, 75),
            ('00000002-0000-0000-0000-000000000004', $3, $4::date + 1, 75)
        """,
        hold_id,
        confirmed_id,
        refunded_id,
        start,
    )
    await conn.execute(
        """
        INSERT INTO payment (id, booking_id, status, created_at)
        VALUES
            ('00000002-0000-0000-0000-000000000001', $1, 'succeeded', now() - interval '2 minutes'),
            ('00000002-0000-0000-0000-000000000002', $2, 'succeeded', now() - interval '2 minutes'),
            ('00000002-0000-0000-0000-000000000003', $2, 'refunded', now() - interval '1 minute')
        """,
        confirmed_id,
        refunded_id,
    )
    await conn.execute(
        """
        INSERT INTO outbox_event (id, aggregate, aggregate_id, event_type)
        VALUES
            ('00000003-0000-0000-0000-000000000001', 'booking', $1, 'booking.confirmed'),
            ('00000003-0000-0000-0000-000000000002', 'booking', $2, 'booking.confirmed'),
            ('00000003-0000-0000-0000-000000000003', 'booking', $2, 'booking.cancelled')
        """,
        confirmed_id,
        refunded_id,
    )
    return start, unit_id, hold_id, confirmed_id


async def run_self_test(dsn: str) -> None:
    """Create an isolated schema, verify clean state, then one fault per invariant."""
    schema = f"check_inv_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(dsn=dsn)
    created = False
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await conn.execute(f'SET search_path TO "{schema}", public')
        for statement in (
            """
            CREATE TABLE inventory_day (
                unit_type_id uuid NOT NULL, date date NOT NULL,
                available integer NOT NULL, hold integer NOT NULL, sold integer NOT NULL
            )
            """,
            """
            CREATE TABLE booking (
                id uuid PRIMARY KEY, status text NOT NULL, unit_type_id uuid NOT NULL,
                checkin_date date NOT NULL, checkout_date date NOT NULL,
                total_amount numeric(12,2) NOT NULL, commission_status text NOT NULL,
                kept_amount numeric(12,2), hold_expires_at timestamptz
            )
            """,
            """
            CREATE TABLE booking_line (
                id uuid PRIMARY KEY, booking_id uuid NOT NULL, date date NOT NULL,
                price numeric(12,2) NOT NULL
            )
            """,
            """
            CREATE TABLE payment (
                id uuid PRIMARY KEY, booking_id uuid NOT NULL, status text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE outbox_event (
                id uuid PRIMARY KEY, aggregate text NOT NULL, aggregate_id text NOT NULL,
                event_type text NOT NULL
            )
            """,
        ):
            await conn.execute(statement)

        start, unit_id, hold_id, confirmed_id = await _self_test_seed(conn)
        clean = await check_connection(conn)
        if clean.violations or clean.warnings:
            raise AssertionError(f"self-test clean state failed: {clean.as_dict()}")

        cases = (
            (
                "inventory (a)",
                "UPDATE inventory_day SET hold = hold + 1 WHERE unit_type_id = $1 AND date = $2",
                (unit_id, start),
                "inventory (a)",
            ),
            (
                "missing_inventory (b)",
                "DELETE FROM inventory_day WHERE unit_type_id = $1 AND date = $2::date + 1",
                (unit_id, start),
                "missing_inventory (b)",
            ),
            (
                "money (c)",
                "UPDATE payment SET status = 'failed' WHERE booking_id = $1 AND status = 'succeeded'",
                (confirmed_id,),
                "money (c)",
            ),
            (
                "commission (d)",
                "UPDATE booking SET commission_status = 'none' WHERE id = $1",
                (confirmed_id,),
                "commission (d)",
            ),
            (
                "outbox (e)",
                "DELETE FROM outbox_event WHERE aggregate_id = $1 AND event_type = 'booking.confirmed'",
                (confirmed_id,),
                "outbox (e)",
            ),
            (
                "booking_lines (f)",
                "UPDATE booking_line SET price = price + 1 WHERE booking_id = $1 AND date = $2",
                (confirmed_id, start),
                "booking_lines (f)",
            ),
        )
        for label, sql, params, expected in cases:
            await conn.execute(sql, *params)
            report = await check_connection(conn)
            actual = {finding.invariant for finding in report.violations}
            if actual != {expected}:
                raise AssertionError(
                    f"self-test corruption {label!r}: expected only {expected!r}, "
                    f"got {report.as_dict()}"
                )
            if label == "inventory (a)":
                await conn.execute(
                    "UPDATE inventory_day SET hold = 1 WHERE unit_type_id = $1 AND date = $2",
                    unit_id,
                    start,
                )
            elif label == "missing_inventory (b)":
                await conn.execute(
                    "INSERT INTO inventory_day (unit_type_id, date, available, hold, sold) "
                    "VALUES ($1, $2::date + 1, 2, 1, 1)",
                    unit_id,
                    start,
                )
            elif label == "money (c)":
                await conn.execute(
                    "UPDATE payment SET status = 'succeeded' WHERE booking_id = $1",
                    confirmed_id,
                )
            elif label == "commission (d)":
                await conn.execute(
                    "UPDATE booking SET commission_status = 'accrued' WHERE id = $1",
                    confirmed_id,
                )
            elif label == "outbox (e)":
                await conn.execute(
                    "INSERT INTO outbox_event (id, aggregate, aggregate_id, event_type) "
                    "VALUES ('00000003-0000-0000-0000-000000000005', 'booking', $1, "
                    "'booking.confirmed')",
                    confirmed_id,
                )
            elif label == "booking_lines (f)":
                await conn.execute(
                    "UPDATE booking_line SET price = 75 WHERE booking_id = $1 AND date = $2",
                    confirmed_id,
                    start,
                )

        await conn.execute(
            "UPDATE booking SET hold_expires_at = now() - interval '6 minutes' WHERE id = $1",
            hold_id,
        )
        warning_report = await check_connection(conn)
        if warning_report.violations or not any(
            warning.invariant == "hold_expiry (g)" and hold_id in warning.ids
            for warning in warning_report.warnings
        ):
            raise AssertionError(f"self-test expiry warning failed: {warning_report.as_dict()}")
    finally:
        if created:
            await conn.execute("SET search_path TO public")
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


def _default_dsn() -> str:
    from app.config.settings import settings

    return settings.database_url


def _print_report(report: CheckReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), sort_keys=True))
        return
    for finding in report.violations:
        print(f"VIOLATION {finding.invariant}: {', '.join(finding.ids)}")
    for finding in report.warnings:
        print(f"WARNING {finding.invariant}: {', '.join(finding.ids)}")
    if not report.violations and not report.warnings:
        print("OK: no invariant violations or warnings")
    elif not report.violations:
        print("OK: no invariant violations")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", help="PostgreSQL DSN; defaults to settings.database_url")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--self-test", action="store_true", help="verify checks in an isolated schema"
    )
    args = parser.parse_args()
    dsn = args.dsn or _default_dsn()
    if args.self_test:
        await run_self_test(dsn)
        if args.json:
            print(json.dumps({"self_test": "passed"}))
        else:
            print("SELF-TEST PASS: clean state, invariants (a)-(f), and warning (g)")
        return 0
    report = await check_dsn(dsn)
    _print_report(report, args.json)
    return 1 if report.violations else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except Exception as error:
        print(f"checker error: {error}", file=sys.stderr)
        sys.exit(2)
