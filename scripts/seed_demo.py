#!/usr/bin/env python3
"""Seed the demo content: two Koktebel hotels with real photos.

Goes through the database directly (a partner doing the same via the cabinet
ends up with the same rows). Idempotent — safe to re-run.

The partner and admin accounts it creates have published passwords, so it
refuses to run unless APP_ENV is local, dev or test.

    uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.db.pool import close_pool, init_pool  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

log = get_logger(__name__)

PARTNER_EMAIL = "demo@example.com"
PARTNER_PASSWORD = "demo-password"

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password"


def _demo_cover(label: str) -> bytes:
    """A warm on-palette gradient stand-in, so the demo catalog has a cover.

    Real photos come from partners; this is only what the demo ships with.
    """
    import hashlib
    import io

    from PIL import Image, ImageDraw, ImageFilter

    width, height = 1600, 1000
    # Deterministic per property: the sun sits differently for each name.
    digest = hashlib.sha256(label.encode()).digest()
    shift = digest[0] / 255

    # Two stops from the paper-and-ink palette: page parchment to muted terracotta.
    top = (244, 240, 232)
    bottom = (190, 158, 130)

    ramp = Image.new("RGB", (1, 256))
    ramp.putdata(
        [
            tuple(round(top[c] + (bottom[c] - top[c]) * i / 255) for c in range(3))
            for i in range(256)
        ]
    )
    base = ramp.resize((width, height))

    # A soft sun: the only shape in the image, blurred to nothing hard.
    sun = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(sun)
    cx = round(width * (0.25 + 0.5 * shift))
    draw.ellipse((cx - 260, 180, cx + 260, 700), fill=90)
    sun = sun.filter(ImageFilter.GaussianBlur(120))

    overlay = Image.new("RGB", (width, height), (236, 214, 188))
    base.paste(overlay, mask=sun)

    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# Room photos used to live in web/public/rooms and be wired in here. Photos
# now belong to the partner API (upload of a photo against a property), and
# until that lands the demo catalogue simply shows no photos.

PROPERTIES = [
    {
        "name": "Выше неба",
        "rooms": [
            (1, "Семейный номер с видом на горы и террасой", 4, 1, 6500),
            (2, "Семейный номер с видом на горы и террасой", 4, 1, 6500),
        ],
    },
    {
        "name": "Седьмое небо",
        "rooms": [
            (3, "Номер с видом на море и горы", 4, 1, 7200),
            (4, "Номер с террасой и видом на горы", 4, 1, 6900),
            (5, "Номер с видом на море, горы и Коктебель", 4, 1, 8400),
            (6, "Номер с балконом и видом на горы", 4, 1, 6100),
        ],
    },
]

AMENITIES = {
    "Выше неба": ["wifi", "parking", "pool", "breakfast", "conditioner", "sea_view", "family"],
    "Седьмое небо": ["wifi", "parking", "kitchen", "washer", "heating", "balcony", "transfer"],
}


DEMO_BOOKINGS = [
    # (room_no, guest name, nights from today, status)
    (3, "Анна Морозова", 4, "confirmed"),
    (5, "Дмитрий Соколов", 10, "confirmed"),
    (1, "Елена Васильева", 2, "confirmed"),
    (6, "Сергей Кузнецов", 18, "confirmed"),
    (4, "Ольга Никитина", 7, "confirmed"),
]


async def _seed_bookings(conn: asyncpg.Connection, partner_id: str) -> None:
    """Confirmed demo bookings so the partner chessboard, the iCal feed and the
    admin commission report are not empty in the demo.

    Goes through create_hold + pay_and_confirm — the same path a guest takes —
    so the inventory, commission and outbox rows are exactly what a real
    booking leaves behind. Idempotent by key: a re-seed never duplicates.
    """
    import datetime as dt

    from app.modules.booking import service as booking_service
    from app.modules.payment import service as payment_service

    today = dt.date.today()
    seeded = 0
    for room_no, guest_name, nights, _status in DEMO_BOOKINGS:
        unit_type_id = await conn.fetchrow(
            """
            SELECT ut.id::text
            FROM unit_type ut
            JOIN property p ON p.id = ut.property_id
            WHERE p.partner_id = $1
              AND ut.name LIKE ('Номер ' || $2 || ' ·%')
            ORDER BY ut.name
            LIMIT 1
            """,
            partner_id,
            str(room_no),
        )
        if unit_type_id is None:
            continue
        unit_type_id = unit_type_id["id"]

        idem = f"demo-booking-{room_no}"
        checkin = today + dt.timedelta(days=nights)
        checkout = checkin + dt.timedelta(days=2)

        existing = await conn.fetchval(
            "SELECT id::text FROM booking WHERE idempotency_key = $1",
            idem,
        )
        if existing is not None:
            continue

        async with conn.transaction(isolation="serializable"):
            hold = await booking_service.create_hold(
                conn,
                unit_type_id=unit_type_id,
                checkin=checkin,
                checkout=checkout,
                guest_name=guest_name,
                guest_email=f"demo-{room_no}@example.com",
                guest_phone="+79990000000",
                idempotency_key=idem,
            )
        await payment_service.pay_and_confirm(hold["id"], conn=conn)
        seeded += 1
    if seeded:
        log.info("demo-bookings-seeded", count=seeded)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--allow-any-database",
        action="store_true",
        help="run although APP_ENV is not local/dev/test",
    )
    args = parser.parse_args()
    if not settings.is_dev and not args.allow_any_database:
        print(
            "refusing to create accounts with published passwords: "
            f"APP_ENV={settings.app_env!r} is not a development environment; "
            "pass --allow-any-database to do it anyway",
            file=sys.stderr,
        )
        return 2

    pool = await init_pool()
    try:
        async with pool.acquire() as conn:
            partner_id = await conn.fetchval(
                """
                INSERT INTO partner (email, password_hash, name)
                VALUES ($1, $2, 'Демо-партнёр')
                ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
                RETURNING id::text
                """,
                "demo@example.com",
                # Same placeholder hash as auth/service.py - demo only.
                __import__("hashlib").sha256(b"demo-password").hexdigest(),
            )

            # Reassign any objects from a superseded demo partner so a re-seed
            # never leaves orphan properties the current token cannot see.
            await conn.execute(
                """
                UPDATE property SET partner_id = $1
                 WHERE partner_id IN (
                     SELECT id FROM partner WHERE email <> 'demo@example.com'
                 )
                """,
                partner_id,
            )

            for property_def in PROPERTIES:
                address = json.dumps({"settlement": "Коктебель", "region": "Крым"})

                # Re-seed keeps a partner's uploaded photos (the catalog cover
                # comes through the API now), only the status is refreshed.
                property_id = await conn.fetchval(
                    """
                    UPDATE property
                       SET status = 'published'
                     WHERE partner_id = $1 AND name = $2
                    RETURNING id::text
                    """,
                    partner_id,
                    property_def["name"],
                )
                if property_id is None:
                    property_id = await conn.fetchval(
                        """
                        INSERT INTO property (partner_id, name, property_type, city, timezone,
                                              status, address, photos)
                        VALUES ($1, $2, 'hotel', 'Коктебель', 'Europe/Simferopol',
                                'published', $3, $4)
                        RETURNING id::text
                        """,
                        partner_id,
                        property_def["name"],
                        address,
                        "[]",
                    )

                # The demo catalog shows what amenities look like to a guest.
                # Re-seed refreshes them, like it refreshes status and price.
                keys = AMENITIES.get(property_def["name"], [])
                if keys:
                    from app.config.amenities import normalise

                    await conn.execute(
                        "UPDATE property SET amenities = $2 WHERE id = $1",
                        property_id,
                        json.dumps(normalise(keys)),
                    )
                # A demo cover so the catalog is not all placeholders: generated
                # here, stored through the same path a partner's upload takes.
                has_photos = await conn.fetchval(
                    "SELECT jsonb_array_length(photos) FROM property WHERE id = $1",
                    property_id,
                )
                if has_photos == 0:
                    from app.modules.property import photos as photos_mod

                    try:
                        await photos_mod.add_photo(
                            conn,
                            property_id,
                            partner_id,
                            _demo_cover(property_def["name"]),
                        )
                        log.info("demo-photo-seeded", property=property_def["name"])
                    except photos_mod.PhotoError as exc:
                        log.warning("demo-photo-skipped", detail=exc.detail)

                for room_no, room_name, capacity, total_units, base_price in property_def["rooms"]:
                    existing = await conn.fetchval(
                        "SELECT id::text FROM unit_type WHERE property_id = $1 AND name = $2",
                        property_id,
                        f"Номер {room_no} · {room_name}",
                    )
                    if existing:
                        # A re-seed keeps prices current (rooms get repriced).
                        await conn.execute(
                            "UPDATE unit_type SET base_price = $2 WHERE id = $1",
                            existing,
                            base_price,
                        )
                        continue
                    unit_type_id = await conn.fetchval(
                        """
                        INSERT INTO unit_type (property_id, name, capacity, total_units, base_price)
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING id::text
                        """,
                        property_id,
                        f"Номер {room_no} · {room_name}",
                        capacity,
                        total_units,
                        base_price,
                    )
                    if unit_type_id:
                        await conn.execute(
                            """
                            INSERT INTO inventory_day (unit_type_id, date, available)
                            SELECT $1, d::date, $2
                            FROM generate_series(
                                CURRENT_DATE, CURRENT_DATE + interval '365 days', '1 day'
                            ) AS d
                            WHERE NOT EXISTS (
                                SELECT 1 FROM inventory_day i
                                WHERE i.unit_type_id = $1 AND i.date = d::date
                            )
                            """,
                            unit_type_id,
                            total_units,
                        )
                log.info("property-seeded", name=property_def["name"], id=property_id)

            # Staff account for the admin panel (reports + moderation).
            await conn.execute(
                """
                INSERT INTO admin (email, password_hash)
                VALUES ($1, $2)
                ON CONFLICT (email) DO NOTHING
                """,
                ADMIN_EMAIL,
                __import__("hashlib").sha256(ADMIN_PASSWORD.encode()).hexdigest(),
            )

            await _seed_bookings(conn, partner_id)

            print(
                f"seeded: partner {PARTNER_EMAIL} / {len(PROPERTIES)} properties, "
                f"admin {ADMIN_EMAIL}"
            )
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
