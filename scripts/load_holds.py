#!/usr/bin/env python3
"""Fire concurrent holds at a running API and check that nothing was oversold.

Creates a throwaway room (partner, property, unit type, 30 days of inventory),
sends N simultaneous `POST /v1/bookings/hold` requests for the same nights,
then reads the database back and checks the invariants the booking core exists
for. Cleans up after itself unless --keep is given.

    uv run python scripts/load_holds.py --n 100 --units 1     # 1 x 201, 99 x 409
    uv run python scripts/load_holds.py --n 100 --units 100   # 100 x 201 on shared rows

Exit status is 1 when an invariant fails, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import legal  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.modules.auth import service as auth_service  # noqa: E402
from app.modules.auth.schemas import PartnerRegisterRequest  # noqa: E402
from app.modules.property import service as property_service  # noqa: E402
from app.modules.property.schemas import PropertyCreate  # noqa: E402


async def _seed(conn: asyncpg.Connection, units: int, nights: int) -> tuple[str, str, str]:
    # One transaction: a failed seed leaves no half-built room behind.
    async with conn.transaction():
        tag = uuid.uuid4().hex[:8]
        partner_id = await auth_service.register_partner(
            conn,
            PartnerRegisterRequest(
                email=f"load-{tag}@example.com", password="load-test-1", name="Load"
            ),
        )
        prop = await property_service.create_property(
            conn,
            partner_id,
            PropertyCreate(
                name=f"Load {tag}",
                property_type="apartment",
                city="Yalta",
                timezone="Europe/Simferopol",
            ),
        )
        unit_type_id = await conn.fetchval(
            "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
            "VALUES ($1, 'Load', 2, $2, 3000) RETURNING id::text",
            prop.id,
            units,
        )
        # The public hold route serves published properties only; _cleanup removes this one.
        await conn.execute("UPDATE property SET status = 'published' WHERE id = $1", prop.id)
        await conn.execute(
            "INSERT INTO inventory_day (unit_type_id, date, available) "
            "SELECT $1, d, $2 FROM generate_series(current_date, current_date + $3::int, '1 day') AS g(d)",
            unit_type_id,
            units,
            nights + 5,
        )
        return partner_id, prop.id, unit_type_id


async def _cleanup(conn: asyncpg.Connection, partner_id: str, property_id: str) -> None:
    await conn.execute(
        "DELETE FROM booking_line WHERE booking_id IN "
        "(SELECT id FROM booking WHERE property_id = $1)",
        property_id,
    )
    await conn.execute("DELETE FROM outbox_event WHERE property_id = $1", property_id)
    await conn.execute("DELETE FROM booking WHERE property_id = $1", property_id)
    await conn.execute(
        "DELETE FROM inventory_day WHERE unit_type_id IN "
        "(SELECT id FROM unit_type WHERE property_id = $1)",
        property_id,
    )
    await conn.execute("DELETE FROM unit_type WHERE property_id = $1", property_id)
    await conn.execute("DELETE FROM property WHERE id = $1", property_id)
    await conn.execute("DELETE FROM partner WHERE id = $1", partner_id)


async def _one(
    client: httpx.AsyncClient, unit_type_id: str, i: int, nights: int
) -> tuple[int, float]:
    today = dt.date.today()
    body = {
        "unit_type_id": unit_type_id,
        "checkin": today.isoformat(),
        "checkout": (today + dt.timedelta(days=nights)).isoformat(),
        "guest": {
            "name": f"Guest {i}",
            "email": f"guest{i}-{uuid.uuid4().hex[:6]}@example.com",
            "phone": "+79991234567",
        },
        "consent": {"docs_version": legal.LEGAL_DOCS_VERSION, "offer": True, "personal_data": True},
    }
    started = time.perf_counter()
    response = await client.post(
        "/v1/bookings/hold", json=body, headers={"Idempotency-Key": uuid.uuid4().hex}
    )
    return response.status_code, (time.perf_counter() - started) * 1000


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=f"http://127.0.0.1:{settings.app_port}")
    parser.add_argument("--n", type=int, default=100, help="simultaneous requests")
    parser.add_argument("--units", type=int, default=1, help="units in the room")
    parser.add_argument("--nights", type=int, default=2)
    parser.add_argument("--keep", action="store_true", help="leave the throwaway rows")
    parser.add_argument(
        "--allow-any-database",
        action="store_true",
        help="run although APP_ENV is not local/dev/test",
    )
    args = parser.parse_args()
    if not settings.is_dev and not args.allow_any_database:
        print(
            f"refusing to create a throwaway room: APP_ENV={settings.app_env!r} is not a "
            "development environment; pass --allow-any-database to do it anyway",
            file=sys.stderr,
        )
        return 2

    conn = await asyncpg.connect(settings.database_url)
    partner_id, property_id, unit_type_id = await _seed(conn, args.units, args.nights)
    try:
        limits = httpx.Limits(max_connections=args.n, max_keepalive_connections=args.n)
        async with httpx.AsyncClient(base_url=args.base, limits=limits, timeout=60) as client:
            wall = time.perf_counter()
            results = await asyncio.gather(
                *(_one(client, unit_type_id, i, args.nights) for i in range(args.n))
            )
            wall = time.perf_counter() - wall

        codes = Counter(code for code, _ in results)
        latencies = [ms for _, ms in results]
        print(f"n={args.n} units={args.units} wall={wall:.2f}s")
        print("status codes:", dict(sorted(codes.items())))
        print(
            f"latency ms: p50={_pct(latencies, 0.5):.0f} p95={_pct(latencies, 0.95):.0f} "
            f"max={max(latencies):.0f}"
        )

        created = codes.get(201, 0)
        rows = await conn.fetch(
            "SELECT hold, sold, available FROM inventory_day WHERE unit_type_id = $1 "
            "AND date < current_date + $2::int ORDER BY date",
            unit_type_id,
            args.nights,
        )
        bookings = await conn.fetchval(
            "SELECT count(*) FROM booking WHERE unit_type_id = $1 AND status = 'hold'", unit_type_id
        )
        expected = min(args.n, args.units)
        problems = []
        if any(r["hold"] != created or r["sold"] != 0 or r["hold"] > r["available"] for r in rows):
            problems.append(
                f"inventory disagrees with {created} created holds: "
                f"{[(r['hold'], r['sold']) for r in rows]}"
            )
        if bookings != created:
            problems.append(f"{bookings} bookings in the database, {created} answered 201")
        if created != expected:
            problems.append(f"{created} holds created, {expected} expected")
        wrong = {c: n for c, n in codes.items() if c not in (201, 409)}
        if wrong:
            problems.append(f"unexpected answers: {wrong}")
        for problem in problems:
            print("FAIL:", problem)
        print("OK: no oversell, no unexpected answers" if not problems else "FAILED")
        return 1 if problems else 0
    finally:
        if not args.keep:
            await _cleanup(conn, partner_id, property_id)
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
