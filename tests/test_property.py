"""Slice 1 tests: property CRUD + partner isolation.

Every test runs inside a rolled-back transaction (see conftest.py), so created
properties never persist between tests.
"""

import json

import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service
from app.modules.property.schemas import PropertyCreate, PropertyUpdate


async def _make_partner(conn, email: str) -> str:
    return await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )


def _sample(name: str = "Апартаменты у моря", city: str = "Yalta") -> PropertyCreate:
    return PropertyCreate(
        name=name,
        property_type="apartment",
        city=city,
        timezone="Europe/Simferopol",
    )


# ---------------------------------------------------------------- create / list


@pytest.mark.asyncio
async def test_create_and_list_property(db_conn) -> None:
    partner_id = await _make_partner(db_conn, "p1@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())

    assert created.partner_id == partner_id
    assert created.status == "draft"  # never published immediately
    assert created.slug and created.slug != ""

    props = await service.get_partner_properties(db_conn, partner_id)
    assert len(props) == 1
    assert props[0].id == created.id


@pytest.mark.asyncio
async def test_create_two_partners_isolation(db_conn) -> None:
    """The core access rule: a partner sees only their own properties."""
    partner_a = await _make_partner(db_conn, "a@example.com")
    partner_b = await _make_partner(db_conn, "b@example.com")

    await service.create_property(db_conn, partner_a, _sample("Квартира A"))
    await service.create_property(db_conn, partner_b, _sample("Квартира B"))

    a_list = await service.get_partner_properties(db_conn, partner_a)
    b_list = await service.get_partner_properties(db_conn, partner_b)

    assert [p.name for p in a_list] == ["Квартира A"]
    assert [p.name for p in b_list] == ["Квартира B"]


# ---------------------------------------------------------------- update


@pytest.mark.asyncio
async def test_update_own_property(db_conn) -> None:
    partner_id = await _make_partner(db_conn, "p2@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())
    upd = PropertyUpdate(name="Апартаменты у гор", city="Sevastopol")

    updated = await service.update_property(db_conn, created.id, partner_id, upd)

    assert updated is not None
    assert updated.name == "Апартаменты у гор"
    assert updated.city == "Sevastopol"


@pytest.mark.asyncio
async def test_cannot_update_other_partners_property(db_conn) -> None:
    """Partner B must not be able to touch partner A's property."""
    partner_a = await _make_partner(db_conn, "a2@example.com")
    partner_b = await _make_partner(db_conn, "b2@example.com")
    created = await service.create_property(db_conn, partner_a, _sample())

    result = await service.update_property(
        db_conn, created.id, partner_b, PropertyUpdate(name="HACKED")
    )

    assert result is None  # not found for this partner


# ---------------------------------------------------------------- public catalog


@pytest.mark.asyncio
async def test_draft_property_hidden_from_catalog(db_conn) -> None:
    """The catalog shows only published properties."""
    partner_id = await _make_partner(db_conn, "p3@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())

    # draft → not visible
    assert await service.get_public_property(db_conn, created.id) is None
    assert (await service.list_public_properties(db_conn))["items"] == []

    # publish it
    await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(status="published")
    )

    public = await service.get_public_property(db_conn, created.id)
    assert public is not None
    assert public["name"] == created.name
    # partner_id must never leak into the public payload
    assert "partner_id" not in public


@pytest.mark.asyncio
async def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValueError):
        PropertyCreate(name="X", property_type="apartment", timezone="Not/AZone")


# ---------------------------------------------------------------- public rooms


@pytest.mark.asyncio
async def test_public_unit_types_listed_for_published_property(db_conn) -> None:
    """A guest booking the catalog needs the room list, unauthenticated.

    The route is a thin wrapper over the pool, so the rule itself is checked
    here: the room list comes from the same query the guest reads, and a draft
    property's rooms are not published either.
    """
    partner_id = await _make_partner(db_conn, "p4@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())

    async with db_conn.transaction():
        ut_id = await db_conn.fetchval(
            "INSERT INTO unit_type (property_id, name, capacity, total_units) "
            "VALUES ($1, 'Стандарт', 2, 3) RETURNING id::text",
            created.id,
        )
    assert ut_id is not None

    # draft → the room list must not serve an unpublished property
    assert await service.get_public_property(db_conn, created.id) is None

    await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(status="published")
    )

    # published → the same room query serves the guest
    public = await service.get_public_property(db_conn, created.id)
    assert public is not None
    rows = await db_conn.fetch(
        "SELECT name, total_units FROM unit_type WHERE property_id = $1 ORDER BY created_at",
        created.id,
    )
    assert [r["name"] for r in rows] == ["Стандарт"]
    assert rows[0]["total_units"] == 3


@pytest.mark.asyncio
async def test_public_room_carries_its_cancellation_policy(db_conn) -> None:
    """A guest paying needs the room's cancellation terms next to its price.

    The room's active rate plan is the only source of terms; without one the
    field is null and the UI says so instead of inventing 'flexible'.
    """
    from app.modules.rate import service as rate_service

    partner_id = await _make_partner(db_conn, "policy-1@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())
    await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(status="published")
    )
    ut_id = await db_conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', 2, 1, 4000) RETURNING id::text",
        created.id,
    )

    rooms = await service.list_public_unit_types(db_conn, created.id)
    assert rooms[0]["cancellation_policy"] is None

    await rate_service.create_rate_plan(
        db_conn, ut_id, "Невозвратный", cancellation_policy="strict"
    )

    rooms = await service.list_public_unit_types(db_conn, created.id)
    assert rooms[0]["cancellation_policy"] == "strict"


