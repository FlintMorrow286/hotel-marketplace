from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate


@pytest.mark.asyncio
async def test_catalog_pages_tied_created_at_rows_in_id_order(db_conn) -> None:
    partner_id = await auth_service.register_partner(
        db_conn, PartnerRegisterRequest(email="fix-pagination@example.com", password="secret123", name="Tester")
    )
    tied_at = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    expected_ids = [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    for suffix, property_id in (("first", expected_ids[1]), ("second", expected_ids[0])):
        prop = await property_service.create_property(
            db_conn,
            partner_id,
            PropertyCreate(name=f"Tie {suffix}", property_type="hotel"),
        )
        await db_conn.execute(
            "UPDATE property SET id = $2::uuid, status = 'published', created_at = $3 "
            "WHERE id = $1",
            prop.id,
            property_id,
            tied_at,
        )

    assert await db_conn.fetchval(
        "SELECT count(*) FROM property WHERE created_at = $1", tied_at
    ) == 2
    first = await property_service.list_public_properties(db_conn, limit=1, offset=0)
    second = await property_service.list_public_properties(db_conn, limit=1, offset=1)

    assert [first["items"][0]["id"], second["items"][0]["id"]] == expected_ids
