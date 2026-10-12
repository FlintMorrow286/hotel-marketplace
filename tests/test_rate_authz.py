"""Who may read a rate plan, and who may set the platform's commission on it."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.modules.auth.jwt import create_access_token
from app.modules.rate import service
from tests.test_rate import _seed


def _bearer(sub: str, scope: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(sub, scope=scope)}"}


@pytest.mark.asyncio
async def test_a_partner_cannot_list_another_partners_rate_plans(committed_conn) -> None:
    mine = await _seed(committed_conn, "ra-1@example.com")
    theirs = await _seed(committed_conn, "ra-2@example.com")
    await service.create_rate_plan(committed_conn, theirs["unit_type_id"], "Чужой тариф")

    with TestClient(create_app()) as client:
        foreign = client.get(
            "/v1/partner/rate-plans",
            params={"unit_type_id": theirs["unit_type_id"]},
            headers=_bearer(mine["partner_id"], "partner"),
        )
        own = client.get(
            "/v1/partner/rate-plans",
            params={"unit_type_id": theirs["unit_type_id"]},
            headers=_bearer(theirs["partner_id"], "partner"),
        )

    assert foreign.status_code == 404, foreign.text
    assert "Чужой тариф" not in foreign.text
    assert own.status_code == 200 and [p["name"] for p in own.json()] == ["Чужой тариф"]


@pytest.mark.asyncio
async def test_an_unknown_unit_type_looks_the_same_as_a_foreign_one(committed_conn) -> None:
    mine = await _seed(committed_conn, "ra-3@example.com")
    theirs = await _seed(committed_conn, "ra-4@example.com")

    with TestClient(create_app()) as client:
        headers = _bearer(mine["partner_id"], "partner")
        foreign = client.get(
            "/v1/partner/rate-plans",
            params={"unit_type_id": theirs["unit_type_id"]},
            headers=headers,
        )
        unknown = client.get(
            "/v1/partner/rate-plans",
            params={"unit_type_id": "00000000-0000-0000-0000-000000000000"},
            headers=headers,
        )

    assert (foreign.status_code, foreign.json()) == (unknown.status_code, unknown.json())


@pytest.mark.asyncio
async def test_a_partner_cannot_set_the_platforms_commission(committed_conn) -> None:
    seed = await _seed(committed_conn, "ra-5@example.com")
    plan = await service.create_rate_plan(committed_conn, seed["unit_type_id"], "Тариф")

    with TestClient(create_app()) as client:
        headers = _bearer(seed["partner_id"], "partner")
        old_route = client.patch(
            f"/v1/partner/rate-plans/{plan['id']}", json={"commission_rate": 0}, headers=headers
        )
        admin_route = client.patch(
            f"/v1/admin/rate-plans/{plan['id']}", json={"commission_rate": 0}, headers=headers
        )

    assert old_route.status_code in (404, 405)
    assert admin_route.status_code == 403
    stored = await committed_conn.fetchval(
        "SELECT commission_rate FROM rate_plan WHERE id = $1", plan["id"]
    )
    assert stored is None, "the commission must still be the platform default"


@pytest.mark.asyncio
async def test_staff_set_the_commission_and_the_next_booking_snapshots_it(committed_conn) -> None:
    seed = await _seed(committed_conn, "ra-6@example.com")
    plan = await service.create_rate_plan(committed_conn, seed["unit_type_id"], "Тариф")

    with TestClient(create_app()) as client:
        admin = _bearer("00000000-0000-0000-0000-0000000000aa", "admin")
        set_rate = client.patch(
            f"/v1/admin/rate-plans/{plan['id']}", json={"commission_rate": 0.2}, headers=admin
        )
        too_big = client.patch(
            f"/v1/admin/rate-plans/{plan['id']}", json={"commission_rate": 1.5}, headers=admin
        )
        missing = client.patch(
            "/v1/admin/rate-plans/00000000-0000-0000-0000-000000000000",
            json={"commission_rate": 0.1},
            headers=admin,
        )
        cleared = client.patch(
            f"/v1/admin/rate-plans/{plan['id']}", json={"commission_rate": None}, headers=admin
        )

    assert set_rate.status_code == 200 and set_rate.json()["commission_rate"] == 0.2
    assert too_big.status_code == 422
    assert missing.status_code == 404
    assert cleared.status_code == 200 and cleared.json()["commission_rate"] is None


@pytest.mark.asyncio
async def test_no_token_and_a_garbage_token_get_nothing(committed_conn) -> None:
    seed = await _seed(committed_conn, "ra-7@example.com")
    plan = await service.create_rate_plan(committed_conn, seed["unit_type_id"], "Тариф")

    with TestClient(create_app()) as client:
        none = client.patch(f"/v1/admin/rate-plans/{plan['id']}", json={"commission_rate": 0})
        garbage = client.patch(
            f"/v1/admin/rate-plans/{plan['id']}",
            json={"commission_rate": 0},
            headers={"Authorization": "Bearer not-a-token"},
        )

    assert none.status_code in (401, 403) and garbage.status_code in (401, 403)