async def test_public_route_exposes_the_policy(committed_conn) -> None:
    """The HTTP route answers the same policy the service computes."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.rate import service as rate_service

    partner_id = await _make_partner(committed_conn, "policy-2@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())
    await service.update_property(
        committed_conn, created.id, partner_id, PropertyUpdate(status="published")
    )
    ut_id = await committed_conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', 2, 1, 4000) RETURNING id::text",
        created.id,
    )
    await rate_service.create_rate_plan(
        committed_conn, ut_id, "Модерат", cancellation_policy="moderate"
    )

    # committed_conn is autocommit, so the app pool sees these rows.
    with TestClient(create_app()) as client:
        resp = client.get(f"/v1/public/unit-types/{created.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["cancellation_policy"] == "moderate"


# ---------------------------------------------------------------- publish route


@pytest.mark.asyncio
async def test_partner_publishes_own_property_over_http(committed_conn) -> None:
    """The cabinet's Publish button: PATCH /partner/properties/{id} to published.

    A property starts as a draft and stays invisible to guests until the
    partner publishes it. This is the route that button hits.
    """
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "pub-1@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/properties/{created.id}",
            json={"status": "published"},
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
    # The guest sees it now — the catalog is what publish unlocks.
    public = (await service.list_public_properties(committed_conn))["items"]
    assert any(p["id"] == created.id for p in public)


@pytest.mark.asyncio
async def test_partner_cannot_publish_other_partners_property(committed_conn) -> None:
    """PATCH on a stranger's property answers 404, never publishes it."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_a = await _make_partner(committed_conn, "pub-a@example.com")
    partner_b = await _make_partner(committed_conn, "pub-b@example.com")
    created = await service.create_property(committed_conn, partner_a, _sample())

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/properties/{created.id}",
            json={"status": "published"},
            headers={"Authorization": f"Bearer {create_access_token(partner_b, scope='partner')}"},
        )

    assert response.status_code == 404, response.text
    assert await service.get_public_property(committed_conn, created.id) is None


@pytest.mark.asyncio
async def test_publish_route_requires_partner_scope(committed_conn) -> None:
    """An admin token cannot publish through the partner route."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "pub-scope@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/properties/{created.id}",
            json={"status": "published"},
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='admin')}"},
        )

    assert response.status_code == 403, response.text
    assert await service.get_public_property(committed_conn, created.id) is None


@pytest.mark.asyncio
async def test_unpublish_returns_property_to_draft(committed_conn) -> None:
    """Published → draft hides the property from guests again.

    The cabinet needs the reverse action too: a property taken offline stops
    being bookable immediately.
    """
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "pub-down@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())
    await service.update_property(
        committed_conn, created.id, partner_id, PropertyUpdate(status="published")
    )

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/properties/{created.id}",
            json={"status": "draft"},
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"
    assert (await service.list_public_properties(committed_conn))["items"] == []


# ------------------------------------------------------------------ room price


@pytest.mark.asyncio
async def test_create_unit_type_carries_base_price(committed_conn) -> None:
    """The room's base price arrives with the room, not in a second step.

    Without it the inventory exists but every night reads 0 in the catalog and
    in the booking total: a rate plan is empty by default and the effective
    price falls back to base_price.
    """
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "price-1@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())
    headers = {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/partner/unit-types",
            json={
                "property_id": created.id,
                "name": "Стандарт",
                "capacity": 2,
                "total_units": 3,
                "base_price": 4500,
            },
            headers=headers,
        )

    assert response.status_code == 201, response.text
    assert response.json()["base_price"] == 4500

    # The partner list echoes it back: the cabinet shows what it saved.
    with TestClient(create_app()) as client:
        response = client.get(
            f"/v1/partner/unit-types/property/{created.id}",
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.json()[0]["base_price"] == 4500

    # And the guest's room list carries the same number.
    await service.update_property(
        committed_conn, created.id, partner_id, PropertyUpdate(status="published")
    )
    public = await service.list_public_unit_types(committed_conn, created.id)
    assert public and public[0]["price"] == 4500


@pytest.mark.asyncio
async def test_unit_type_base_price_defaults_to_zero(committed_conn) -> None:
    """An omitted price is 0, not an error: the room still becomes bookable,
    and RateManager is where the partner sets the real number later."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "price-2@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/partner/unit-types",
            json={
                "property_id": created.id,
                "name": "Стандарт",
                "capacity": 2,
                "total_units": 1,
            },
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"},
        )

    assert response.status_code == 201, response.text
    assert response.json()["base_price"] == 0


