from __future__ import annotations

import datetime as dt

import pytest

from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.booking import service as booking_service
from app.modules.channel import service as channel_service
from app.modules.payment import service as payment_service
from app.modules.payment.provider import PaymentResult
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate


class _RecordingProvider:
    name = "stub"

    def __init__(self) -> None:
        self.pay_calls = 0
        self.refund_calls = 0

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        self.pay_calls += 1
        return PaymentResult("succeeded", f"charge-{booking_id}")

    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        self.refund_calls += 1
        return PaymentResult("refunded", f"refund-{booking_id}")


class _InventoryTracker:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.inventory_locks = 0
        self.inventory_updates = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetch(self, query: str, *args):
        if "FROM inventory_day" in query and "FOR UPDATE" in query:
            self.inventory_locks += 1
        return await self._conn.fetch(query, *args)

    async def executemany(self, query: str, args):
        if "UPDATE inventory_day" in query:
            self.inventory_updates += 1
        return await self._conn.executemany(query, args)


async def _seed_channel_inventory(conn, email: str) -> dict:
    partner_id = await auth_service.register_partner(
        conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    prop = await property_service.create_property(
        conn, partner_id, PropertyCreate(name="Channel property", property_type="hotel")
    )
    unit_type_id = await conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Standard', 2, 1, 3000) RETURNING id::text",
        prop.id,
    )
    checkin = dt.date.today() + dt.timedelta(days=10)
    checkout = checkin + dt.timedelta(days=2)
    await conn.execute(
        "INSERT INTO inventory_day (unit_type_id, date, available) "
        "SELECT $1, d.date, 1 FROM generate_series($2::date, ($3::date - 1), '1 day') AS d(date)",
        unit_type_id,
        checkin,
        checkout,
    )
    return {
        "partner_id": str(partner_id),
        "unit_type_id": unit_type_id,
        "checkin": checkin,
        "checkout": checkout,
    }


async def _create_channel_booking(conn, seed: dict) -> dict:
    return await channel_service.create_channel_booking(
        conn,
        partner_id=seed["partner_id"],
        unit_type_id=seed["unit_type_id"],
        checkin=seed["checkin"],
        checkout=seed["checkout"],
        guest_name="Guest",
        guest_email=f"guest-{seed['partner_id']}@example.com",
        guest_phone="+79991234567",
        client_key="fix-cancel",
    )


@pytest.mark.asyncio
async def test_repeated_refund_cancel_returns_same_result_without_second_effect(
    monkeypatch, committed_conn
) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(payment_service, "get_payment_provider", lambda: provider)
    seed = await _seed_channel_inventory(committed_conn, "fix-cancel-refund@example.com")
    booking = await _create_channel_booking(committed_conn, seed)
    tracker = _InventoryTracker(committed_conn)

    first = await channel_service.cancel_channel_booking(
        tracker, seed["partner_id"], booking["id"]
    )
    second = await channel_service.cancel_channel_booking(
        tracker, seed["partner_id"], booking["id"]
    )

    assert first == second
    assert first["status"] == "refunded"
    assert provider.pay_calls == 1
    assert provider.refund_calls == 1
    assert tracker.inventory_locks == 1
    assert tracker.inventory_updates == 1
    assert await committed_conn.fetchval(
        "SELECT sum(sold) FROM inventory_day WHERE unit_type_id = $1", seed["unit_type_id"]
    ) == 0


@pytest.mark.asyncio
async def test_repeated_hold_cancel_releases_inventory_once(committed_conn) -> None:
    seed = await _seed_channel_inventory(committed_conn, "fix-cancel-hold@example.com")
    async with committed_conn.transaction(isolation="serializable"):
        hold = await booking_service.create_hold(
            committed_conn,
            unit_type_id=seed["unit_type_id"],
            checkin=seed["checkin"],
            checkout=seed["checkout"],
            guest_name="Guest",
            guest_email="held-channel@example.com",
            guest_phone="+79991234567",
            origin="channel",
        )
    tracker = _InventoryTracker(committed_conn)

    first = await channel_service.cancel_channel_booking(
        tracker, seed["partner_id"], hold["id"]
    )
    second = await channel_service.cancel_channel_booking(
        tracker, seed["partner_id"], hold["id"]
    )

    assert first == second == {"id": hold["id"], "status": "cancelled"}
    assert tracker.inventory_updates == 1
