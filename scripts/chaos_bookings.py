"""Seeded concurrency and provider-fault test rig for booking safety.

Examples:
    DATABASE_URL=postgresql://.../chaos_app python scripts/chaos_bookings.py inproc --ops 300 --guests 32 --rounds 30
    python scripts/chaos_bookings.py http --dsn postgresql://.../chaos_app --base-url http://127.0.0.1:8300 --ops 20000 --guests 100
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import random
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.payment.provider import PaymentProvider, PaymentResult  # noqa: E402
from scripts.check_invariants import CheckReport, check_dsn  # noqa: E402


@dataclass
class SeedData:
    tag: str
    partner_id: str
    property_id: str
    unit_type_ids: list[str]
    guest_emails: list[str]
    inventory_start: date
    stay_start: date


@dataclass
class RunStats:
    status_codes: Counter[str] = field(default_factory=Counter)
    operations_by_outcome: Counter[str] = field(default_factory=Counter)
    request_outcomes: Counter[str] = field(default_factory=Counter)
    operations: int = 0
    background: Counter[str] = field(default_factory=Counter)
    trace: list[dict[str, Any]] = field(default_factory=list)
    background_trace: list[dict[str, Any]] = field(default_factory=list)
    request_errors: list[dict[str, Any]] = field(default_factory=list)
    retry_results: dict[str, list[int | None]] = field(default_factory=dict)
    sequence: int = 0

    def next_order(self) -> int:
        self.sequence += 1
        return self.sequence


@dataclass
class ProviderPlan:
    pay_mode: str
    refund_mode: str
    delay: float


class ChaosProvider(PaymentProvider):
    """Booking-idempotent test provider with independently tracked money truth."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.name_value = "stub"
        self.plans: dict[str, ProviderPlan] = {}
        self.pay_state: dict[str, str] = {}
        self.refund_state: dict[str, str] = {}
        self.charged: dict[str, Decimal] = {}
        self.refunded: dict[str, Decimal] = {}
        self.unknown_after_charge: set[str] = set()
        self.unknown_after_refund: set[str] = set()
        self.call_counts: dict[str, Counter[str]] = defaultdict(Counter)
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return self.name_value

    def register_booking(self, booking_id: str, stable_key: str) -> None:
        if booking_id in self.plans:
            return
        token = hashlib.sha256(f"{self.seed}:{stable_key}".encode()).digest()
        rng = random.Random(int.from_bytes(token[:8], "big"))
        self.plans[booking_id] = ProviderPlan(
            pay_mode=rng.choices(
                ("success", "failed", "raise_before", "raise_after"),
                weights=(58, 12, 12, 18),
                k=1,
            )[0],
            refund_mode=rng.choices(
                ("success", "failed", "raise_before", "raise_after"),
                weights=(55, 15, 12, 18),
                k=1,
            )[0],
            delay=rng.uniform(0.001, 0.02) if rng.random() < 0.35 else 0.0,
        )

    def _plan(self, booking_id: str) -> ProviderPlan:
        if booking_id not in self.plans:
            self.register_booking(booking_id, f"fallback:{booking_id}")
        return self.plans[booking_id]

    def _lock(self, booking_id: str) -> asyncio.Lock:
        return self._locks.setdefault(booking_id, asyncio.Lock())

    @staticmethod
    def _money(amount: float) -> Decimal:
        return Decimal(str(amount)).quantize(Decimal("0.01"))

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        async with self._lock(booking_id):
            self.call_counts[booking_id]["pay"] += 1
            previous = self.pay_state.get(booking_id)
            if previous is not None:
                if previous == "raise_before":
                    raise RuntimeError("chaos provider pay timed out before charge")
                if previous == "failed":
                    return PaymentResult("failed", f"chaos-failed-{booking_id[:8]}")
                return PaymentResult("succeeded", f"chaos-pay-{booking_id[:8]}")

            plan = self._plan(booking_id)
            if plan.delay:
                await asyncio.sleep(plan.delay)
            if plan.pay_mode == "raise_before":
                self.pay_state[booking_id] = "raise_before"
                raise RuntimeError("chaos provider pay timed out before charge")
            if plan.pay_mode == "failed":
                self.pay_state[booking_id] = "failed"
                return PaymentResult("failed", f"chaos-failed-{booking_id[:8]}")

            self.charged[booking_id] = self._money(amount)
            if plan.pay_mode == "raise_after":
                self.pay_state[booking_id] = "raise_after"
                self.unknown_after_charge.add(booking_id)
                raise RuntimeError("chaos provider pay timed out after charge")

            self.pay_state[booking_id] = "succeeded"
            return PaymentResult("succeeded", f"chaos-pay-{booking_id[:8]}")

    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        async with self._lock(booking_id):
            self.call_counts[booking_id]["refund"] += 1
            previous = self.refund_state.get(booking_id)
            if previous is not None:
                if previous == "raise_before":
                    raise RuntimeError("chaos provider refund timed out before return")
                if previous == "failed":
                    return PaymentResult("failed", f"chaos-refund-failed-{booking_id[:8]}")
                return PaymentResult("refunded", f"chaos-refund-{booking_id[:8]}")

            plan = self._plan(booking_id)
            if plan.delay:
                await asyncio.sleep(plan.delay)
            if plan.refund_mode == "raise_before":
                self.refund_state[booking_id] = "raise_before"
                raise RuntimeError("chaos provider refund timed out before return")
            if plan.refund_mode == "failed":
                self.refund_state[booking_id] = "failed"
                return PaymentResult("failed", f"chaos-refund-failed-{booking_id[:8]}")

            self.refunded[booking_id] = self._money(amount)
            if plan.refund_mode == "raise_after":
                self.refund_state[booking_id] = "raise_after"
                self.unknown_after_refund.add(booking_id)
                raise RuntimeError("chaos provider refund timed out after return")

            self.refund_state[booking_id] = "refunded"
            return PaymentResult("refunded", f"chaos-refund-{booking_id[:8]}")