@pytest.mark.asyncio
async def test_negative_base_price_rejected(committed_conn) -> None:
    """A negative price is a client error, never a row in the database."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "price-3@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/partner/unit-types",
            json={
                "property_id": created.id,
                "name": "Стандарт",
                "capacity": 2,
                "total_units": 1,
                "base_price": -100,
            },
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"},
        )

    assert response.status_code == 422, response.text


# ------------------------------------------------------------------ catalog price


@pytest.mark.asyncio
async def test_published_property_carries_cheapest_night(db_conn) -> None:
    """The catalog card shows the cheapest night, so a guest compares objects
    before opening one. The base price is used when no rate plan overrides it;
    the cheapest room has inventory for the current night.
    """
    partner_id = await _make_partner(db_conn, "min-1@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())

    async with db_conn.transaction():
        budget_id = await db_conn.fetchval(
            "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
            "VALUES ($1, 'Бюджет', 2, 1, 3200) RETURNING id::text",
            created.id,
        )
        await db_conn.execute(
            "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
            "VALUES ($1, 'Люкс', 4, 2, 8900)",
            created.id,
        )
        await db_conn.execute(
            "INSERT INTO inventory_day (unit_type_id, date, available) "
            "VALUES ($1, CURRENT_DATE, 1)",
            budget_id,
        )

    await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(status="published")
    )

    single = await service.get_public_property(db_conn, created.id)
    assert single is not None
    assert single["min_price"] == 3200

    rows = (await service.list_public_properties(db_conn))["items"]
    match = next(p for p in rows if p["id"] == created.id)
    assert match["min_price"] == 3200


@pytest.mark.asyncio
async def test_property_without_rooms_has_no_min_price(db_conn) -> None:
    """A published property with no room types is not bookable yet. min_price
    is None, and the card says 'цена не указана' instead of promising a 0."""
    partner_id = await _make_partner(db_conn, "min-2@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())

    await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(status="published")
    )

    single = await service.get_public_property(db_conn, created.id)
    assert single is not None
    assert single["min_price"] is None


# ------------------------------------------------------------------ room editing


@pytest.mark.asyncio
async def test_partner_reprices_own_unit_type(committed_conn) -> None:
    """A room created with a wrong price is fixed without a rate plan.

    base_price is the fallback every unpriced day reads, so this one call
    reprices the whole season the partner never touched in RateManager.
    """
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "edit-1@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())
    headers = {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/partner/unit-types",
            json={
                "property_id": created.id,
                "name": "Стандарт",
                "capacity": 2,
                "total_units": 2,
                "base_price": 1000,
            },
            headers=headers,
        )
    unit_id = response.json()["id"]

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/unit-types/{unit_id}",
            json={"base_price": 5200},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["base_price"] == 5200
    # The catalog's "от X ₽" follows the room, not just the partner list.
    await service.update_property(
        committed_conn, created.id, partner_id, PropertyUpdate(status="published")
    )
    public = (await service.list_public_properties(committed_conn))["items"]
    match = next(p for p in public if p["id"] == created.id)
    assert match["min_price"] == 5200


@pytest.mark.asyncio
async def test_partner_cannot_edit_other_partners_unit_type(committed_conn) -> None:
    """PATCH on a stranger's room answers 404 and never changes the price."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    owner = await _make_partner(committed_conn, "edit-a@example.com")
    intruder = await _make_partner(committed_conn, "edit-b@example.com")
    created = await service.create_property(committed_conn, owner, _sample())

    with TestClient(create_app()) as client:
        create = client.post(
            "/v1/partner/unit-types",
            json={
                "property_id": created.id,
                "name": "Стандарт",
                "capacity": 2,
                "total_units": 1,
                "base_price": 3000,
            },
            headers={"Authorization": f"Bearer {create_access_token(owner, scope='partner')}"},
        )
    unit_id = create.json()["id"]

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/unit-types/{unit_id}",
            json={"base_price": 10},
            headers={"Authorization": f"Bearer {create_access_token(intruder, scope='partner')}"},
        )

    assert response.status_code == 404, response.text
    saved = await committed_conn.fetchval(
        "SELECT base_price::float8 FROM unit_type WHERE id = $1", unit_id
    )
    assert saved == 3000.0


