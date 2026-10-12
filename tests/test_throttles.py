"""The expensive endpoints are throttled per client address, and the limiter survives Redis."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from starlette.testclient import TestClient

from app.config.settings import settings
from app.main import create_app
from app.modules.auth.jwt import create_access_token
from app.utils import ratelimit
from tests.test_public_ratelimit import _ClientAddress, _fresh_ip

pytestmark = pytest.mark.usefixtures(
    "_test_db"
)  # the app under test boots against the suite's database

TODAY = dt.date.today()


def _from(client: TestClient, ip: str) -> dict:
    return {"X-Test-Client": ip}


def _partner() -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(uuid.uuid4()), 'partner')}"}


# name of the setting, how to call the route once
ROUTES = {
    "hold_limit_per_min": lambda c, h: c.post("/v1/bookings/hold", json={}, headers=h),
    "availability_limit_per_min": lambda c, h: c.get(
        "/v1/availability",
        params={
            "unit_type_id": str(uuid.uuid4()),
            "date_from": TODAY.isoformat(),
            "date_to": (TODAY + dt.timedelta(days=2)).isoformat(),
        },
        headers=h,
    ),
    "search_limit_per_min": lambda c, h: c.get("/v1/properties", headers=h),
    "photo_upload_limit_per_min": lambda c, h: c.post(
        f"/v1/partner/properties/{uuid.uuid4()}/photos",
        files={"file": ("x.png", b"not an image", "image/png")},
        headers={**h, **_partner()},
    ),
    "ical_sync_limit_per_min": lambda c, h: c.post(
        f"/v1/partner/unit-types/{uuid.uuid4()}/ical-import/sync", headers={**h, **_partner()}
    ),
}


@pytest.mark.parametrize("setting", sorted(ROUTES))
def test_the_route_answers_429_after_its_limit_per_client_address(setting, monkeypatch) -> None:
    monkeypatch.setattr(settings, setting, 2)
    busy, other = _fresh_ip(), _fresh_ip()
    call = ROUTES[setting]

    with TestClient(_ClientAddress(create_app())) as client:
        answers = [call(client, _from(client, busy)).status_code for _ in range(3)]
        blocked = call(client, _from(client, busy))
        elsewhere = call(client, _from(client, other)).status_code

    assert 429 not in answers[:2], answers
    assert answers[2] == 429 and blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert elsewhere != 429, "another client address has its own budget"


@pytest.mark.parametrize("setting", sorted(ROUTES))
def test_zero_switches_the_route_off(setting, monkeypatch) -> None:
    monkeypatch.setattr(settings, setting, 0)

    with TestClient(_ClientAddress(create_app())) as client:
        response = ROUTES[setting](client, _from(client, _fresh_ip()))

    assert response.status_code == 429


# ---------------------------------------------------------------- Redis is down


class _DeadRedis:
    async def eval(self, *args, **kwargs):
        raise ConnectionError("Error 111 connecting to redis:6379")


@pytest.fixture
def redis_down(monkeypatch):
    monkeypatch.setattr(ratelimit, "get_redis", lambda: _DeadRedis())
    monkeypatch.setattr(ratelimit, "_last_fallback_log", 0.0)
    monkeypatch.setattr(ratelimit, "_local", {})


@pytest.mark.asyncio
async def test_while_redis_is_down_each_process_still_counts(redis_down) -> None:
    key = f"rl:test:{uuid.uuid4().hex}"

    first = [await ratelimit.acquire(key, 3, 60) for _ in range(3)]
    fourth = await ratelimit.acquire(key, 3, 60)
    other_key = await ratelimit.acquire(key + "-other", 3, 60)

    assert first == [(True, 0)] * 3
    assert fourth[0] is False and 1 <= fourth[1] <= 61
    assert other_key == (True, 0)


@pytest.mark.asyncio
async def test_the_local_window_ends(redis_down, monkeypatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock["now"])
    key = f"rl:test:{uuid.uuid4().hex}"

    assert [(await ratelimit.acquire(key, 1, 60))[0] for _ in range(2)] == [True, False]
    clock["now"] += 61

    assert (await ratelimit.acquire(key, 1, 60))[0] is True


@pytest.mark.asyncio
async def test_the_local_counters_cannot_grow_without_bound(redis_down, monkeypatch) -> None:
    monkeypatch.setattr(ratelimit, "_LOCAL_MAX_KEYS", 50)

    for i in range(500):
        await ratelimit.acquire(f"rl:flood:{i}", 5, 60)

    assert len(ratelimit._local) <= 50


@pytest.mark.asyncio
async def test_the_outage_is_logged_once_not_once_per_request(redis_down, monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(ratelimit.log, "warning", lambda event, **kw: events.append(event))

    for i in range(20):
        await ratelimit.acquire(f"rl:log:{i}", 5, 60)

    assert events == ["ratelimit-local-fallback"]


@pytest.mark.asyncio
async def test_a_zero_limit_blocks_without_asking_redis(redis_down) -> None:
    assert await ratelimit.acquire("rl:test:zero", 0, 60) == (False, 60)