class ChaosRunner:
    def __init__(
        self,
        *,
        clients: list[httpx.AsyncClient],
        data: SeedData,
        seed: int,
        provider: ChaosProvider | None,
        stats: RunStats,
    ) -> None:
        self.clients = clients
        self.data = data
        self.seed = seed
        self.provider = provider
        self.stats = stats
        self.bookings: list[list[dict[str, Any]]] = []
        self.idempotent_payloads: dict[tuple[int, str], dict[str, Any]] = {}
        self.pay_recovery_started: set[str] = set()
        self.refund_recovery_started: set[str] = set()

    def _client(self, guest: int) -> httpx.AsyncClient:
        return self.clients[guest % len(self.clients)]

    def _hold_body(self, guest: int, rng: random.Random) -> tuple[dict[str, Any], str | None]:
        unit_type_id = rng.choice(self.data.unit_type_ids)
        offset = rng.randrange(5)
        checkin = self.data.stay_start + timedelta(days=offset)
        max_nights = min(3, 6 - offset)
        checkout = checkin + timedelta(days=rng.randint(1, max_nights))
        body = {
            "unit_type_id": unit_type_id,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "guest": {
                "name": f"Chaos guest {guest}",
                "email": self.data.guest_emails[guest],
                "phone": "+15550000000",
            },
        }
        key: str | None = None
        if rng.random() < 0.42:
            key = f"{self.data.tag}-g{guest}-idem{rng.randrange(4)}"
            old = self.idempotent_payloads.get((guest, key))
            if old is not None and rng.random() < 0.65:
                body = old.copy()
            else:
                self.idempotent_payloads.setdefault((guest, key), body.copy())
        return body, key

    async def _request(
        self,
        client: httpx.AsyncClient,
        action: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response | None:
        try:
            response = await client.request(method, path, **kwargs)
        except Exception:
            self.stats.request_outcomes[f"{action}:exception"] += 1
            return None
        code = str(response.status_code)
        self.stats.status_codes[code] += 1
        self.stats.request_outcomes[f"{action}:{code}"] += 1
        if response.status_code >= 400:
            try:
                payload = response.json()
                detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            except ValueError:
                detail = response.text
            self.stats.request_errors.append(
                {
                    "order": self.stats.next_order(),
                    "action": action,
                    "status": response.status_code,
                    "path": path,
                    "detail": str(detail)[:200],
                }
            )
        return response

    @staticmethod
    def _response_label(response: httpx.Response | None) -> str:
        return str(response.status_code) if response is not None else "exception"

    async def _hold(
        self, guest: int, op_index: int, rng: random.Random, trace: dict[str, Any]
    ) -> str:
        body, key = self._hold_body(guest, rng)
        trace.update(
            {
                "unit_type_id": body["unit_type_id"],
                "checkin": body["checkin"],
                "checkout": body["checkout"],
                "idempotency_key": key,
            }
        )
        headers = {"Idempotency-Key": key} if key else {}
        response = await self._request(
            self._client(guest), "hold", "POST", "/v1/bookings/hold", json=body, headers=headers
        )
        if response is None:
            return "exception"
        if response.status_code == 201:
            result = response.json()
            booking_id = result["id"]
            booking = {
                "id": booking_id,
                "code": result["code"],
                "token": result["access_token"],
                "email": body["guest"]["email"],
                "unit_type_id": body["unit_type_id"],
            }
            self.bookings[guest].append(booking)
            trace["booking_id"] = booking_id
            if self.provider is not None:
                self.provider.register_booking(booking_id, f"guest:{guest}:operation:{op_index}")
            return "created_or_replayed"
        return f"http_{response.status_code}"

    def _target(self, guest: int, rng: random.Random) -> dict[str, Any] | None:
        return rng.choice(self.bookings[guest]) if self.bookings[guest] else None

    async def _pay(self, guest: int, booking: dict[str, Any]) -> str:
        booking_id = booking["id"]
        headers = {"X-Booking-Token": booking["token"]}
        response = await self._request(
            self._client(guest),
            "pay",
            "POST",
            f"/v1/bookings/{booking_id}/pay",
            headers=headers,
        )
        statuses = [self._response_label(response)]
        if (
            response is not None
            and response.status_code >= 500
            and self.provider is not None
            and booking_id in self.provider.unknown_after_charge
            and booking_id not in self.pay_recovery_started
        ):
            self.pay_recovery_started.add(booking_id)
            self.stats.background["unknown_pay_retry_attempts"] += 1
            retry = await self._request(
                self._client(guest),
                "pay_retry",
                "POST",
                f"/v1/bookings/{booking_id}/pay",
                headers=headers,
            )
            statuses.append(self._response_label(retry))
            self.stats.retry_results.setdefault(booking_id, []).append(
                retry.status_code if retry is not None else None
            )
            if retry is not None and 200 <= retry.status_code < 300:
                self.stats.background["unknown_pay_recovered"] += 1
        return "->".join(statuses)

    async def _refund(self, guest: int, booking: dict[str, Any]) -> str:
        booking_id = booking["id"]
        headers = {"X-Booking-Token": booking["token"]}
        response = await self._request(
            self._client(guest),
            "refund",
            "POST",
            f"/v1/bookings/{booking_id}/refund",
            headers=headers,
        )
        statuses = [self._response_label(response)]
        if (
            response is not None
            and response.status_code >= 500
            and self.provider is not None
            and booking_id in self.provider.unknown_after_refund
            and booking_id not in self.refund_recovery_started
        ):
            self.refund_recovery_started.add(booking_id)
            self.stats.background["unknown_refund_retry_attempts"] += 1
            retry = await self._request(
                self._client(guest),
                "refund_retry",
                "POST",
                f"/v1/bookings/{booking_id}/refund",
                headers=headers,
            )
            statuses.append(self._response_label(retry))
            self.stats.retry_results.setdefault(booking_id, []).append(
                retry.status_code if retry is not None else None
            )
            if retry is not None and 200 <= retry.status_code < 300:
                self.stats.background["unknown_refund_recovered"] += 1
        return "->".join(statuses)

    async def _operate(
        self,
        guest: int,
        op_index: int,
        action: str,
        rng: random.Random,
        trace: dict[str, Any],
    ) -> str:
        client = self._client(guest)
        if action == "hold":
            return await self._hold(guest, op_index, rng, trace)

        booking = self._target(guest, rng)
        if action in {"pay", "pay_parallel", "cancel", "refund", "refund_parallel", "lookup"}:
            if booking is None:
                return "no_booking"

        if action == "pay":
            trace["booking_id"] = booking["id"]
            return await self._pay(guest, booking)
        if action == "pay_parallel":
            trace["booking_id"] = booking["id"]
            results = await asyncio.gather(
                self._pay(guest, booking),
                self._pay(guest, booking),
            )
            return "parallel:" + ",".join(results)
        if action == "cancel":
            trace["booking_id"] = booking["id"]
            response = await self._request(
                client,
                "cancel",
                "POST",
                f"/v1/bookings/{booking['id']}/cancel",
                headers={"X-Booking-Token": booking["token"]},
            )
            return self._response_label(response)
        if action == "refund":
            trace["booking_id"] = booking["id"]
            return await self._refund(guest, booking)
        if action == "refund_parallel":
            trace["booking_id"] = booking["id"]
            results = await asyncio.gather(
                self._refund(guest, booking),
                self._refund(guest, booking),
            )
            return "parallel:" + ",".join(results)
        if action == "lookup":
            trace["booking_id"] = booking["id"]
            email = booking["email"]
            if rng.random() < 0.08:
                email = f"wrong-{email}"
            response = await self._request(
                client,
                "lookup",
                "POST",
                "/v1/bookings/lookup",
                json={"code": booking["code"], "email": email},
            )
            return self._response_label(response)
        if action == "availability":
            unit_type_id = rng.choice(self.data.unit_type_ids)
            offset = rng.randrange(5)
            start = self.data.stay_start + timedelta(days=offset)
            end = start + timedelta(days=rng.randint(1, min(3, 6 - offset)))
            trace.update(
                {
                    "unit_type_id": unit_type_id,
                    "date_from": start.isoformat(),
                    "date_to": end.isoformat(),
                }
            )
            response = await self._request(
                client,
                "availability",
                "GET",
                "/v1/inventory/availability",
                params={
                    "unit_type_id": unit_type_id,
                    "date_from": start.isoformat(),
                    "date_to": end.isoformat(),
                },
            )
            return self._response_label(response)
        return "unknown_action"

    async def guest(self, guest: int, actions: list[str]) -> None:
        rng = random.Random(self.seed ^ (guest * 1_000_003 + 0x6A09E667))
        for op_index, action in enumerate(actions):
            self.stats.operations += 1
            trace: dict[str, Any] = {
                "order": self.stats.next_order(),
                "guest": guest,
                "index": op_index,
                "action": action,
            }
            try:
                outcome = await self._operate(guest, op_index, action, rng, trace)
            except Exception as error:
                outcome = f"raised:{type(error).__name__}"
                trace["error"] = str(error)[:160]
            trace["outcome"] = outcome
            self.stats.operations_by_outcome[f"{action}:{outcome}"] += 1
            self.stats.trace.append(trace)


_ACTIONS = (
    ("hold", 34),
    ("pay", 18),
    ("pay_parallel", 9),
    ("cancel", 12),
    ("refund", 12),
    ("refund_parallel", 5),
    ("lookup", 5),
    ("availability", 5),
)


def _plans(seed: int, ops: int, guests: int) -> list[list[str]]:
    base, extra = divmod(ops, guests)
    plans: list[list[str]] = []
    for guest in range(guests):
        rng = random.Random(seed + guest * 1_000_003)
        count = base + (1 if guest < extra else 0)
        plans.append(
            [
                rng.choices([item[0] for item in _ACTIONS], [item[1] for item in _ACTIONS])[0]
                for _ in range(count)
            ]
        )
    return plans


async def _seed_scenario(conn: asyncpg.Connection, seed: int, guests: int) -> SeedData:
    tag = f"chaos-{seed}-{uuid.uuid4().hex[:10]}"
    partner_email = f"{tag}-partner@example.com"
    guest_emails = [f"{tag}-guest{guest}@example.com" for guest in range(guests)]
    inventory_start = date.today() + timedelta(days=1)
    stay_start = inventory_start + timedelta(days=2)
    async with conn.transaction():
        partner_id = await conn.fetchval(
            "INSERT INTO partner (email, password_hash, name) "
            "VALUES ($1, 'chaos-only', $2) RETURNING id::text",
            partner_email,
            f"Chaos {tag}",
        )
        property_id = await conn.fetchval(
            """
            INSERT INTO property
                (partner_id, name, slug, property_type, city, timezone, status)
            VALUES ($1, $2, $3, 'apartment', 'Chaos Lab', 'UTC', 'published')
            RETURNING id::text
            """,
            partner_id,
            f"Chaos {tag}",
            tag,
        )
        unit_ids: list[str] = []
        for index, units in enumerate((1, 2, 3), start=1):
            unit_id = await conn.fetchval(
                """
                INSERT INTO unit_type (property_id, name, capacity, total_units, base_price)
                VALUES ($1, $2, 4, $3, 100)
                RETURNING id::text
                """,
                property_id,
                f"Chaos unit {index}",
                units,
            )
            unit_ids.append(unit_id)
            await conn.execute(
                """
                INSERT INTO inventory_day (unit_type_id, date, available)
                SELECT $1, d::date, $2
                FROM generate_series($3::date, ($3::date + 20)::date, interval '1 day') AS d
                """,
                unit_id,
                units,
                inventory_start,
            )
    return SeedData(
        tag=tag,
        partner_id=partner_id,
        property_id=property_id,
        unit_type_ids=unit_ids,
        guest_emails=guest_emails,
        inventory_start=inventory_start,
        stay_start=stay_start,
    )


async def _cleanup_scenario(conn: asyncpg.Connection, data: SeedData, keep: bool) -> None:
    if keep:
        return
    guest_pattern = f"{data.tag}-guest%@example.com"
    async with conn.transaction():
        await conn.execute(
            """
            DELETE FROM outbox_event e
            WHERE (e.aggregate = 'booking' AND e.aggregate_id IN (
                      SELECT b.id::text FROM booking b
                      WHERE b.guest_email::text LIKE $1
                  ))
               OR (e.aggregate = 'inventory' AND e.aggregate_id IN (
                      SELECT ut.id::text FROM unit_type ut WHERE ut.property_id = $2
                  ))
            """,
            guest_pattern,
            data.property_id,
        )
        await conn.execute(
            "DELETE FROM payment WHERE booking_id IN "
            "(SELECT id FROM booking WHERE guest_email::text LIKE $1)",
            guest_pattern,
        )
        await conn.execute(
            "DELETE FROM booking_line WHERE booking_id IN "
            "(SELECT id FROM booking WHERE guest_email::text LIKE $1)",
            guest_pattern,
        )
        await conn.execute(
            "DELETE FROM booking WHERE guest_email::text LIKE $1",
            guest_pattern,
        )
        await conn.execute(
            "DELETE FROM inventory_day WHERE unit_type_id IN "
            "(SELECT id FROM unit_type WHERE property_id = $1)",
            data.property_id,
        )
        await conn.execute("DELETE FROM unit_type WHERE property_id = $1", data.property_id)
        await conn.execute("DELETE FROM property WHERE id = $1", data.property_id)
        await conn.execute("DELETE FROM partner WHERE id = $1", data.partner_id)


async def _interfere(
    pool: Any,
    data: SeedData,
    seed: int,
    stop: asyncio.Event,
    stats: RunStats,
) -> None:
    from app.modules.booking.service import expire_holds
    from app.modules.inventory.service import close_range

    rng = random.Random(seed ^ 0xBB67AE85)
    while not stop.is_set():
        try:
            async with pool.acquire() as conn:
                held = await conn.fetch(
                    """
                    SELECT id::text FROM booking
                    WHERE unit_type_id = ANY($1::uuid[]) AND status = 'hold'
                    ORDER BY id
                    """,
                    data.unit_type_ids,
                )
                if held and rng.random() < 0.22:
                    target = rng.choice(held)["id"]
                    update_status = await conn.execute(
                        """
                        UPDATE booking SET hold_expires_at = now() - interval '1 minute'
                        WHERE id = $1 AND status = 'hold'
                        """,
                        target,
                    )
                    if update_status == "UPDATE 1":
                        stats.background["forced_expiries"] += 1
                        stats.background_trace.append(
                            {
                                "order": stats.next_order(),
                                "action": "force_expiry",
                                "booking_id": target,
                            }
                        )
                stats.background["reaper_calls"] += 1
                reaped = await expire_holds(conn)
                stats.background["reaped_holds"] += reaped
                if reaped:
                    stats.background_trace.append(
                        {
                            "order": stats.next_order(),
                            "action": "expire_holds",
                            "count": reaped,
                        }
                    )

                if rng.random() < 0.8:
                    unit_id = rng.choice(data.unit_type_ids)
                    offset = rng.randrange(6)
                    days = rng.randint(1, min(3, 6 - offset))
                    start = data.stay_start + timedelta(days=offset)
                    async with conn.transaction():
                        await close_range(
                            conn,
                            unit_id,
                            start,
                            start + timedelta(days=days),
                            closed=bool(rng.getrandbits(1)),
                        )
                    stats.background["stop_sell_toggles"] += 1
                    stats.background_trace.append(
                        {
                            "order": stats.next_order(),
                            "action": "stop_sell",
                            "unit_type_id": unit_id,
                            "date_from": start.isoformat(),
                            "date_to": (start + timedelta(days=days)).isoformat(),
                        }
                    )
        except Exception:
            stats.background["errors"] += 1
        await asyncio.sleep(0.02)


def _finding_dicts(report: CheckReport) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return report.as_dict()["violations"], report.as_dict()["warnings"]


async def _provider_analysis(
    conn: asyncpg.Connection,
    data: SeedData,
    provider: ChaosProvider,
    runner: ChaosRunner,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = await conn.fetch(
        """
        SELECT id::text AS id, status, total_amount::numeric AS total_amount,
               hold_expires_at
        FROM booking WHERE unit_type_id = ANY($1::uuid[])
        """,
        data.unit_type_ids,
    )
    bookings = {row["id"]: row for row in rows}
    ids = list(bookings)
    payment_rows = (
        await conn.fetch(
            """
            SELECT booking_id::text AS booking_id, status,
                   amount::numeric AS amount, created_at, id::text AS payment_id
            FROM payment
            WHERE booking_id = ANY($1::uuid[])
            ORDER BY booking_id, created_at, id
            """,
            ids,
        )
        if ids
        else []
    )
    history: dict[str, list[asyncpg.Record]] = defaultdict(list)
    for row in payment_rows:
        history[row["booking_id"]].append(row)

    discrepancies: list[dict[str, Any]] = []
    unknown_details: list[dict[str, Any]] = []
    for booking_id, charged in provider.charged.items():
        booking = bookings.get(booking_id)
        entries = history.get(booking_id, [])
        successes = [event for event in entries if event["status"] == "succeeded"]
        refunds = [event for event in entries if event["status"] == "refunded"]
        state = booking["status"] if booking else "missing"
        refund_total = sum((event["amount"] for event in refunds), Decimal("0"))
        refund_after_latest = False
        if successes:
            last_success_key = (successes[-1]["created_at"], successes[-1]["payment_id"])
            refund_after_latest = any(
                (event["created_at"], event["payment_id"]) > last_success_key for event in refunds
            )

        issue: list[str] = []
        returned = provider.refunded.get(booking_id)
        if not successes:
            issue.append("provider charge has no succeeded payment row")
        elif len(successes) > 1:
            issue.append(f"database has {len(successes)} succeeded rows for one idempotent charge")
        if returned is None:
            if state not in ("confirmed", "paid") or not successes or refund_after_latest:
                issue.append(f"unreturned provider charge ends in booking status {state}")
        else:
            if not refund_after_latest:
                issue.append("provider refund has no refunded payment row after the latest charge")
            if refund_total != returned:
                issue.append(
                    f"provider returned {returned} but database refund rows total {refund_total}"
                )
            if state not in ("cancelled", "refunded"):
                issue.append(f"returned provider charge leaves booking status {state}")

        if issue:
            discrepancies.append(
                {
                    "booking_id": booking_id,
                    "status": state,
                    "charged": str(charged),
                    "refunded": str(returned) if returned is not None else None,
                    "succeeded_rows": len(successes),
                    "refunded_rows": len(refunds),
                    "pay_mode": provider.plans[booking_id].pay_mode,
                    "refund_mode": provider.plans[booking_id].refund_mode,
                    "pay_calls": provider.call_counts[booking_id]["pay"],
                    "refund_calls": provider.call_counts[booking_id]["refund"],
                    "details": issue,
                }
            )

    for booking_id in sorted(provider.unknown_after_charge):
        booking = bookings.get(booking_id)
        entries = history.get(booking_id, [])
        successes = [event for event in entries if event["status"] == "succeeded"]
        refunds = [event for event in entries if event["status"] == "refunded"]
        status = booking["status"] if booking else "missing"
        net_returned = provider.refunded.get(booking_id) is not None
        recovered = (status in ("cancelled", "refunded") and net_returned and bool(refunds)) or (
            status in ("confirmed", "paid") and not net_returned and bool(successes)
        )
        holdable = bool(
            booking
            and status == "hold"
            and (
                booking["hold_expires_at"] is None
                or booking["hold_expires_at"] > dt.datetime.now(dt.UTC)
            )
        )
        unknown_details.append(
            {
                "booking_id": booking_id,
                "status": status,
                "provider_refunded": net_returned,
                "succeeded_rows": len(successes),
                "refunded_rows": len(refunds),
                "retry_attempted": booking_id in runner.pay_recovery_started,
                "retry_results": runner.stats.retry_results.get(booking_id, []),
                "recovered": recovered,
                "still_holdable": holdable,
            }
        )

    summary = {
        "charges": len(provider.charged),
        "provider_refunds": len(provider.refunded),
        "pay_calls": sum(counts["pay"] for counts in provider.call_counts.values()),
        "refund_calls": sum(counts["refund"] for counts in provider.call_counts.values()),
        "unknown_after_charge_count": len(provider.unknown_after_charge),
        "unknown_after_charge": unknown_details,
        "unknown_after_refund_count": len(provider.unknown_after_refund),
        "pay_modes": dict(Counter(plan.pay_mode for plan in provider.plans.values())),
        "refund_modes": dict(Counter(plan.refund_mode for plan in provider.plans.values())),
        "per_booking": {
            booking_id: {
                "pay_mode": plan.pay_mode,
                "refund_mode": plan.refund_mode,
                "charged": str(provider.charged[booking_id])
                if booking_id in provider.charged
                else None,
                "refunded": str(provider.refunded[booking_id])
                if booking_id in provider.refunded
                else None,
                "pay_calls": provider.call_counts[booking_id]["pay"],
                "refund_calls": provider.call_counts[booking_id]["refund"],
            }
            for booking_id, plan in provider.plans.items()
        },
        "discrepancies": discrepancies,
    }
    rig_findings = [
        {
            "invariant": "provider_truth",
            "ids": [item["booking_id"]],
            "details": item["details"],
        }
        for item in discrepancies
    ]
    return summary, rig_findings


def _result(
    *,
    seed: int,
    ops: int,
    guests: int,
    stats: RunStats,
    report: CheckReport,
    provider_summary: dict[str, Any] | None,
    provider_findings: list[dict[str, Any]],
    keep: bool,
) -> dict[str, Any]:
    checker_violations, warnings = _finding_dicts(report)
    violations = checker_violations + provider_findings
    result: dict[str, Any] = {
        "seed": seed,
        "ops_requested": ops,
        "ops_completed": stats.operations,
        "guests": guests,
        "operations_by_outcome": dict(sorted(stats.operations_by_outcome.items())),
        "request_outcomes": dict(sorted(stats.request_outcomes.items())),
        "status_code_histogram": dict(sorted(stats.status_codes.items())),
        "background": dict(sorted(stats.background.items())),
        "checker_violations": checker_violations,
        "warnings": warnings,
        "provider": provider_summary,
        "violations": violations,
        "kept": keep,
    }
    if violations:
        result["repro_trace"] = sorted(stats.trace, key=lambda item: item["order"])
        result["background_trace"] = sorted(stats.background_trace, key=lambda item: item["order"])
        result["request_errors"] = stats.request_errors
    return result


async def run_inproc_round(
    *,
    seed: int,
    ops: int,
    guests: int,
    dsn: str | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    from app.config.settings import settings

    original_settings = {
        key: getattr(settings, key)
        for key in (
            "database_url",
            "run_background_jobs",
            "login_limit_per_min",
            "register_limit_per_min",
            "lookup_limit_per_min",
            "media_dir",
        )
    }
    from app.db.pool import get_pool
    from app.main import create_app
    from app.modules.payment import service as payment_service

    target_dsn = dsn or settings.pool_dsn
    if dsn is not None and os.environ.get("PYTEST_RUNNING") == "1":
        if dsn != settings.pool_dsn:
            raise ValueError("pytest inproc runs must use settings.pool_dsn")
    elif dsn is not None:
        settings.database_url = dsn

    settings.run_background_jobs = False
    settings.login_limit_per_min = 1_000_000
    settings.register_limit_per_min = 1_000_000
    settings.lookup_limit_per_min = 1_000_000
    provider = ChaosProvider(seed)
    previous_provider = payment_service.get_payment_provider
    payment_service.get_payment_provider = lambda: provider
    stats = RunStats()
    report = CheckReport()
    provider_summary: dict[str, Any] | None = None
    provider_findings: list[dict[str, Any]] = []
    data: SeedData | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="chaos-bookings-media-") as media_dir:
            settings.media_dir = Path(media_dir)
            apps = [create_app(), create_app()]
            async with AsyncExitStack() as stack:
                for app in apps:
                    await stack.enter_async_context(app.router.lifespan_context(app))
                pool = get_pool()
                async with pool.acquire() as conn:
                    data = await _seed_scenario(conn, seed, guests)

                transports = [
                    httpx.ASGITransport(app=app, raise_app_exceptions=False) for app in apps
                ]
                clients = [
                    httpx.AsyncClient(transport=transport, base_url="http://chaos.local")
                    for transport in transports
                ]
                runner = ChaosRunner(
                    clients=clients,
                    data=data,
                    seed=seed,
                    provider=provider,
                    stats=stats,
                )
                runner.bookings = [[] for _ in range(guests)]
                stop = asyncio.Event()
                interferer = asyncio.create_task(_interfere(pool, data, seed, stop, stats))
                try:
                    await asyncio.gather(
                        *(
                            runner.guest(guest, actions)
                            for guest, actions in enumerate(_plans(seed, ops, guests))
                        )
                    )
                finally:
                    stop.set()
                    await interferer
                    for client in clients:
                        await client.aclose()

                async with pool.acquire() as conn:
                    await __import__(
                        "app.modules.booking.service", fromlist=["expire_holds"]
                    ).expire_holds(conn)
                    provider_summary, provider_findings = await _provider_analysis(
                        conn, data, provider, runner
                    )
                report = await check_dsn(target_dsn)
                if not keep:
                    async with pool.acquire() as conn:
                        await _cleanup_scenario(conn, data, keep=False)
                result = _result(
                    seed=seed,
                    ops=ops,
                    guests=guests,
                    stats=stats,
                    report=report,
                    provider_summary=provider_summary,
                    provider_findings=provider_findings,
                    keep=keep,
                )
                return result
    finally:
        payment_service.get_payment_provider = previous_provider
        for key, value in original_settings.items():
            setattr(settings, key, value)


async def run_http_round(
    *,
    seed: int,
    ops: int,
    guests: int,
    dsn: str,
    base_url: str,
    keep: bool = False,
) -> dict[str, Any]:
    from app.modules.booking.service import expire_holds

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4, command_timeout=30)
    stats = RunStats()
    report = CheckReport()
    data: SeedData | None = None
    try:
        async with pool.acquire() as conn:
            data = await _seed_scenario(conn, seed, guests)
        limits = httpx.Limits(
            max_connections=max(100, guests * 2),
            max_keepalive_connections=max(20, guests),
        )
        async with httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits) as client:
            runner = ChaosRunner(
                clients=[client],
                data=data,
                seed=seed,
                provider=None,
                stats=stats,
            )
            runner.bookings = [[] for _ in range(guests)]
            stop = asyncio.Event()
            interferer = asyncio.create_task(_interfere(pool, data, seed, stop, stats))
            try:
                await asyncio.gather(
                    *(
                        runner.guest(guest, actions)
                        for guest, actions in enumerate(_plans(seed, ops, guests))
                    )
                )
            finally:
                stop.set()
                await interferer
        async with pool.acquire() as conn:
            await expire_holds(conn)
            provider_summary: dict[str, Any] = {
                "mode": "stub (server)",
                "fault_injection": False,
            }
        report = await check_dsn(dsn)
        if not keep:
            async with pool.acquire() as conn:
                await _cleanup_scenario(conn, data, keep=False)
        return _result(
            seed=seed,
            ops=ops,
            guests=guests,
            stats=stats,
            report=report,
            provider_summary=provider_summary,
            provider_findings=[],
            keep=keep,
        )
    finally:
        await pool.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    inproc = subparsers.add_parser(
        "inproc", help="run against the real ASGI app with fault injection"
    )
    inproc.add_argument("--seed", type=int, default=1)
    inproc.add_argument("--ops", type=int, default=300)
    inproc.add_argument("--guests", type=int, default=32)
    inproc.add_argument("--rounds", type=int, default=1)
    inproc.add_argument("--dsn")
    inproc.add_argument("--keep", action="store_true")

    http = subparsers.add_parser("http", help="run against an already-started HTTP server")
    http.add_argument("--seed", type=int, default=1)
    http.add_argument("--ops", type=int, default=2000)
    http.add_argument("--guests", type=int, default=64)
    http.add_argument("--rounds", type=int, default=1)
    http.add_argument("--dsn", required=True)
    http.add_argument("--base-url", default="http://127.0.0.1:8300")
    http.add_argument("--keep", action="store_true")
    for sub in (inproc, http):
        sub.add_argument(
            "--allow-any-database",
            action="store_true",
            help="skip the check that the database name says it is throwaway",
        )
    return parser