@pytest.mark.asyncio
async def test_empty_unit_type_update_is_rejected(committed_conn) -> None:
    """An empty PATCH is a client error: nothing to save, and answering 200
    would make a UI miss look like a success."""
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "edit-empty@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())

    with TestClient(create_app()) as client:
        create = client.post(
            "/v1/partner/unit-types",
            json={
                "property_id": created.id,
                "name": "Стандарт",
                "capacity": 2,
                "total_units": 1,
            },
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"},
        )
    unit_id = create.json()["id"]

    with TestClient(create_app()) as client:
        response = client.patch(
            f"/v1/partner/unit-types/{unit_id}",
            json={},
            headers={"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"},
        )

    assert response.status_code == 422, response.text


# ------------------------------------------------------------------ guest filter


async def _seed_rooms(conn, property_id: str, capacities: list[int]) -> None:
    """Stand up room types of the given capacities; price irrelevant here."""
    for i, cap in enumerate(capacities):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
                "VALUES ($1, $2, $3, 1, 1000)",
                property_id,
                f"Комната {i}",
                cap,
            )


@pytest.mark.asyncio
async def test_guests_filter_keeps_property_that_fits(db_conn) -> None:
    """A property is kept when any of its room types sleeps the party.

    A hotel must not vanish because its cheapest room is a single: the filter
    looks for a room type with capacity >= guests, not at the property.
    """
    partner_id = await _make_partner(db_conn, "guests-1@example.com")
    hotel = await service.create_property(
        db_conn, partner_id, PropertyCreate(name="Отель", property_type="hotel", city="Yalta")
    )
    await _seed_rooms(db_conn, hotel.id, [2, 4])
    await service.update_property(db_conn, hotel.id, partner_id, PropertyUpdate(status="published"))

    for guests in (1, 2, 3, 4):
        rows = (await service.list_public_properties(db_conn, guests=guests))["items"]
        assert [p["id"] for p in rows] == [hotel.id], guests

    # A party of five does not fit any room.
    rows = (await service.list_public_properties(db_conn, guests=5))["items"]
    assert rows == []


@pytest.mark.asyncio
async def test_guests_filter_excludes_property_without_rooms(db_conn) -> None:
    """A published property with no room types matches nothing — it is not
    bookable, so the filter must not promise a stay."""
    partner_id = await _make_partner(db_conn, "guests-2@example.com")
    empty = await service.create_property(
        db_conn, partner_id, PropertyCreate(name="Пустой", property_type="house", city="Yalta")
    )
    await service.update_property(db_conn, empty.id, partner_id, PropertyUpdate(status="published"))

    assert (await service.list_public_properties(db_conn, guests=1))["items"] == []
    # Without a filter the property is still in the catalog.
    rows = (await service.list_public_properties(db_conn))["items"]
    assert [p["id"] for p in rows] == [empty.id]


@pytest.mark.asyncio
async def test_guests_filter_combines_with_city(db_conn) -> None:
    """The two filters compose: guests narrow first, city among the rest."""
    partner_id = await _make_partner(db_conn, "guests-3@example.com")
    yalta = await service.create_property(
        db_conn, partner_id, PropertyCreate(name="Ялта", property_type="hotel", city="Yalta")
    )
    sevastopol = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(name="Севастополь", property_type="hotel", city="Sevastopol"),
    )
    await _seed_rooms(db_conn, yalta.id, [4])
    await _seed_rooms(db_conn, sevastopol.id, [2])
    await service.update_property(db_conn, yalta.id, partner_id, PropertyUpdate(status="published"))
    await service.update_property(
        db_conn, sevastopol.id, partner_id, PropertyUpdate(status="published")
    )

    rows = (await service.list_public_properties(db_conn, city="Yalta", guests=4))["items"]
    assert [p["id"] for p in rows] == [yalta.id]

    # Sevastopol has rooms, but not for four.
    rows = (await service.list_public_properties(db_conn, city="Sevastopol", guests=4))["items"]
    assert rows == []


# ------------------------------------------------------------- search & paging


async def _publish_n(conn, partner_id: str, n: int) -> list[str]:
    """Create and publish n properties named 'Отель k'; returns their ids."""
    ids = []
    for k in range(n):
        prop = await service.create_property(
            conn,
            partner_id,
            PropertyCreate(name=f"Отель {k}", property_type="hotel", city="Yalta"),
        )
        await service.update_property(conn, prop.id, partner_id, PropertyUpdate(status="published"))
        ids.append(prop.id)
    return ids


