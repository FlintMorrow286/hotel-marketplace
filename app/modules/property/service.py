"""Property service.

All functions take an `asyncpg.Connection` so tests can pass a rolled-back
transaction.

Access rule: a partner can only touch their own properties. This is enforced
in the routes by passing the partner_id from the JWT.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets

import asyncpg

from app.config.amenities import AMENITY_CATALOG, normalise
from app.modules.property.schemas import PropertyCreate, PropertyOut, PropertyUpdate
from app.utils.logger import get_logger

log = get_logger(__name__)


def _jsonb(v) -> dict:
    """asyncpg returns jsonb columns as str unless a codec is registered."""
    import json

    if isinstance(v, str):
        return json.loads(v)
    return v or {}


def _amenities(v) -> list[str]:
    """The column is a jsonb array of keys; asyncpg hands back a str or list.

    Unknown keys are dropped here too, so a key removed from the catalog never
    reaches the UI as its own label.
    """
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    return normalise(v)


def _make_slug(name: str) -> str:
    """Transliteration-free slug: name → latin-safe short token.

    Cyrillic is kept as-is but stripped of spaces; a random suffix guarantees
    uniqueness. Slice 1 keeps this intentionally simple.
    """
    base = "".join(c if (c.isalnum() or c == "-") else "-" for c in name.lower())
    base = base.strip("-")[:40] or "property"
    return f"{base}-{secrets.token_hex(3)}"


async def create_property(
    conn: asyncpg.Connection, partner_id: str, data: PropertyCreate
) -> PropertyOut:
    row = await conn.fetchrow(
        """
        INSERT INTO property
            (partner_id, name, slug, property_type, city, timezone,
             checkin_time, checkout_time, currency, lat, lng, address,
             amenities, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'draft')
        RETURNING
            id::text, partner_id::text, name, slug, property_type, city, timezone,
            checkin_time, checkout_time, currency, status,
            lat::float8, lng::float8, address::jsonb, amenities::jsonb, created_at
        """,
        partner_id,
        data.name,
        _make_slug(data.name),
        data.property_type,
        data.city,
        data.timezone,
        data.checkin_time,
        data.checkout_time,
        data.currency,
        data.lat,
        data.lng,
        json.dumps(data.address),
        # Normalised, not trusted: the column is free-form jsonb, and what the
        # partner's UI sends is what the guest's UI has to render.
        json.dumps(normalise(data.amenities)),
    )
    assert row is not None
    log.info("property-created", partner_id=partner_id, name=data.name)
    return PropertyOut(
        id=row["id"],
        partner_id=row["partner_id"],
        name=row["name"],
        slug=row["slug"],
        property_type=row["property_type"],
        city=row["city"],
        timezone=row["timezone"],
        checkin_time=row["checkin_time"],
        checkout_time=row["checkout_time"],
        currency=row["currency"],
        status=row["status"],
        lat=row["lat"],
        lng=row["lng"],
        address=_jsonb(row["address"]),
        amenities=_amenities(row["amenities"]),
        created_at=row["created_at"],
    )


async def get_partner_properties(conn: asyncpg.Connection, partner_id: str) -> list[PropertyOut]:
    rows = await conn.fetch(
        """
        SELECT id::text, partner_id::text, name, slug, property_type, city, timezone,
               checkin_time, checkout_time, currency, status,
               lat::float8, lng::float8, address::jsonb, amenities::jsonb, created_at
        FROM property
        WHERE partner_id = $1
        ORDER BY created_at DESC
        """,
        partner_id,
    )
    return [_row_to_out(r) for r in rows]


async def get_property_for_partner(
    conn: asyncpg.Connection, property_id: str, partner_id: str
) -> PropertyOut | None:
    """Fetch a property, but ONLY if it belongs to this partner."""
    row = await conn.fetchrow(
        """
        SELECT id::text, partner_id::text, name, slug, property_type, city, timezone,
               checkin_time, checkout_time, currency, status,
               lat::float8, lng::float8, address::jsonb, amenities::jsonb, created_at
        FROM property
        WHERE id = $1 AND partner_id = $2
        """,
        property_id,
        partner_id,
    )
    return _row_to_out(row) if row else None


async def get_public_property(conn: asyncpg.Connection, property_id: str) -> dict | None:
    """Public catalog read: published only, no partner-internal fields."""
    row = await conn.fetchrow(
        """
        SELECT p.id::text, p.name, p.slug, p.property_type, p.city, p.timezone,
               p.checkin_time, p.checkout_time, p.currency,
               p.lat::float8, p.lng::float8, p.address::jsonb, p.photos::jsonb,
               p.amenities::jsonb,
               (SELECT min(ut.base_price)::float8
                  FROM unit_type ut
                 WHERE ut.property_id = p.id) AS min_price
        FROM property p
        WHERE p.id = $1 AND p.status = 'published'
        """,
        property_id,
    )
    if row is None:
        return None
    return dict(
        row,
        address=_jsonb(row["address"]),
        photos=_jsonb(row["photos"]),
        amenities=_amenities(row["amenities"]),
    )


def _like_escape(text: str) -> str:
    """Make `%`, `_` and `\\` in a guest's search text match themselves, not act as wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_public_properties(
    conn: asyncpg.Connection,
    city: str | None = None,
    guests: int | None = None,
    q: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    amenities: list[str] | None = None,
    limit: int = 24,
    offset: int = 0,
) -> dict:
    """Published catalog with the filters a guest actually narrows on.

    `guests` keeps a property that has *any* room type sleeping that many — a
    hotel is not excluded because its cheapest room is a single. Properties
    without room types never match, which is right: they are not bookable yet.
    `q` matches the name or the city, the two things the search pill offered.
    `date_from`/`date_to` keep a property that has a room type free for the
    whole stay: every night of the half-open interval open and unsold. This is
    the catalog-level view of `inventory.get_availability`, so the two never
    disagree about what is bookable.
    `amenities` keeps a property that offers *all* of the asked keys: a guest
    filtering on Wi-Fi and parking wants both, not one of the two. Unknown
    keys never match, so a stale URL cannot empty the catalog — the catalog
    itself is the source of truth for which keys exist.
    Returns a page plus the total, so the UI can page without fetching all.
    """
    args: list = []
    where = "p.status = 'published'"
    if city:
        args.append(city)
        where += f" AND p.city = ${len(args)}"
    if guests:
        args.append(guests)
        where += (
            f" AND EXISTS (SELECT 1 FROM unit_type ut"
            f" WHERE ut.property_id = p.id AND ut.capacity >= ${len(args)})"
        )
    if q:
        args.append(f"%{_like_escape(q)}%")
        where += f" AND (p.name ILIKE ${len(args)} OR p.city ILIKE ${len(args)})"
    if date_from and date_to and date_to > date_from:
        # Half-open [date_from, date_to), same as the availability read: the
        # checkout day is not a stay. Every night of the stay must have a row
        # that is open and still free — a missing row means the generator
        # never ran for that room, so it is not bookable, and the catalog
        # must not offer it. This mirrors inventory.get_availability, which
        # treats a missing day as unavailable.
        # (unit_type, date) is the key, so "every night has such a row" is "as many
        # such rows as nights". A night generated per day of the range cost whatever a
        # client sent: year 1 to 9999 took two seconds, and `date_from=0001-01-01`
        # (asyncpg sends date.min as -infinity) never finished, filling the disk with
        # temp files. The nights are counted here: infinite dates cannot be subtracted.
        nights = (date_to - date_from).days
        args += [date_from, date_to, nights]
        first, last, count = len(args) - 2, len(args) - 1, len(args)
        where += (
            f" AND EXISTS (SELECT 1 FROM unit_type ut"
            f" WHERE ut.property_id = p.id"
            f" AND (SELECT count(*) FROM inventory_day i"
            f"  WHERE i.unit_type_id = ut.id AND i.date >= ${first} AND i.date < ${last}"
            f"    AND NOT i.closed AND i.available - i.hold - i.sold > 0)"
            f"  = ${count})"
        )
    if amenities:
        # Only known keys can be in the column (the write path normalises),
        # but a request may still carry a key the catalog dropped in a
        # release — filter it out here rather than answering an empty page.
        wanted = [a for a in amenities if a in AMENITY_CATALOG]
        if not wanted:
            # Nothing the catalog recognises: the filter cannot match
            # anything, and returning everything would ignore the guest.
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        args.append(json.dumps(wanted))
        where += (
            # jsonb @> covers the containment; the cast keeps the planner on
            # the expression index over the cast column.
            f" AND p.amenities::jsonb @> ${len(args)}::jsonb"
        )
    total = await conn.fetchval(
        f"SELECT count(*) FROM property p WHERE {where}",
        *args,
    )

    args.append(limit)
    args.append(offset)
    rows = await conn.fetch(
        f"""
        SELECT p.id::text, p.name, p.slug, p.property_type, p.city, p.timezone,
               p.checkin_time, p.checkout_time, p.currency,
               p.lat::float8, p.lng::float8, p.address::jsonb, p.photos::jsonb,
               p.amenities::jsonb,
               (SELECT min(ut.base_price)::float8
                  FROM unit_type ut
                 WHERE ut.property_id = p.id) AS min_price
        FROM property p
        WHERE {where}
        ORDER BY p.created_at DESC
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    out = []
    for r in rows:
        row = dict(
            r,
            address=_jsonb(r["address"]),
            photos=_jsonb(r["photos"]),
            amenities=_amenities(r["amenities"]),
        )
        # No room types yet: the property is published but unbookable, and
        # min_price None is exactly that — the UI says "цена не указана".
        row["min_price"] = r["min_price"]
        out.append(row)
    return {"items": out, "total": total, "limit": limit, "offset": offset}


async def update_property(
    conn: asyncpg.Connection, property_id: str, partner_id: str, data: PropertyUpdate
) -> PropertyOut | None:
    """Partial update; returns None if the property is not owned by partner."""
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        saved = await get_property_for_partner(conn, property_id, partner_id)
        return saved

    if "address" in fields:
        fields["address"] = json.dumps(fields["address"])
    if "amenities" in fields:
        # Same normalisation as create: unknown keys die here, order survives.
        fields["amenities"] = json.dumps(normalise(fields["amenities"]))
    cols = list(fields)
    assignments = ", ".join(f"{c} = ${i + 3}" for i, c in enumerate(cols))
    values = [property_id, partner_id, *[fields[c] for c in cols]]

    row = await conn.fetchrow(
        f"""
        UPDATE property
        SET {assignments}
        WHERE id = $1 AND partner_id = $2
        RETURNING id::text, partner_id::text, name, slug, property_type, city, timezone,
                  checkin_time, checkout_time, currency, status,
                  lat::float8, lng::float8, address::jsonb, amenities::jsonb, created_at
        """,
        *values,
    )
    return _row_to_out(row) if row else None


def _row_to_out(row: asyncpg.Record) -> PropertyOut:
    return PropertyOut(
        id=row["id"],
        partner_id=row["partner_id"],
        name=row["name"],
        slug=row["slug"],
        property_type=row["property_type"],
        city=row["city"],
        timezone=row["timezone"],
        checkin_time=row["checkin_time"],
        checkout_time=row["checkout_time"],
        currency=row["currency"],
        status=row["status"],
        lat=row["lat"],
        lng=row["lng"],
        address=_jsonb(row["address"]),
        amenities=_amenities(row["amenities"]),
        created_at=row["created_at"],
    )


async def list_public_unit_types(conn: asyncpg.Connection, property_id: str) -> list[dict]:
    """The guest's room list for one published property, cheapest price included.

    A rate plan overrides base_price when it has rows for the date; see
    rate.get_effective_price for the same fallback in the availability path.
    The price shown is the cheapest night across the next 30 days, so a guest
    browsing the catalog sees what the stay actually costs instead of reaching
    the checkout to find out.
    """
    rows = await conn.fetch(
        """
        SELECT ut.id::text, ut.property_id::text, ut.name, ut.capacity,
               ut.total_units, ut.base_price::float8,
               COALESCE(
                 (SELECT min(pd.price)::float8
                  FROM price_day pd
                  JOIN rate_plan rp ON rp.id = pd.rate_plan_id
                  WHERE rp.unit_type_id = ut.id AND rp.active
                    AND pd.date >= current_date
                    AND pd.date < current_date + interval '30 days'
                    AND pd.stop_sell = false),
                 ut.base_price::float8
               ) AS price,
               -- The guest needs the room's cancellation terms before paying.
               -- A room with no rate plan has no terms yet, so it stays null
               -- and the UI says so rather than inventing a default.
               (SELECT rp.cancellation_policy
                  FROM rate_plan rp
                 WHERE rp.unit_type_id = ut.id AND rp.active
                 ORDER BY rp.created_at
                 LIMIT 1) AS cancellation_policy
        FROM unit_type ut
        WHERE ut.property_id = $1
        ORDER BY ut.created_at
        """,
        property_id,
    )
    return [dict(r) for r in rows]
