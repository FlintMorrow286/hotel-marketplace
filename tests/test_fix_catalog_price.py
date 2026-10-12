from __future__ import annotations

import datetime as dt

import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate, PropertyUpdate


@pytest.mark.asyncio
async def test_catalog_prices_only_include_bookable_nights_and_active_rates(db_conn) -> None:
    partner_id = await auth_service.register_partner(
        db_conn,
        PartnerRegisterRequest(
            email="fix-catalog-price@example.com", password="secret123", name="Tester"
        ),
    )
    prop = await property_service.create_property(
        db_conn, partner_id, PropertyCreate(name="Price property", property_type="hotel")
    )
    priced_unit = await db_conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Has inventory', 2, 1, 5000) RETURNING id::text",
        prop.id,
    )
    empty_unit = await db_conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'No inventory', 2, 1, 1000) RETURNING id::text",
        prop.id,
    )
    active_plan = await db_conn.fetchval(
        "INSERT INTO rate_plan (unit_type_id, name, active) "
        "VALUES ($1, 'Active', true) RETURNING id::text",
        priced_unit,
    )
    inactive_plan = await db_conn.fetchval(
        "INSERT INTO rate_plan (unit_type_id, name, active) "
        "VALUES ($1, 'Inactive', false) RETURNING id::text",
        priced_unit,
    )
    empty_plan = await db_conn.fetchval(
        "INSERT INTO rate_plan (unit_type_id, name, active) "
        "VALUES ($1, 'Empty unit active', true) RETURNING id::text",
        empty_unit,
    )

    today = await db_conn.fetchval("SELECT CURRENT_DATE")
    closed_night = today + dt.timedelta(days=1)
    sold_out_night = today + dt.timedelta(days=2)
    active_rate_night = today + dt.timedelta(days=3)
    for day, price in ((closed_night, 1), (sold_out_night, 2), (active_rate_night, 4000)):
        await db_conn.execute(
            "INSERT INTO price_day (rate_plan_id, date, price) VALUES ($1, $2, $3)",
            active_plan,
            day,
            price,
        )
    await db_conn.execute(
        "INSERT INTO price_day (rate_plan_id, date, price) VALUES ($1, $2, 1)",
        inactive_plan,
        active_rate_night,
    )
    await db_conn.execute(
        "INSERT INTO price_day (rate_plan_id, date, price) VALUES ($1, $2, 10)",
        empty_plan,
        active_rate_night,
    )

    await db_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available, closed) VALUES ($1, $2, 1, true)",
        priced_unit,
        closed_night,
    )
    await db_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available, sold) VALUES ($1, $2, 1, 1)",
        priced_unit,
        sold_out_night,
    )
    await db_conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) VALUES ($1, $2, 1)",
        priced_unit,
        active_rate_night,
    )
    await property_service.update_property(
        db_conn, prop.id, partner_id, PropertyUpdate(status="published")
    )

    public = await property_service.get_public_property(db_conn, prop.id)
    assert public is not None
    assert public["min_price"] == 4000
    cards = await property_service.list_public_properties(db_conn)
    assert cards["items"][0]["min_price"] == public["min_price"]

    rooms = await property_service.list_public_unit_types(db_conn, prop.id)
    by_name = {room["name"]: room["price"] for room in rooms}
    assert by_name["Has inventory"] == 4000
    assert by_name["No inventory"] is None