@pytest.mark.asyncio
async def test_catalog_search_matches_name_or_city(db_conn) -> None:
    """The search pill queries the server, so the filter reaches guests on
    page 2 too — a client-side filter would have missed them."""
    partner_id = await _make_partner(db_conn, "search-1@example.com")
    await _publish_n(db_conn, partner_id, 1)
    # A property in another city that the name query must not catch.
    other = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(name="Дом у моря", property_type="house", city="Alushta"),
    )
    await service.update_property(db_conn, other.id, partner_id, PropertyUpdate(status="published"))

    page = await service.list_public_properties(db_conn, q="отель")
    assert page["total"] == 1
    assert page["items"][0]["name"] == "Отель 0"

    page = await service.list_public_properties(db_conn, q="alushta")
    assert page["total"] == 1
    assert page["items"][0]["city"] == "Alushta"

    page = await service.list_public_properties(db_conn, q="фыва")
    assert page["total"] == 0
    assert page["items"] == []


@pytest.mark.asyncio
async def test_catalog_returns_pages_and_total(db_conn) -> None:
    """limit/offset slice the catalog; total is the whole filtered set, so
    the UI can page without fetching everything."""
    partner_id = await _make_partner(db_conn, "search-2@example.com")
    ids = await _publish_n(db_conn, partner_id, 5)
    ordered_ids = sorted(ids)

    first = await service.list_public_properties(db_conn, limit=2, offset=0)
    assert first["total"] == 5
    assert [p["id"] for p in first["items"]] == ordered_ids[:2]

    second = await service.list_public_properties(db_conn, limit=2, offset=2)
    assert second["total"] == 5
    assert [p["id"] for p in second["items"]] == ordered_ids[2:4]

    past_end = await service.list_public_properties(db_conn, limit=2, offset=4)
    assert past_end["total"] == 5
    assert [p["id"] for p in past_end["items"]] == ordered_ids[4:]


@pytest.mark.asyncio
async def test_catalog_search_combines_with_guests(db_conn) -> None:
    """Search and the guests filter apply together."""
    partner_id = await _make_partner(db_conn, "search-3@example.com")
    await _publish_n(db_conn, partner_id, 1)
    big = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(name="Отель большой", property_type="hotel", city="Yalta"),
    )
    async with db_conn.transaction():
        await db_conn.execute(
            "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
            "VALUES ($1, 'Семейный', 6, 2, 3000)",
            big.id,
        )
    await service.update_property(db_conn, big.id, partner_id, PropertyUpdate(status="published"))

    page = await service.list_public_properties(db_conn, q="отель", guests=6)
    assert page["total"] == 1
    assert page["items"][0]["id"] == big.id

    page = await service.list_public_properties(db_conn, q="отель", guests=10)
    assert page["total"] == 0


@pytest.mark.asyncio
async def test_malformed_uuid_path_is_404_not_500(committed_conn) -> None:
    """A non-uuid id must answer 404, not leak a Postgres syntax error.

    Unauthenticated public reads are the surface a guest can reach, so the
    check is on them; the handler is app-wide and covers the rest.
    """
    from starlette.testclient import TestClient

    from app.main import create_app

    # committed_conn only exists to bring the test database up; nothing is
    # seeded because the request must never reach a query result.
    assert committed_conn is not None

    with TestClient(create_app()) as client:
        property_resp = client.get("/v1/properties/not-a-uuid")
        units_resp = client.get("/v1/public/unit-types/not-a-uuid")

    assert property_resp.status_code == 404, property_resp.text
    assert units_resp.status_code == 404, units_resp.text


@pytest.mark.asyncio
async def test_catalog_page_past_the_end_keeps_total(db_conn) -> None:
    """offset beyond the last row is an empty page, not an error."""
    partner_id = await _make_partner(db_conn, "page-end@example.com")
    await _publish_n(db_conn, partner_id, 2)
    page = await service.list_public_properties(db_conn, limit=10, offset=100)
    assert page["total"] == 2
    assert page["items"] == []


async def _publish_with_room(
    conn, partner_id: str, email: str, base_price: int = 5000, capacity: int = 2
) -> str:
    """A published property with one bookable room and generated inventory.

    The room's inventory_day rows must exist for the date filter to see the
    room as free, which is also what makes it bookable at all.
    """
    from app.modules.inventory import service as inventory_service

    prop = await service.create_property(conn, partner_id, _sample(email + " house", city="Yalta"))
    await service.update_property(conn, prop.id, partner_id, PropertyUpdate(status="published"))
    unit_id = await conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', $2, 2, $3) RETURNING id::text",
        prop.id,
        capacity,
        base_price,
    )
    await inventory_service.ensure_inventory(conn, unit_id)
    return prop.id


