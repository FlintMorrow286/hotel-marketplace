"""The partner sees the bookings on their own inventory.

A partner can set up rates, inventory and stop-sell but had no way to see who
actually booked. The guest's /lookup is deliberately blind to
contacts and origin; this is the counterpart the partner needs.
"""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.auth.jwt import create_access_token
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service as booking_service
from app.modules.payment import service as payment_service
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate

TODAY = dt.date.today()


async def _seed(conn: asyncpg.Connection, email: str, name: str = "Дом у моря") -> dict:
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name=name)
    )
    prop = await property_service.create_property(
        conn,
        partner_id,
        PropertyCreate(
            name=name, property_type="house", city="Koktebel", timezone="Europe/Simferopol"
        ),
    )
    row = await conn.fetchrow(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', 2, 1, 3000) RETURNING id::text",
        prop.id,
    )
    assert row is not None
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series($2::date, ($2::date + 59)::date, '1 day') AS d(date)",
        row["id"],
        TODAY,
    )
    return {"unit_type_id": row["id"], "partner_id": str(partner_id)}


def _guest() -> dict:
    return {
        "guest_name": "Иван Гость",
        "guest_email": f"g{uuid.uuid4().hex[:6]}@example.com",
        "guest_phone": "+79991234567",
    }


async def _confirmed(conn: asyncpg.Connection, ut: str, checkin: dt.date, nights: int = 2) -> str:
    b = await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=nights),
        idempotency_key=f"k-{uuid.uuid4()}",
        **_guest(),
    )
    await payment_service.pay_and_confirm(b["id"], conn=conn)
    return b["id"]


async def _hold(conn: asyncpg.Connection, ut: str, checkin: dt.date) -> dict:
    return await booking_service.create_hold(
        conn,
        unit_type_id=ut,
        checkin=checkin,
        checkout=checkin + dt.timedelta(days=2),
        idempotency_key=f"k-{uuid.uuid4()}",
        **_guest(),
    )


def _auth_header(partner_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}


# ----------------------------------------------------------------- the list


@pytest.mark.asyncio
async def test_partner_sees_own_bookings(committed_conn) -> None:
    """A confirmed booking on the partner's inventory shows up with contacts."""
    mine = await _seed(committed_conn, "pb-own@example.com")
    booking_id = await _confirmed(
        committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=10)
    )

    with TestClient(create_app()) as client:
        response = client.get("/v1/bookings/partner/list", headers=_auth_header(mine["partner_id"]))

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    [row] = body
    assert row["id"] == booking_id
    assert row["status"] == "confirmed"
    # The guest's contacts: what the guest's own lookup deliberately omits.
    assert row["guest_name"] == "Иван Гость"
    assert "@" in row["guest_email"]
    assert row["guest_phone"] == "+79991234567"
    # Booked via the site, so no channel is named.
    assert row["origin"] == "web"
    assert row["source_channel"] is None
    # The partner's objects are named, so the row is not a bare id soup.
    assert row["property_name"] == "Дом у моря"
    assert row["unit_type_name"] == "Стандарт"
    # Commission is what the platform takes on this booking.
    assert row["commission_amount"] >= 0
    assert 0 <= row["commission_rate"] <= 1
    # Lines carry the per-night split, matching the guest's view of the price.
    assert len(row["lines"]) == 2
    assert all("date" in ln and "price" in ln for ln in row["lines"])


@pytest.mark.asyncio
async def test_partner_does_not_see_other_partners_bookings(committed_conn) -> None:
    """A booking on someone else's inventory never appears in the list."""
    mine = await _seed(committed_conn, "pb-mine@example.com", name="Мой дом")
    other = await _seed(committed_conn, "pb-other@example.com", name="Чужой дом")
    await _confirmed(committed_conn, other["unit_type_id"], TODAY + dt.timedelta(days=10))

    with TestClient(create_app()) as client:
        response = client.get("/v1/bookings/partner/list", headers=_auth_header(mine["partner_id"]))

    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.asyncio
async def test_holds_and_confirmed_both_show(committed_conn) -> None:
    """A live hold and a confirmed booking are both in the list.

    The hold is what the guest is still paying for; the partner needs to see it
    before the money lands, not after.
    """
    mine = await _seed(committed_conn, "pb-hold@example.com")
    confirmed = await _confirmed(
        committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=20)
    )
    held = await _hold(committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=25))

    with TestClient(create_app()) as client:
        response = client.get("/v1/bookings/partner/list", headers=_auth_header(mine["partner_id"]))

    assert response.status_code == 200, response.text
    statuses = {b["status"]: b["id"] for b in response.json()}
    assert statuses["confirmed"] == confirmed
    assert statuses["hold"] == held["id"]


@pytest.mark.asyncio
async def test_status_filter_narrows_the_list(committed_conn) -> None:
    """?status=confirmed drops holds and cancelled bookings."""
    mine = await _seed(committed_conn, "pb-filter@example.com")
    await _confirmed(committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=30))
    held = await _hold(committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=35))

    with TestClient(create_app()) as client:
        response = client.get(
            "/v1/bookings/partner/list?status=confirmed",
            headers=_auth_header(mine["partner_id"]),
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["id"] != held["id"]


@pytest.mark.asyncio
async def test_list_is_newest_first(committed_conn) -> None:
    """Later bookings come first — the newest arrival is at the top."""
    mine = await _seed(committed_conn, "pb-order@example.com")
    earlier = await _confirmed(committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=10))
    later = await _confirmed(committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=40))

    with TestClient(create_app()) as client:
        response = client.get("/v1/bookings/partner/list", headers=_auth_header(mine["partner_id"]))

    assert response.status_code == 200, response.text
    ids = [b["id"] for b in response.json()]
    assert ids.index(later) < ids.index(earlier)


# ----------------------------------------------------------------- access


@pytest.mark.asyncio
async def test_unauthenticated_is_401(committed_conn) -> None:
    """No token, no partner data."""
    with TestClient(create_app()) as client:
        response = client.get("/v1/bookings/partner/list")

    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_admin_scope_cannot_read_partner_bookings(committed_conn) -> None:
    """An admin token has the admin scope, not the partner one.

    The admin already has /reports/commission for the marketplace-wide view;
    this route is the partner's own.
    """
    mine = await _seed(committed_conn, "pb-scope@example.com")
    await _confirmed(committed_conn, mine["unit_type_id"], TODAY + dt.timedelta(days=10))

    admin_token = create_access_token(mine["partner_id"], scope="admin")
    with TestClient(create_app()) as client:
        response = client.get(
            "/v1/bookings/partner/list", headers={"Authorization": f"Bearer {admin_token}"}
        )

    assert response.status_code == 403, response.text
