"""Payouts over HTTP: who may do what, what a partner may see, and the file the books get."""

from __future__ import annotations

import csv
import io
import uuid

import pytest
from starlette.testclient import TestClient

from app.config import legal
from app.main import create_app
from app.modules.auth.jwt import create_access_token
from app.modules.payout import service
from tests._payouts import (
    ADMIN,
    booking,
    partner_with_property,
    refund_free,
    refund_late,
)

ADMIN_HEADERS = {"Authorization": f"Bearer {create_access_token('admin-under-test', 'admin')}"}


def _as_partner(partner_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(partner_id, 'partner')}"}


def _rows(response) -> list[list[str]]:
    assert response.content.startswith(b"\xef\xbb\xbf"), "a BOM, so Excel reads the UTF-8"
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))


# ---------------------------------------------------------------- who may do what


@pytest.mark.asyncio
async def test_only_staff_may_use_the_operators_side(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-scope@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    calls = [
        ("GET", "/v1/admin/payouts/balances", None),
        ("POST", "/v1/admin/payouts/generate", {}),
        ("GET", "/v1/admin/payouts", None),
        ("GET", f"/v1/admin/payouts/{draft_id}", None),
        ("GET", f"/v1/admin/payouts/{draft_id}/export.csv", None),
        ("POST", f"/v1/admin/payouts/{draft_id}/approve", None),
        ("POST", f"/v1/admin/payouts/{draft_id}/paid", {"reference": "PP-1"}),
        ("POST", f"/v1/admin/payouts/{draft_id}/cancel", None),
    ]

    with TestClient(create_app()) as client:
        for method, path, body in calls:
            anonymous = client.request(method, path, json=body)
            as_partner = client.request(
                method, path, json=body, headers=_as_partner(p["partner_id"])
            )
            assert anonymous.status_code in (401, 403), (method, path)
            assert as_partner.status_code == 403, (method, path)

    assert (
        await committed_conn.fetchval("SELECT status FROM payout WHERE id = $1", draft_id)
    ) == "draft"


@pytest.mark.asyncio
async def test_only_a_partner_may_use_the_partners_side(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-scope2@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)
    paths = [
        "/v1/partner/payouts/summary",
        "/v1/partner/payouts/pending",
        "/v1/partner/payouts",
        f"/v1/partner/payouts/{draft_id}",
        f"/v1/partner/payouts/{draft_id}/export.csv",
    ]

    with TestClient(create_app()) as client:
        for path in paths:
            assert client.get(path).status_code in (401, 403), path
            assert client.get(path, headers=ADMIN_HEADERS).status_code == 403, path


# ---------------------------------------------------------------- the operator's flow


@pytest.mark.asyncio
async def test_the_operator_builds_approves_and_pays_a_payout(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-flow@example.com", name="Мария")
    await booking(committed_conn, p, total="10000.00")

    with TestClient(create_app()) as client:
        balances = client.get("/v1/admin/payouts/balances", headers=ADMIN_HEADERS).json()
        built = client.post(
            "/v1/admin/payouts/generate",
            json={"partner_id": p["partner_id"]},
            headers=ADMIN_HEADERS,
        )
        (draft,) = built.json()["payouts"]
        detail = client.get(f"/v1/admin/payouts/{draft['id']}", headers=ADMIN_HEADERS).json()
        approved = client.post(f"/v1/admin/payouts/{draft['id']}/approve", headers=ADMIN_HEADERS)
        paid = client.post(
            f"/v1/admin/payouts/{draft['id']}/paid",
            json={"reference": "  ПП   № 4711  "},
            headers=ADMIN_HEADERS,
        )
        listed = client.get("/v1/admin/payouts?status=paid", headers=ADMIN_HEADERS).json()
        nothing_draft = client.get("/v1/admin/payouts?status=draft", headers=ADMIN_HEADERS).json()
        after = client.get("/v1/admin/payouts/balances", headers=ADMIN_HEADERS).json()

    assert balances["rule"] == {
        "eligible_from": "checkout",
        "delay_days": 0,
        "min_amount": 0.01,
        "money_flow": "agent_collect",
    }
    assert [(b["partner_email"], b["available"], b["paid"]) for b in balances["balances"]] == [
        ("poh-flow@example.com", 8800.0, 0.0)
    ]
    assert built.status_code == 200 and "lines" not in draft
    assert (draft["status"], draft["net_amount"], draft["line_count"], draft["number"] >= 1) == (
        "draft",
        8800.0,
        1,
        True,
    )
    assert (draft["partner_name"], draft["currency"]) == ("Мария", "RUB")
    assert [
        (ln["kind"], ln["gross_amount"], ln["commission_amount"], ln["net_amount"])
        for ln in detail["lines"]
    ] == [("booking", 10000.0, 1200.0, 8800.0)]
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "admin-under-test" and "lines" not in approved.json()
    assert paid.status_code == 200
    assert (paid.json()["status"], paid.json()["reference"]) == ("paid", "ПП № 4711")
    assert [x["id"] for x in listed] == [draft["id"]] and nothing_draft == []
    assert [(b["available"], b["approved"], b["paid"]) for b in after["balances"]] == [
        (0.0, 0.0, 8800.0)
    ]


@pytest.mark.asyncio
async def test_generating_for_everyone_and_the_refusals_the_operator_sees(committed_conn) -> None:
    a = await partner_with_property(committed_conn, "poh-all-a@example.com")
    b = await partner_with_property(committed_conn, "poh-all-b@example.com")
    await booking(committed_conn, a, total="1000.00")
    await booking(committed_conn, b, total="2000.00")
    missing = str(uuid.uuid4())

    with TestClient(create_app()) as client:
        built = client.post("/v1/admin/payouts/generate", json={}, headers=ADMIN_HEADERS)
        draft_id = built.json()["payouts"][0]["id"]
        replies = {
            "paid before approval": client.post(
                f"/v1/admin/payouts/{draft_id}/paid",
                json={"reference": "PP-1"},
                headers=ADMIN_HEADERS,
            ),
            "blank reference": client.post(
                f"/v1/admin/payouts/{draft_id}/paid",
                json={"reference": "   "},
                headers=ADMIN_HEADERS,
            ),
            "no reference": client.post(
                f"/v1/admin/payouts/{draft_id}/paid", json={}, headers=ADMIN_HEADERS
            ),
            "reference too long": client.post(
                f"/v1/admin/payouts/{draft_id}/paid",
                json={"reference": "x" * 201},
                headers=ADMIN_HEADERS,
            ),
            "unknown payout": client.post(
                f"/v1/admin/payouts/{missing}/approve", headers=ADMIN_HEADERS
            ),
            "not a uuid": client.get("/v1/admin/payouts/not-a-uuid", headers=ADMIN_HEADERS),
            "unknown status": client.get("/v1/admin/payouts?status=bogus", headers=ADMIN_HEADERS),
            "export of nothing": client.get(
                f"/v1/admin/payouts/{missing}/export.csv", headers=ADMIN_HEADERS
            ),
        }
        client.post(f"/v1/admin/payouts/{draft_id}/approve", headers=ADMIN_HEADERS)
        twice = client.post(f"/v1/admin/payouts/{draft_id}/approve", headers=ADMIN_HEADERS)
        cancelled = client.post(f"/v1/admin/payouts/{draft_id}/cancel", headers=ADMIN_HEADERS)
        cancelled_twice = client.post(f"/v1/admin/payouts/{draft_id}/cancel", headers=ADMIN_HEADERS)

    assert len(built.json()["payouts"]) == 2
    assert {k: r.status_code for k, r in replies.items()} == {
        "paid before approval": 409,
        "blank reference": 422,
        "no reference": 422,
        "reference too long": 422,
        "unknown payout": 404,
        "not a uuid": 404,
        "unknown status": 422,
        "export of nothing": 404,
    }
    assert "only an approved payout" in replies["paid before approval"].json()["detail"]
    assert twice.status_code == 409 and "only a draft can be approved" in twice.json()["detail"]
    assert cancelled.json() == {"id": draft_id, "outcome": "cancelled"}
    assert cancelled_twice.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("partner_id", ["", "not-a-uuid", 7])
async def test_a_partner_id_that_is_not_one_never_means_everyone(
    committed_conn, partner_id
) -> None:
    """An empty id used to read as "no id" and rebuilt every partner's drafts."""
    p = await partner_with_property(committed_conn, f"poh-badid-{uuid.uuid4().hex[:6]}@example.com")
    await booking(committed_conn, p)

    with TestClient(create_app()) as client:
        reply = client.post(
            "/v1/admin/payouts/generate", json={"partner_id": partner_id}, headers=ADMIN_HEADERS
        )

    assert reply.status_code == 422
    assert await committed_conn.fetchval("SELECT count(*) FROM payout") == 0


@pytest.mark.asyncio
async def test_a_cancel_that_names_the_wrong_state_changes_nothing(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-expect@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)
    path = f"/v1/admin/payouts/{draft_id}/cancel"

    with TestClient(create_app()) as client:
        stale = client.post(path, json={"expect": "draft"}, headers=ADMIN_HEADERS)
        nonsense = client.post(path, json={"expect": "paid"}, headers=ADMIN_HEADERS)
        right = client.post(path, json={"expect": "approved"}, headers=ADMIN_HEADERS)

    assert stale.status_code == 409 and "is approved, not draft" in stale.json()["detail"]
    assert nonsense.status_code == 422
    assert right.json() == {"id": draft_id, "outcome": "cancelled"}


@pytest.mark.asyncio
async def test_a_stale_draft_is_a_409_the_operator_can_act_on(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-stale@example.com")
    b = await booking(committed_conn, p)

    with TestClient(create_app()) as client:
        (draft,) = client.post(
            "/v1/admin/payouts/generate",
            json={"partner_id": p["partner_id"]},
            headers=ADMIN_HEADERS,
        ).json()["payouts"]
        await refund_free(committed_conn, b)
        stale = client.post(f"/v1/admin/payouts/{draft['id']}/approve", headers=ADMIN_HEADERS)
        rebuilt = client.post(
            "/v1/admin/payouts/generate",
            json={"partner_id": p["partner_id"]},
            headers=ADMIN_HEADERS,
        )

    assert stale.status_code == 409 and "out of date" in stale.json()["detail"]
    assert rebuilt.json() == {"payouts": []}, "nothing is owed any more, the draft is gone"


@pytest.mark.asyncio
async def test_the_operators_screens_work_while_the_rule_cannot_but_actions_say_why(
    committed_conn, monkeypatch
) -> None:
    p = await partner_with_property(committed_conn, "poh-flow-off@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    monkeypatch.setattr(legal, "MONEY_FLOW", "direct_to_partner")

    with TestClient(create_app()) as client:
        listed = client.get("/v1/admin/payouts", headers=ADMIN_HEADERS)
        balances = client.get("/v1/admin/payouts/balances", headers=ADMIN_HEADERS)
        generate = client.post("/v1/admin/payouts/generate", json={}, headers=ADMIN_HEADERS)
        approve = client.post(f"/v1/admin/payouts/{draft_id}/approve", headers=ADMIN_HEADERS)
        summary = client.get("/v1/partner/payouts/summary", headers=_as_partner(p["partner_id"]))

    assert listed.status_code == 200 and len(listed.json()) == 1
    for reply in (balances, generate, approve, summary):
        assert reply.status_code == 503 and "direct_to_partner" in reply.json()["detail"]


# ---------------------------------------------------------------- the partner's own statement


@pytest.mark.asyncio
async def test_a_partner_sees_their_own_documents_and_never_a_draft(committed_conn) -> None:
    a = await partner_with_property(committed_conn, "poh-mine@example.com")
    b = await partner_with_property(committed_conn, "poh-theirs@example.com")
    await booking(committed_conn, a, total="10000.00")
    await booking(committed_conn, b, total="4000.00", rate="0.10")
    (draft_a,) = await service.generate(committed_conn, a["partner_id"])
    (draft_b,) = await service.generate(committed_conn, b["partner_id"])
    mine, theirs = _as_partner(a["partner_id"]), _as_partner(b["partner_id"])

    with TestClient(create_app()) as client:
        in_draft = {
            "list": client.get("/v1/partner/payouts", headers=mine).json(),
            "detail": client.get(f"/v1/partner/payouts/{draft_a}", headers=mine).status_code,
            "export": client.get(
                f"/v1/partner/payouts/{draft_a}/export.csv", headers=mine
            ).status_code,
            "summary": client.get("/v1/partner/payouts/summary", headers=mine).json(),
        }
        await service.approve(committed_conn, draft_a, ADMIN)
        await service.approve(committed_conn, draft_b, ADMIN)
        listed = client.get("/v1/partner/payouts", headers=mine).json()
        detail = client.get(f"/v1/partner/payouts/{draft_a}", headers=mine)
        their_detail = client.get(f"/v1/partner/payouts/{draft_b}", headers=mine)
        their_export = client.get(f"/v1/partner/payouts/{draft_b}/export.csv", headers=mine)
        their_own = client.get(f"/v1/partner/payouts/{draft_b}", headers=theirs)
        summary = client.get("/v1/partner/payouts/summary", headers=mine).json()

    assert in_draft["list"] == [] and in_draft["detail"] == 404 and in_draft["export"] == 404
    assert in_draft["summary"]["balances"][0]["available"] == 8800.0, (
        "a draft is not yet money on its way"
    )
    assert "draft" not in in_draft["summary"]["balances"][0]
    assert [x["id"] for x in listed] == [draft_a] and listed[0]["status"] == "approved"
    assert "approved_by" not in listed[0] and "approved_by" not in detail.json(), (
        "operators' ids stay inside"
    )
    assert detail.status_code == 200 and detail.json()["lines"][0]["net_amount"] == 8800.0
    assert (their_detail.status_code, their_export.status_code) == (404, 404)
    assert their_own.status_code == 200 and their_own.json()["net_amount"] == 3600.0
    assert [(x["approved"], x["available"]) for x in summary["balances"]] == [(8800.0, 0.0)]
    assert summary["rule"]["eligible_from"] == "checkout"


@pytest.mark.asyncio
async def test_a_cancelled_payout_is_shown_to_the_partner_who_had_seen_it(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-cancelled@example.com")
    await booking(committed_conn, p)
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)
    await service.cancel(committed_conn, draft_id, ADMIN)
    await service.generate(committed_conn, p["partner_id"])  # a fresh draft: never shown

    with TestClient(create_app()) as client:
        listed = client.get("/v1/partner/payouts", headers=_as_partner(p["partner_id"])).json()

    assert [(x["id"], x["status"]) for x in listed] == [(draft_id, "cancelled")]


@pytest.mark.asyncio
async def test_the_partner_sees_which_bookings_stand_behind_the_balance(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-pending@example.com")
    await booking(committed_conn, p, checkout_in=-1)
    await booking(committed_conn, p, checkout_in=5, total="5000.00")
    other = await partner_with_property(committed_conn, "poh-pending-other@example.com")
    await booking(committed_conn, other, total="7000.00")

    with TestClient(create_app()) as client:
        pending = client.get(
            "/v1/partner/payouts/pending", headers=_as_partner(p["partner_id"])
        ).json()
        capped = client.get(
            "/v1/partner/payouts/pending?limit=1", headers=_as_partner(p["partner_id"])
        ).json()
        too_many = client.get(
            "/v1/partner/payouts/pending?limit=2001", headers=_as_partner(p["partner_id"])
        )

    assert pending["total"] == 2
    assert [(x["state"], x["net_amount"]) for x in pending["items"]] == [
        ("available", 8800.0),
        ("upcoming", 4400.0),
    ]
    assert (len(capped["items"]), capped["total"]) == (1, 2), "a page says how long the list is"
    assert too_many.status_code == 422


# ---------------------------------------------------------------- the file


@pytest.mark.asyncio
async def test_the_export_is_a_file_a_russian_spreadsheet_opens(committed_conn) -> None:
    p = await partner_with_property(committed_conn, "poh-csv@example.com", name="Мария")
    first = await booking(committed_conn, p, total="10000.00")
    (one,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, one, ADMIN)
    await service.mark_paid(committed_conn, one, "ПП 1", ADMIN)
    await refund_late(committed_conn, first, "5000.00")  # 5 000 less 600: 4 400 comes back
    await booking(committed_conn, p, total="9999.99", commission="1199.99")  # 8 800.00
    (two,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, two, ADMIN)
    await service.mark_paid(committed_conn, two, "ПП 2", ADMIN)

    with TestClient(create_app()) as client:
        mine = client.get(
            f"/v1/partner/payouts/{two}/export.csv", headers=_as_partner(p["partner_id"])
        )
        theirs = client.get(f"/v1/admin/payouts/{two}/export.csv", headers=ADMIN_HEADERS)
        number = client.get(f"/v1/admin/payouts/{two}", headers=ADMIN_HEADERS).json()["number"]

    assert mine.status_code == 200 and mine.content == theirs.content
    assert mine.headers["content-type"] == "text/csv; charset=utf-8"
    assert mine.headers["content-disposition"] == f'attachment; filename="payout-{number}.csv"'
    assert mine.headers["cache-control"] == "no-store"
    rows = _rows(mine)
    assert rows[0] == ["Выплата", f"№ {number}"]
    assert rows[1] == ["Партнёр", "Мария <poh-csv@example.com>"]
    assert rows[2] == ["Статус", "Выплачена"] and rows[3] == ["Валюта", "RUB"]
    assert rows[4][0] == "Выплачена" and len(rows[4][1]) == 10 and rows[4][1][2] == "."
    assert rows[5] == ["Платёжное поручение", "ПП 2"] and rows[6] == []
    assert rows[7] == [
        "Код брони",
        "Объект",
        "Заезд",
        "Выезд",
        "Строка",
        "Сумма",
        "Комиссия",
        "К выплате",
    ]
    lines = sorted((r[4], r[5], r[6], r[7]) for r in rows[8:-1])
    assert lines == [
        ("Бронь", "9999,99", "1199,99", "8800,00"),
        ("Корректировка", "-5000,00", "-600,00", "-4400,00"),
    ], "kopecks with a comma, negatives as they are"
    assert rows[-1] == ["Итого", "", "", "", "", "4999,99", "599,99", "4400,00"]


@pytest.mark.asyncio
async def test_what_users_type_cannot_run_as_a_formula_in_the_spreadsheet(committed_conn) -> None:
    p = await partner_with_property(
        committed_conn, "poh-formula@example.com", name='=HYPERLINK("http://evil.example","x")'
    )
    await booking(committed_conn, p)
    await committed_conn.execute(
        "UPDATE property SET name = '-2+3+cmd|calc' WHERE id = $1", p["property_id"]
    )
    (draft_id,) = await service.generate(committed_conn, p["partner_id"])
    await service.approve(committed_conn, draft_id, ADMIN)
    await service.mark_paid(committed_conn, draft_id, "@SUM(1+1)", ADMIN)

    with TestClient(create_app()) as client:
        reply = client.get(f"/v1/admin/payouts/{draft_id}/export.csv", headers=ADMIN_HEADERS)

    cells = [c for row in _rows(reply) for c in row]
    assert [c for c in cells if c and c[0] in "=+-@\t\r"] == [], (
        "nothing here is negative: no cell may start so"
    )
    assert any(c.startswith("'=HYPERLINK") for c in cells)
    assert "'@SUM(1+1)" in cells and "'-2+3+cmd|calc" in cells
    assert "8800,00" in cells, "amounts are never touched"