@pytest.mark.asyncio
async def test_date_filter_keeps_property_free_for_the_whole_stay(db_conn) -> None:
    """A property with a room open every night of the stay is a match."""
    import datetime as dt

    partner_id = await _make_partner(db_conn, "dates-1@example.com")
    await _publish_with_room(db_conn, partner_id, "dates-1")

    stay_from = dt.date.today() + dt.timedelta(days=20)
    stay_to = stay_from + dt.timedelta(days=3)

    page = await service.list_public_properties(db_conn, date_from=stay_from, date_to=stay_to)
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_date_filter_drops_property_with_one_closed_night(db_conn) -> None:
    """One closed night inside the stay makes the property unavailable.

    The guest cannot skip the night, so partial availability is not a match —
    same answer GET /availability would give per room.
    """
    import datetime as dt

    from app.modules.inventory import service as inventory_service

    partner_id = await _make_partner(db_conn, "dates-2@example.com")
    prop_id = await _publish_with_room(db_conn, partner_id, "dates-2")
    unit_id = await db_conn.fetchval(
        "SELECT id::text FROM unit_type WHERE property_id = $1", prop_id
    )

    closed = dt.date.today() + dt.timedelta(days=21)
    await inventory_service.close_range(
        db_conn, unit_id, closed, closed + dt.timedelta(days=1), closed=True
    )

    stay_from = dt.date.today() + dt.timedelta(days=20)
    stay_to = stay_from + dt.timedelta(days=3)
    page = await service.list_public_properties(db_conn, date_from=stay_from, date_to=stay_to)
    assert page["total"] == 0


@pytest.mark.asyncio
async def test_date_filter_drops_property_with_no_inventory(db_conn) -> None:
    """A published property whose room has no inventory rows is not bookable.

    Missing rows are unavailable, so the property must not be offered for the
    stay — offering it would send the guest to a checkout that cannot confirm.
    """
    import datetime as dt

    partner_id = await _make_partner(db_conn, "dates-3@example.com")
    prop = await service.create_property(
        db_conn, partner_id, _sample("Нет инвентаря", city="Yalta")
    )
    await service.update_property(db_conn, prop.id, partner_id, PropertyUpdate(status="published"))
    await db_conn.execute(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', 2, 1, 5000)",
        prop.id,
    )

    stay_from = dt.date.today() + dt.timedelta(days=20)
    stay_to = stay_from + dt.timedelta(days=2)
    page = await service.list_public_properties(db_conn, date_from=stay_from, date_to=stay_to)
    assert page["total"] == 0


@pytest.mark.asyncio
async def test_date_filter_combines_with_guests(db_conn) -> None:
    """Dates and guests narrow together."""
    import datetime as dt

    partner_id = await _make_partner(db_conn, "dates-4@example.com")
    await _publish_with_room(db_conn, partner_id, "dates-4", capacity=4)

    stay_from = dt.date.today() + dt.timedelta(days=20)
    stay_to = stay_from + dt.timedelta(days=2)

    matching = await service.list_public_properties(
        db_conn, date_from=stay_from, date_to=stay_to, guests=4
    )
    assert matching["total"] == 1

    too_many = await service.list_public_properties(
        db_conn, date_from=stay_from, date_to=stay_to, guests=6
    )
    assert too_many["total"] == 0


@pytest.mark.asyncio
async def test_date_filter_reversed_range_is_ignored(db_conn) -> None:
    """date_to <= date_from is not a stay; the filter is skipped, not an error.

    The availability read raises on this, but the catalog treats a nonsense
    range as "no date filter" rather than refusing to answer.
    """
    import datetime as dt

    partner_id = await _make_partner(db_conn, "dates-5@example.com")
    await _publish_with_room(db_conn, partner_id, "dates-5")

    today = dt.date.today()
    page = await service.list_public_properties(
        db_conn, date_from=today + dt.timedelta(days=5), date_to=today
    )
    # No date filter applied, so the property is visible again.
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_date_filter_agrees_with_availability_read(db_conn) -> None:
    """What the catalog offers, GET /availability must call free.

    This is the invariant that makes the date filter trustworthy: the catalog
    never shows a property whose rooms the availability read reports closed
    or sold out for the same nights.
    """
    import datetime as dt

    from app.modules.inventory import service as inventory_service

    partner_id = await _make_partner(db_conn, "dates-6@example.com")
    prop_id = await _publish_with_room(db_conn, partner_id, "dates-6")
    unit_id = await db_conn.fetchval(
        "SELECT id::text FROM unit_type WHERE property_id = $1", prop_id
    )

    stay_from = dt.date.today() + dt.timedelta(days=20)
    stay_to = stay_from + dt.timedelta(days=3)

    # The catalog keeps it...
    page = await service.list_public_properties(db_conn, date_from=stay_from, date_to=stay_to)
    assert page["total"] == 1

    # ...and every night of the stay is free for that room.
    days = await inventory_service.get_availability(db_conn, unit_id, stay_from, stay_to)
    assert len(days) == 3
    assert all(d["free"] > 0 and not d["closed"] for d in days)


