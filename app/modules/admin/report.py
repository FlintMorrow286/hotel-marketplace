"""Admin commission reports.

Commission is snapshotted at confirmation time (booking.commission_amt) and
marked 'accrued'. A free cancellation marks it 'void'; a late one keeps it,
scaled to the share the partner keeps (booking.kept_amount), so `gross` is that
share there and the whole total otherwise. Reports therefore read committed
money, never recompute it from rules that may change later.
"""

from __future__ import annotations

import datetime as dt

import asyncpg


async def commission_report(
    conn: asyncpg.Connection,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict:
    """Totals per partner + per booking rows, over an optional date range."""
    args: list = []
    where = "WHERE b.commission_status IN ('accrued','settled')"
    if date_from is not None:
        args.append(date_from)
        where += f" AND b.checkin_date >= ${len(args)}"
    if date_to is not None:
        args.append(date_to)
        where += f" AND b.checkin_date < ${len(args)}"

    totals = await conn.fetch(
        f"""
        SELECT pt.id::text                 AS partner_id,
               pt.email                    AS partner_email,
               count(*)                    AS bookings,
               coalesce(sum(coalesce(b.kept_amount, b.total_amount)), 0)::float8 AS gross,
               coalesce(sum(b.commission_amt), 0)::float8 AS commission
        FROM booking b
        JOIN property p  ON p.id = b.property_id
        JOIN partner  pt ON pt.id = p.partner_id
        {where}
        GROUP BY pt.id, pt.email
        ORDER BY commission DESC
        """,
        *args,
    )

    grand = await conn.fetchrow(
        f"""
        SELECT count(*)::int                       AS bookings,
               coalesce(sum(coalesce(b.kept_amount, b.total_amount)), 0)::float8 AS gross,
               coalesce(sum(b.commission_amt), 0)::float8 AS commission
        FROM booking b
        {where}
        """,
        *args,
    )
    grand = grand or {"bookings": 0, "gross": 0.0, "commission": 0.0}

    return {
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "total": {
            "bookings": grand["bookings"],
            "gross": grand["gross"],
            "commission": grand["commission"],
        },
        "partners": [dict(t) for t in totals],
    }
