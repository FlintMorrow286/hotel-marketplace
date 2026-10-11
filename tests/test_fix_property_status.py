from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import routes as property_routes
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate, PropertyUpdate


async def _create_property(conn, email: str):
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await property_service.create_property(
        conn, partner_id, PropertyCreate(name="Test property", property_type="hotel")
    )
    return partner_id, prop


@pytest.mark.asyncio
async def test_partner_cannot_enter_or_leave_blocked_status(db_conn) -> None:
    partner_id, prop = await _create_property(db_conn, "fix-blocked@example.com")

    with pytest.raises(ValueError, match="illegal partner transition"):
        await property_service.update_property(
            db_conn, prop.id, partner_id, PropertyUpdate(status="blocked")
        )

    await db_conn.execute("UPDATE property SET status = 'blocked' WHERE id = $1", prop.id)
    with pytest.raises(ValueError, match="illegal partner transition"):
        await property_service.update_property(
            db_conn, prop.id, partner_id, PropertyUpdate(status="published")
        )
    assert await db_conn.fetchval("SELECT status FROM property WHERE id = $1", prop.id) == "blocked"


@pytest.mark.asyncio
async def test_partner_can_still_self_publish_from_draft(db_conn) -> None:
    partner_id, prop = await _create_property(db_conn, "fix-self-publish@example.com")

    updated = await property_service.update_property(
        db_conn, prop.id, partner_id, PropertyUpdate(status="published")
    )

    assert updated is not None
    assert updated.status == "published"


@pytest.mark.asyncio
async def test_blocked_partner_transition_is_http_conflict(monkeypatch, committed_conn) -> None:
    partner_id, prop = await _create_property(committed_conn, "fix-blocked-route@example.com")

    class _Pool:
        async def acquire(self):
            return committed_conn

        async def release(self, conn):
            assert conn is committed_conn

    monkeypatch.setattr(property_routes, "get_pool", lambda: _Pool())
    with pytest.raises(HTTPException) as exc_info:
        await property_routes.update_my_property(
            prop.id,
            PropertyUpdate(status="blocked"),
            SimpleNamespace(sub=partner_id),
        )

    assert exc_info.value.status_code == 409
    assert "illegal partner transition" in exc_info.value.detail
