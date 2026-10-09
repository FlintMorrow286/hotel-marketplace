"""Concurrent booking operations: conflicts are retried, money moves once.

The HTTP tests fire requests from several threads at once through the real ASGI
app and its connection pool, so every request holds its own Postgres session —
the same shape as a double click or a burst of guests in production.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.db import tx
from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.payment import service as payment_service
from app.modules.payment.provider import StubProvider
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate

TODAY = dt.date.today()
CHECKIN = TODAY + dt.timedelta(days=10)
CHECKOUT = CHECKIN + dt.timedelta(days=2)


async def _seed_unit(conn: asyncpg.Connection, email: str, total_units: int = 1) -> str:
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await property_service.create_property(
        conn,
        partner_id,
        PropertyCreate(
            name="Test", property_type="apartment", city="Yalta", timezone="Europe/Simferopol"
        ),
    )
    row = await conn.fetchrow(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Квартира', 2, $2, 3000) RETURNING id::text",
        prop.id,
        total_units,
    )
    assert row is not None
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, $2 FROM generate_series($3::date, ($3::date + 29)::date, '1 day') AS d(date)",
        row["id"],
        total_units,
        TODAY,
    )
    return row["id"]


def _hold_body(unit_type_id: str, i: int = 0) -> dict:
    return {
        "unit_type_id": unit_type_id,
        "checkin": CHECKIN.isoformat(),
        "checkout": CHECKOUT.isoformat(),
        "guest": {"name": f"Гость {i}", "email": f"guest{i}@example.com", "phone": "+79991234567"},
    }


def _race(n: int, fn):
    """Run fn(i) in n threads released together; returns the results in order."""
    barrier = threading.Barrier(n)

    def run(i: int):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


class _SlowProvider(StubProvider):
    """A provider slow enough that concurrent requests overlap, counting its calls."""

    def __init__(self) -> None:
        self.pays = 0
        self.refunds = 0

    async def pay(self, booking_id: str, amount: float):
        self.pays += 1
        await asyncio.sleep(0.3)
        return await super().pay(booking_id, amount)

    async def refund(self, booking_id: str, amount: float):
        self.refunds += 1
        await asyncio.sleep(0.3)
        return await super().refund(booking_id, amount)


# ---------------------------------------------------------------- holds


@pytest.mark.asyncio
async def test_parallel_holds_for_one_room_are_never_500(committed_conn) -> None:
    """One room, many guests at once: one booking, the rest a clean 409."""
    ut = await _seed_unit(committed_conn, "conc-hold@example.com")

    with TestClient(create_app()) as client:
        responses = _race(12, lambda i: client.post("/v1/bookings/hold", json=_hold_body(ut, i)))

    assert sorted(r.status_code for r in responses) == [201] + [409] * 11
    assert await committed_conn.fetchval("SELECT count(*) FROM booking WHERE status = 'hold'") == 1
    held = await committed_conn.fetchval(
        "SELECT hold FROM inventory_day WHERE unit_type_id = $1 AND date = $2", ut, CHECKIN
    )
    assert held == 1


@pytest.mark.asyncio
async def test_double_click_with_one_idempotency_key_books_once(committed_conn) -> None:
    """The cabinet sends one key per stay: a double click replays the first booking."""
    ut = await _seed_unit(committed_conn, "conc-idem@example.com")
    headers = {"Idempotency-Key": "double-click"}

    with TestClient(create_app()) as client:
        responses = _race(
            6, lambda i: client.post("/v1/bookings/hold", json=_hold_body(ut), headers=headers)
        )

    assert [r.status_code for r in responses] == [201] * 6
    assert len({r.json()["id"] for r in responses}) == 1
    assert await committed_conn.fetchval("SELECT count(*) FROM booking") == 1


@pytest.mark.asyncio
async def test_replayed_hold_key_after_payment_is_not_a_500(committed_conn) -> None:
    """The same key after the booking was paid names a confirmed booking: no hold timer."""
    ut = await _seed_unit(committed_conn, "conc-replay@example.com")
    headers = {"Idempotency-Key": "replay-after-pay"}

    with TestClient(create_app()) as client:
        first = client.post("/v1/bookings/hold", json=_hold_body(ut), headers=headers)
        paid = client.post(f"/v1/bookings/{first.json()['id']}/pay")
        replay = client.post("/v1/bookings/hold", json=_hold_body(ut), headers=headers)

    assert (first.status_code, paid.status_code, replay.status_code) == (201, 200, 201)
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["status"] == "confirmed"
    assert replay.json()["hold_expires_at"] is None


# ---------------------------------------------------------------- money


@pytest.mark.asyncio
async def test_parallel_payments_charge_the_provider_once(committed_conn, monkeypatch) -> None:
    provider = _SlowProvider()
    monkeypatch.setattr(payment_service, "get_payment_provider", lambda: provider)
    ut = await _seed_unit(committed_conn, "conc-pay@example.com")

    with TestClient(create_app()) as client:
        booking_id = client.post("/v1/bookings/hold", json=_hold_body(ut)).json()["id"]
        responses = _race(5, lambda i: client.post(f"/v1/bookings/{booking_id}/pay"))

    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]
    assert provider.pays == 1
    assert (
        await committed_conn.fetchval(
            "SELECT count(*) FROM payment WHERE booking_id = $1 AND status = 'succeeded'",
            booking_id,
        )
        == 1
    )
    day = await committed_conn.fetchrow(
        "SELECT hold, sold FROM inventory_day WHERE unit_type_id = $1 AND date = $2", ut, CHECKIN
    )
    assert (day["hold"], day["sold"]) == (0, 1)


@pytest.mark.asyncio
async def test_parallel_refunds_refund_the_provider_once(committed_conn, monkeypatch) -> None:
    provider = _SlowProvider()
    monkeypatch.setattr(payment_service, "get_payment_provider", lambda: provider)
    ut = await _seed_unit(committed_conn, "conc-refund@example.com")

    with TestClient(create_app()) as client:
        booking_id = client.post("/v1/bookings/hold", json=_hold_body(ut)).json()["id"]
        assert client.post(f"/v1/bookings/{booking_id}/pay").status_code == 200
        responses = _race(5, lambda i: client.post(f"/v1/bookings/{booking_id}/refund"))

    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]
    assert provider.refunds == 1
    assert (
        await committed_conn.fetchval(
            "SELECT count(*) FROM payment WHERE booking_id = $1 AND status = 'refunded'",
            booking_id,
        )
        == 1
    )
    assert (
        await committed_conn.fetchval("SELECT status FROM booking WHERE id = $1", booking_id)
        == "refunded"
    )
    sold = await committed_conn.fetchval(
        "SELECT sold FROM inventory_day WHERE unit_type_id = $1 AND date = $2", ut, CHECKIN
    )
    assert sold == 0


# ---------------------------------------------------------------- helpers


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.isolation: str | None = None

    def transaction(self, *, isolation: str) -> _FakeTransaction:
        self.isolation = isolation
        return _FakeTransaction()


@pytest.mark.asyncio
async def test_serializable_runs_again_after_a_conflict() -> None:
    calls = 0

    async def body() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise asyncpg.SerializationError("could not serialize access")
        return "done"

    conn = _FakeConn()
    assert await tx.serializable(conn, body) == "done"
    assert calls == 3
    assert conn.isolation == "serializable"


@pytest.mark.asyncio
async def test_serializable_gives_up_after_the_last_attempt() -> None:
    calls = 0

    async def body() -> None:
        nonlocal calls
        calls += 1
        raise asyncpg.DeadlockDetectedError("deadlock detected")

    with pytest.raises(asyncpg.DeadlockDetectedError):
        await tx.serializable(_FakeConn(), body, attempts=3)
    assert calls == 3


@pytest.mark.asyncio
async def test_serializable_does_not_retry_other_errors() -> None:
    calls = 0

    async def body() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("no free inventory")

    with pytest.raises(ValueError):
        await tx.serializable(_FakeConn(), body)
    assert calls == 1


def test_a_conflict_that_outlasts_the_retries_answers_503(monkeypatch) -> None:
    warnings: list[tuple[str, dict]] = []
    # The app logger caches itself on first use, so a capture hook would miss it by now.
    monkeypatch.setattr(
        tx, "log", SimpleNamespace(warning=lambda event, **kw: warnings.append((event, kw)))
    )
    app = FastAPI()
    tx.install_conflict_handler(app)

    @app.get("/boom/{booking_id}")
    async def boom(booking_id: str) -> None:
        raise asyncpg.SerializationError("could not serialize access")

    response = TestClient(app).get("/boom/secret-booking-id")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    # The 503 replaces a 500 that used to log a traceback: it must not become silent,
    # and the log carries the route template, never the id in the path.
    assert [event for event, _ in warnings] == ["conflict-answered-503"]
    assert warnings[0][1]["route"] == "/boom/{booking_id}"
    assert "secret-booking-id" not in repr(warnings)
