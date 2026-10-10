"""Rate limits on the public endpoints that take guesses.

Logins take a password guess and the booking lookup takes a code guess, so
both are throttled per client address and, against guesses spread over many
addresses, per account or per code. Each test uses client addresses of its own
(Redis counters outlive a test), and the rest of the suite runs with the limits
relaxed (see conftest.py).
"""

from __future__ import annotations

import itertools
import uuid
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app.config.settings import settings
from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import PartnerRegisterRequest
from app.utils import ratelimit
from app.utils.ratelimit_http import SUBJECT_FACTOR, enforce


class _ClientAddress:
    """Test wrapper: one app (one lifespan) called from many client addresses.

    The address comes from the X-Test-Client header, the way a proxy's
    forwarded address would reach the app.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            ip = dict(scope["headers"]).get(b"x-test-client")
            if ip:
                scope = {**scope, "client": (ip.decode(), 50000)}
        await self.app(scope, receive, send)


def _fresh_ip() -> str:
    """A client address no other test (or earlier run, within the window) used."""
    n = uuid.uuid4().int
    return f"10.{n % 250 + 1}.{n // 250 % 250 + 1}.{n // 62500 % 250 + 1}"


def _post(client: TestClient, path: str, body: dict, ip: str | None = None):
    return client.post(path, json=body, headers={"X-Test-Client": ip or _fresh_ip()})


def _login(client: TestClient, ip: str, email: str, password: str = "wrong-password"):
    return _post(client, "/v1/auth/login", {"email": email, "password": password}, ip)


def _email() -> str:
    return f"rl-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def tight(monkeypatch) -> int:
    """3 attempts per address per window for every public endpoint."""
    monkeypatch.setattr(settings, "login_limit_per_min", 3)
    monkeypatch.setattr(settings, "register_limit_per_min", 3)
    monkeypatch.setattr(settings, "lookup_limit_per_min", 3)
    return 3


@pytest.mark.asyncio
async def test_login_is_limited_per_address(committed_conn, tight) -> None:
    email = _email()
    await auth_service.register_partner(
        committed_conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    ip = _fresh_ip()

    with TestClient(_ClientAddress(create_app())) as client:
        wrong = [_login(client, ip, email).status_code for _ in range(tight)]
        blocked = _login(client, ip, email)
        # Counting comes before checking: the right password does not get past the limit.
        blocked_right = _login(client, ip, email, "secret123")
        other_address = _login(client, _fresh_ip(), _email())

    assert wrong == [401] * tight
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked_right.status_code == 429
    assert other_address.status_code == 401


@pytest.mark.asyncio
async def test_one_account_is_limited_across_addresses(committed_conn, tight) -> None:
    """A password guessed from many addresses still meets the per-account ceiling."""
    email = _email()
    ceiling = tight * SUBJECT_FACTOR

    with TestClient(_ClientAddress(create_app())) as client:
        codes = [_login(client, _fresh_ip(), email).status_code for _ in range(ceiling + 1)]

    assert codes == [401] * ceiling + [429]


@pytest.mark.asyncio
async def test_admin_login_is_limited(committed_conn, tight) -> None:
    ip, email = _fresh_ip(), _email()

    with TestClient(_ClientAddress(create_app())) as client:
        statuses = [
            _post(
                client, "/v1/admin/login", {"email": email, "password": "wrong-password"}, ip
            ).status_code
            for _ in range(tight + 1)
        ]

    assert statuses == [401] * tight + [429]


@pytest.mark.asyncio
async def test_register_is_limited(committed_conn, tight) -> None:
    ip = _fresh_ip()

    with TestClient(_ClientAddress(create_app())) as client:
        statuses = [
            _post(
                client,
                "/v1/auth/register",
                {"email": _email(), "password": "secret123", "name": "Partner"},
                ip,
            ).status_code
            for _ in range(tight + 1)
        ]

    assert statuses == [201] * tight + [429]


@pytest.mark.asyncio
async def test_booking_lookup_is_limited_per_address_and_per_code(committed_conn, tight) -> None:
    def lookup(client: TestClient, ip: str, code: str):
        return _post(
            client, "/v1/bookings/lookup", {"code": code, "email": "nobody@example.com"}, ip
        ).status_code

    ceiling = tight * SUBJECT_FACTOR
    one_ip = _fresh_ip()
    guessed = f"BK-{uuid.uuid4().hex[:6].upper()}"

    with TestClient(_ClientAddress(create_app())) as client:
        by_address = [
            lookup(client, one_ip, f"BK-{uuid.uuid4().hex[:6].upper()}") for _ in range(tight + 1)
        ]
        by_code = [lookup(client, _fresh_ip(), guessed) for _ in range(ceiling + 1)]

    assert by_address == [404] * tight + [429]
    assert by_code == [404] * ceiling + [429]


def _spellings(text: str, swaps: dict[str, str]) -> list[str]:
    """Every way to write `text` with each letter of `swaps` plain or swapped."""
    options = [(c, swaps[c]) if c in swaps else (c,) for c in text]
    return ["".join(parts) for parts in itertools.product(*options)]


@pytest.mark.asyncio
async def test_account_ceiling_holds_across_spellings_the_database_treats_as_one(
    committed_conn, tight
) -> None:
    """`citext` lowercases `İ` to `i`, so `İvan@` logs in to the account `ivan@`. A bucket
    keyed by the spelling gave each one its own ceiling: a guess spread over addresses could
    spread over spellings too and never meet it."""
    email = f"iiiii-{uuid.uuid4().hex[:8]}@example.com"
    ceiling = tight * SUBJECT_FACTOR
    same = [
        spelling
        for spelling in _spellings(email, {"i": "İ"})
        if await committed_conn.fetchval("SELECT $1::citext = $2::citext", spelling, email)
    ]
    if len(same) <= ceiling:
        pytest.skip("this database does not treat these spellings as one account")

    with TestClient(_ClientAddress(create_app())) as client:
        statuses = [_login(client, _fresh_ip(), spelling).status_code for spelling in same]
        elsewhere = _login(client, _fresh_ip(), _email()).status_code

    assert statuses[: ceiling + 1] == [401] * ceiling + [429]
    assert elsewhere == 401  # another account is not caught by it


@pytest.mark.asyncio
async def test_code_ceiling_holds_across_spellings_the_database_treats_as_one(
    committed_conn, tight
) -> None:
    """The lookup compares `UPPER(code)`, which turns `ſ` (long s) into `S`. The booking code
    alphabet has an S, so a code can be asked for under many spellings."""
    code = f"BK-SSSS{uuid.uuid4().hex[:12].upper()}"
    ceiling = tight * SUBJECT_FACTOR
    same = [
        spelling
        for spelling in _spellings(code, {"S": "ſ"})
        if await committed_conn.fetchval("SELECT upper($1::text) = $2::text", spelling, code)
    ]
    if len(same) <= ceiling:
        pytest.skip("this database does not treat these spellings as one code")

    def lookup(client: TestClient, spelling: str) -> int:
        return _post(
            client,
            "/v1/bookings/lookup",
            {"code": spelling, "email": "nobody@example.com"},
            _fresh_ip(),
        ).status_code

    with TestClient(_ClientAddress(create_app())) as client:
        statuses = [lookup(client, spelling) for spelling in same]

    assert statuses[: ceiling + 1] == [404] * ceiling + [429]


class _DownRedis:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def eval(self, script, numkeys, key, *args):
        self.keys.append(key)
        raise ConnectionError("redis is down")


class _Recorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def debug(self, event: str, **fields) -> None:
        self.records.append((event, fields))

    info = warning = debug


@pytest.mark.asyncio
async def test_the_limiter_keeps_the_email_and_the_code_out_of_redis_and_the_log(
    monkeypatch,
) -> None:
    """The bucket key reaches Redis and, when Redis is down, the limiter's warning."""
    redis, log = _DownRedis(), _Recorder()
    monkeypatch.setattr(ratelimit, "get_redis", lambda: redis)
    monkeypatch.setattr(ratelimit, "log", log)
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
    email, code = "guest.private@example.com", "BK-PRIVAT"

    await enforce(request, "login-account", 5, subject=email)
    await enforce(request, "login-account", 5, subject=email.upper())
    await enforce(request, "lookup-code", 5, subject=code)

    assert [event for event, _ in log.records].count("ratelimit-failed-open") == 3
    seen = " ".join(redis.keys) + repr(log.records)
    assert "private" not in seen.lower() and "privat" not in seen.lower()
    assert redis.keys[0] == redis.keys[1] != redis.keys[2]  # same account, same bucket