_THROWAWAY_WORDS = ("chaos", "test", "scratch", "tmp")


def _refuse_a_real_database(dsn: str, allowed: bool) -> None:
    """The rig seeds fake partners, guests and bookings: it must not run against real data."""
    name = urlsplit(dsn).path.lstrip("/")
    if not allowed and not any(word in name for word in _THROWAWAY_WORDS):
        raise ValueError(
            f"refusing to write fake bookings into database {name!r}: use a throwaway one "
            f"(its name should contain one of {_THROWAWAY_WORDS}) or pass --allow-any-database"
        )


async def _main() -> int:
    args = _parser().parse_args()
    if args.ops < 1 or args.guests < 1 or args.rounds < 1:
        raise ValueError("--ops, --guests, and --rounds must be positive")

    from app.config.settings import settings

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError("set DATABASE_URL to a throwaway database or pass --dsn")
    _refuse_a_real_database(dsn, args.allow_any_database)
    settings.database_url = dsn
    failed = False
    for offset in range(args.rounds):
        seed = args.seed + offset
        if args.mode == "inproc":
            result = await run_inproc_round(
                seed=seed,
                ops=args.ops,
                guests=args.guests,
                dsn=dsn,
                keep=args.keep,
            )
        else:
            result = await run_http_round(
                seed=seed,
                ops=args.ops,
                guests=args.guests,
                dsn=dsn,
                base_url=args.base_url,
                keep=args.keep,
            )
        print(json.dumps(result, sort_keys=True))
        failed = failed or bool(result["violations"])
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except Exception as error:
        print(f"chaos runner error: {error}", file=sys.stderr)
        sys.exit(2)