# ---------------------------------------------------------------- amenities


@pytest.mark.asyncio
async def test_amenities_round_trip_in_the_partner_and_public_views(db_conn) -> None:
    """Amenities set on create reach both the partner's list and the catalog.

    Both views normalise the same way: the DB column is free-form, so the API
    is the one place that decides what a guest ever sees.
    """
    partner_id = await _make_partner(db_conn, "am-1@example.com")
    created = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(
            name="Дом с видом на горы",
            property_type="house",
            amenities=["wifi", "pool", "parking"],
        ),
    )
    assert created.amenities == ["wifi", "pool", "parking"]

    # The partner's own list echoes the same keys.
    mine = await service.get_partner_properties(db_conn, partner_id)
    assert [p.amenities for p in mine] == [["wifi", "pool", "parking"]]

    # And so does the guest's catalog, once published.
    await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(status="published")
    )
    public = await service.get_public_property(db_conn, created.id)
    assert public is not None
    assert public["amenities"] == ["wifi", "pool", "parking"]

    page = await service.list_public_properties(db_conn)
    assert page["items"][0]["amenities"] == ["wifi", "pool", "parking"]


@pytest.mark.asyncio
async def test_unknown_amenity_keys_are_dropped_on_write(db_conn) -> None:
    """A key the catalog does not know never reaches the guest.

    The partner's UI offers a fixed list, but the column is free-form jsonb,
    so the service is what keeps a typo from being rendered as its own label.
    """
    partner_id = await _make_partner(db_conn, "am-2@example.com")
    created = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(
            name="Квартира у моря",
            property_type="apartment",
            amenities=["wifi", "typo_key", "WI-FI"],
        ),
    )
    assert created.amenities == ["wifi"]

    # What survived the write is what is stored.
    stored = await db_conn.fetchval("SELECT amenities FROM property WHERE id = $1", created.id)
    # asyncpg decodes jsonb to a str unless a codec is registered; either way
    # the only key that survived the write is the known one.
    assert json.loads(stored) == ["wifi"]


@pytest.mark.asyncio
async def test_amenities_update_replaces_the_whole_list(db_conn) -> None:
    """PATCH amenities is a full replace, not an append.

    The UI sends the whole selection, so the order the partner sees is the
    order the guest gets, and un-selecting a key removes it.
    """
    partner_id = await _make_partner(db_conn, "am-3@example.com")
    created = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(
            name="Отель у парка",
            property_type="hotel",
            amenities=["wifi", "breakfast"],
        ),
    )

    updated = await service.update_property(
        db_conn,
        created.id,
        partner_id,
        PropertyUpdate(amenities=["breakfast", "parking"]),
    )
    assert updated is not None
    assert updated.amenities == ["breakfast", "parking"]

    # Not sending amenities leaves them alone — the field is optional.
    again = await service.update_property(
        db_conn, created.id, partner_id, PropertyUpdate(city="Yalta")
    )
    assert again is not None
    assert again.amenities == ["breakfast", "parking"]


@pytest.mark.asyncio
async def test_amenities_default_to_an_empty_list(db_conn) -> None:
    """A property created without amenities has [], never null.

    The UI maps over the array; a null would need a second branch that can
    never say anything useful.
    """
    partner_id = await _make_partner(db_conn, "am-4@example.com")
    created = await service.create_property(db_conn, partner_id, _sample())

    assert created.amenities == []
    stored = await db_conn.fetchval("SELECT amenities FROM property WHERE id = $1", created.id)
    assert json.loads(stored) == []


@pytest.mark.asyncio
async def test_amenities_survive_the_http_round_trip(committed_conn) -> None:
    """The partner route writes amenities and the guest route reads them.

    The public route answers a plain dict, so this is also the check that the
    jsonb array reaches JSON as an array, not as a quoted string.
    """
    from starlette.testclient import TestClient

    from app.main import create_app
    from app.modules.auth.jwt import create_access_token

    partner_id = await _make_partner(committed_conn, "am-http@example.com")
    created = await service.create_property(committed_conn, partner_id, _sample())

    with TestClient(create_app()) as client:
        headers = {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}
        patch = client.patch(
            f"/v1/partner/properties/{created.id}",
            json={"amenities": ["wifi", "pool", "sea_view"], "status": "published"},
            headers=headers,
        )
        assert patch.status_code == 200, patch.text
        assert patch.json()["amenities"] == ["wifi", "pool", "sea_view"]

        # The guest's view carries the same keys, in the same order.
        public = client.get(f"/v1/properties/{created.id}")
        assert public.status_code == 200, public.text
        assert public.json()["amenities"] == ["wifi", "pool", "sea_view"]


@pytest.mark.asyncio
async def test_amenity_filter_keeps_only_properties_offering_all(db_conn) -> None:
    """The catalog's amenity filter is AND-wise: a guest asking for Wi-Fi and a
    pool wants both, not a place with one of the two.
    """
    partner_id = await _make_partner(db_conn, "am-filter-1@example.com")
    with_pool = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(
            name="С бассейном",
            property_type="hotel",
            city="Koktebel",
            amenities=["wifi", "pool", "parking"],
        ),
    )
    wifi_only = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(
            name="Только вайфай",
            property_type="apartment",
            city="Koktebel",
            amenities=["wifi"],
        ),
    )
    for prop in (with_pool, wifi_only):
        await service.update_property(
            db_conn, prop.id, partner_id, PropertyUpdate(status="published")
        )

    page = await service.list_public_properties(db_conn, amenities=["wifi"])
    assert sorted(p["id"] for p in page["items"]) == sorted([with_pool.id, wifi_only.id])

    page = await service.list_public_properties(db_conn, amenities=["wifi", "pool"])
    assert [p["id"] for p in page["items"]] == [with_pool.id]

    # A key nobody offers still narrows honestly.
    page = await service.list_public_properties(db_conn, amenities=["gym"])
    assert page["total"] == 0


@pytest.mark.asyncio
async def test_amenity_filter_ignores_keys_the_catalog_dropped(db_conn) -> None:
    """A request may carry a key a newer release removed. It is dropped and the
    rest still filter — a stale shared URL must not silently empty the catalog,
    and a guest never knows which keys are live.
    """
    partner_id = await _make_partner(db_conn, "am-filter-2@example.com")
    prop = await service.create_property(
        db_conn,
        partner_id,
        PropertyCreate(
            name="Стиральная",
            property_type="house",
            city="Feodosia",
            amenities=["wifi", "washer"],
        ),
    )
    await service.update_property(db_conn, prop.id, partner_id, PropertyUpdate(status="published"))

    # Only unknown keys: nothing recognisable is being asked for, and
    # returning everything would ignore the guest entirely.
    only_unknown = await service.list_public_properties(db_conn, amenities=["retired_key"])
    assert only_unknown["total"] == 0

    # An unknown key riding along does not disable the known one.
    mixed = await service.list_public_properties(db_conn, amenities=["wifi", "retired_key"])
    assert [p["id"] for p in mixed["items"]] == [prop.id]

    # But a real key the property lacks still excludes it.
    absent = await service.list_public_properties(db_conn, amenities=["pool"])
    assert absent["total"] == 0


@pytest.mark.asyncio
async def test_inverted_date_range_is_rejected_over_http(committed_conn) -> None:
    """`checkout <= checkin` is not a stay. The catalog answers 422 rather than
    applying a filter that cannot match anything.
    """
    from starlette.testclient import TestClient

    from app.main import create_app

    assert committed_conn is not None
    with TestClient(create_app()) as client:
        inverted = client.get("/v1/properties?date_from=2026-11-22&date_to=2026-11-20")
        assert inverted.status_code == 422, inverted.text
        assert inverted.json()["detail"] == "date_to must be after date_from"

        same_day = client.get("/v1/properties?date_from=2026-11-20&date_to=2026-11-20")
        assert same_day.status_code == 422, same_day.text


@pytest.mark.asyncio
async def test_amenity_filter_reaches_guests_over_http(committed_conn) -> None:
    """The filter is wired to the route: ?amenities=wifi&amenities=pool keeps
    only the property offering both.
    """
    from starlette.testclient import TestClient

    from app.main import create_app

    partner_id = await _make_partner(committed_conn, "am-http-filter@example.com")
    both = await service.create_property(
        committed_conn,
        partner_id,
        PropertyCreate(
            name="Оба",
            property_type="hotel",
            city="Koktebel",
            amenities=["wifi", "pool"],
        ),
    )
    one = await service.create_property(
        committed_conn,
        partner_id,
        PropertyCreate(
            name="Один",
            property_type="hotel",
            city="Koktebel",
            amenities=["wifi"],
        ),
    )
    for prop in (both, one):
        await service.update_property(
            committed_conn, prop.id, partner_id, PropertyUpdate(status="published")
        )

    with TestClient(create_app()) as client:
        page = client.get("/v1/properties?amenities=wifi&amenities=pool")
        assert page.status_code == 200, page.text
        assert [p["id"] for p in page.json()["items"]] == [both.id]
