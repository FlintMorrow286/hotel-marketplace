"""Partners hand us URLs our servers then request: private addresses are refused.

A webhook or iCal URL aimed at the loopback, the cloud metadata address or an
internal host would make our servers call the platform's own network, and part of
the reply comes back to the partner in the delivery error. The suite itself runs
with the guard off (conftest.py); these tests turn it on.
"""

from __future__ import annotations

import functools
import socket
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from app.config.settings import settings
from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.auth.jwt import create_access_token
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.outbox import deliver
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate
from app.modules.sync import ical_import
from app.utils import netguard

PUBLIC_IP = "93.184.216.34"

# URLs a partner can type that no client can request. None of them may escape the guard
# as a bare ValueError: on the routes that was a 500; in delivery it escaped deliver()
# into the worker, which swallows it, so the event sat claimed with no attempt, no
# delivery row and no log line.
MALFORMED = [
    "http://example.com:99999/hook",  # port out of range
    "http://example.com:abc/hook",  # port is not a number
    "http://1.2.3.4:99999/hook",  # the same on an address literal
    "http://[::1/hook",  # unbalanced bracket
    "http://:80/hook",  # no host
    "http://" + "a" * 64 + ".example/hook",  # a label too long to look up
    "http://a..example/hook",  # an empty label
]

# URLs urlparse accepts but httpx's own parser refuses (a control character, over 64 KB).
# The guard let them through, so they were stored; on the iCal side the refusal escaped
# sync_subscription: a 500 on "sync now", and in the poller, which swallows what escapes,
# a row stuck at "pending" with nothing for the partner to see.
UNBUILDABLE = [
    "http://example.com/a\x01b",
    "http://example.com/?q=\x7f",
    "http://example.com/" + "a" * 70000,
]


@pytest.fixture(autouse=True)
def guard_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", False)


def _resolving(monkeypatch, table: dict[str, list[str]]) -> None:
    async def fake(host: str, port: int) -> list[str]:
        if host not in table:
            raise socket.gaierror("no such host")
        return table[host]

    monkeypatch.setattr(netguard, "_resolve", fake)


def _event() -> dict:
    return {"id": str(uuid.uuid4()), "event_type": "booking.confirmed"}


def _mock_http(monkeypatch, module, handler) -> None:
    """Make `module`'s httpx clients talk to `handler` instead of the network."""
    client = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, "AsyncClient", client)


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        PUBLIC_IP,
        "2001:4860:4860::8888",
        "64:ff9b::808:808",  # 8.8.8.8 behind a NAT64 gateway
        "::ffff:0:808:808",  # 8.8.8.8 behind an SIIT translator
    ],
)
def test_public_addresses_are_allowed(address: str) -> None:
    assert netguard.is_public(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4 loopback written as IPv6
        "::ffff:10.0.0.1",
        # Python calls these globally reachable; a translator forwards them to the IPv4
        # host in the low 32 bits, so the private IPv4 behind them must be refused.
        "64:ff9b::7f00:1",
        "64:ff9b::a00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::",
        "::ffff:0:a00:1",
        "::ffff:0:7f00:1",
        "::7f00:1",  # deprecated IPv4-compatible form
        "::a00:1",
        "fec0::1",  # deprecated site-local
    ],
)
def test_private_addresses_are_refused(address: str) -> None:
    assert not netguard.is_public(address)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/hook",
        "http://[64:ff9b::a00:1]/hook",  # 10.0.0.1 behind a NAT64 gateway
        "http://localhost:9/hook",
        "http://2130706433/hook",  # 127.0.0.1 as one integer
    ],
)
async def test_private_urls_are_refused(url: str) -> None:
    with pytest.raises(netguard.UnsafeUrl):
        await netguard.assert_public_url(url)


@pytest.mark.asyncio
async def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch) -> None:
    _resolving(
        monkeypatch, {"internal.example": ["10.0.0.5"], "mixed.example": [PUBLIC_IP, "10.0.0.5"]}
    )
    for url in ("https://internal.example/hook", "https://mixed.example/hook"):
        with pytest.raises(netguard.UnsafeUrl):
            await netguard.assert_public_url(url)


@pytest.mark.asyncio
async def test_public_and_not_yet_resolvable_urls_pass(monkeypatch) -> None:
    _resolving(monkeypatch, {"hooks.example": [PUBLIC_IP]})
    await netguard.assert_public_url("https://hooks.example/hook")
    await netguard.assert_public_url(f"http://{PUBLIC_IP}/hook")
    # A name with no address yet: nothing to reach; the request-time check runs again.
    await netguard.assert_public_url("https://not-registered-yet.example/hook")


@pytest.mark.asyncio
async def test_the_guard_can_be_switched_off_for_development(monkeypatch) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", True)
    await netguard.assert_public_url("http://127.0.0.1:9/hook")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", MALFORMED)
async def test_a_url_that_cannot_be_parsed_or_looked_up_is_invalid(url: str) -> None:
    with pytest.raises(netguard.InvalidUrl):
        await netguard.assert_public_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", UNBUILDABLE)
async def test_a_url_httpx_cannot_build_is_invalid(url: str, monkeypatch) -> None:
    with pytest.raises(netguard.InvalidUrl):
        await netguard.assert_public_url(url)
    # With the guard off for development the answer is the same: nothing can request it.
    monkeypatch.setattr(settings, "allow_private_targets", True)
    with pytest.raises(netguard.InvalidUrl):
        await netguard.assert_public_url(url)


def test_an_invalid_url_is_an_unsafe_url() -> None:
    """Delivery and sync catch UnsafeUrl; this is what keeps a bad URL from escaping them."""
    assert issubclass(netguard.InvalidUrl, netguard.UnsafeUrl)


# ---------------------------------------------------------------- webhook delivery


@pytest.mark.asyncio
async def test_delivery_to_a_private_address_never_connects(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not be requested: {request.url}")

    _mock_http(monkeypatch, deliver, handler)

    ok, status_code, error = await deliver.deliver(
        "http://127.0.0.1:9/hook", "secret-secret", _event()
    )

    assert (ok, status_code) == (False, None)
    assert "non-public" in (error or "")


@pytest.mark.asyncio
async def test_a_redirect_into_the_private_network_is_not_followed(monkeypatch) -> None:
    _resolving(monkeypatch, {"hooks.example": [PUBLIC_IP]})
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    _mock_http(monkeypatch, deliver, handler)

    ok, _, error = await deliver.deliver("https://hooks.example/hook", "secret-secret", _event())

    assert ok is False and "non-public" in (error or "")
    assert requested == ["https://hooks.example/hook"]


@pytest.mark.asyncio
async def test_delivery_to_a_public_address_still_works(monkeypatch) -> None:
    _mock_http(monkeypatch, deliver, lambda request: httpx.Response(204))

    ok, status_code, error = await deliver.deliver(
        f"https://{PUBLIC_IP}/hook", "secret-secret", _event()
    )

    assert (ok, status_code, error) == (True, 204, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", [*MALFORMED, *UNBUILDABLE, "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11/hook"]
)
async def test_delivery_to_a_malformed_url_fails_cleanly(url: str) -> None:
    """deliver() answers (ok, status, error) for any URL, httpx's own refusals included."""
    ok, status_code, error = await deliver.deliver(url, "secret-secret", _event())

    assert (ok, status_code) == (False, None)
    assert error


# ---------------------------------------------------------------- iCal import


@pytest.mark.asyncio
async def test_ical_fetch_refuses_private_addresses_and_redirects_into_them(monkeypatch) -> None:
    _resolving(monkeypatch, {"calendar.example": [PUBLIC_IP]})
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(301, headers={"Location": "http://10.0.0.5/internal.ics"})

    _mock_http(monkeypatch, ical_import, handler)

    with pytest.raises(netguard.UnsafeUrl):
        await ical_import._fetch("http://127.0.0.1:9/cal.ics")
    with pytest.raises(netguard.UnsafeUrl):
        await ical_import._fetch("https://calendar.example/cal.ics")
    assert requested == ["https://calendar.example/cal.ics"]


# ---------------------------------------------------------------- routes


@pytest.mark.asyncio
async def test_webhook_and_ical_urls_must_be_public(committed_conn, monkeypatch) -> None:
    _resolving(monkeypatch, {"internal.example": ["10.0.0.5"]})
    email = f"ssrf-{uuid.uuid4().hex[:8]}@example.com"
    partner_id = await auth_service.register_partner(
        committed_conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    headers = {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}

    def webhook(url: str):
        return client.post(
            "/v1/partner/webhooks",
            json={"url": url, "secret": "route-level-secret"},
            headers=headers,
        )

    with TestClient(create_app()) as client:
        refused = [
            webhook("http://127.0.0.1:8000/hook"),
            webhook("http://169.254.169.254/latest/meta-data/"),
            webhook("https://internal.example/hook"),
        ]
        accepted = webhook(f"https://{PUBLIC_IP}/hook")
        ical = client.put(
            f"/v1/partner/unit-types/{uuid.uuid4()}/ical-import",
            json={"url": "http://169.254.169.254/latest/meta-data/"},
            headers=headers,
        )

    assert [r.status_code for r in refused] == [422, 422, 422]
    assert all("public address" in r.text for r in refused)
    assert accepted.status_code == 201, accepted.text
    assert ical.status_code == 422 and "public address" in ical.text


@pytest.mark.asyncio
async def test_malformed_urls_are_422_not_500(committed_conn) -> None:
    email = f"bad-{uuid.uuid4().hex[:8]}@example.com"
    partner_id = await auth_service.register_partner(
        committed_conn, PartnerRegisterRequest(email=email, password="secret123", name="Tester")
    )
    headers = {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}
    urls = [*MALFORMED, *UNBUILDABLE]

    with TestClient(create_app()) as client:
        responses = [
            client.post(
                "/v1/partner/webhooks",
                json={"url": url, "secret": "route-level-secret"},
                headers=headers,
            )
            for url in urls
        ] + [
            client.put(
                f"/v1/partner/unit-types/{uuid.uuid4()}/ical-import",
                json={"url": url},
                headers=headers,
            )
            for url in urls
        ]

    assert [r.status_code for r in responses] == [422] * (2 * len(urls))
    # A mistyped port is not "a private address": the UI must not say so.
    assert not any("public address" in r.text for r in responses)


async def _unit_type(conn) -> tuple[str, str]:
    partner_id = await auth_service.register_partner(
        conn,
        PartnerRegisterRequest(
            email=f"ical-{uuid.uuid4().hex[:8]}@example.com", password="secret123", name="Tester"
        ),
    )
    prop = await property_service.create_property(
        conn, partner_id, PropertyCreate(name="Alpha", property_type="hotel", city="Yalta")
    )
    unit_id = await conn.fetchval(
        "INSERT INTO unit_type (property_id, name, capacity, total_units, base_price) "
        "VALUES ($1, 'Стандарт', 2, 1, 5000) RETURNING id::text",
        prop.id,
    )
    return partner_id, unit_id


@pytest.mark.asyncio
@pytest.mark.parametrize("url", UNBUILDABLE)
async def test_a_stored_ical_url_httpx_cannot_build_is_a_recorded_failure(
    committed_conn, url: str
) -> None:
    """A row stored before the save-time check: 'sync now' answered 500, and the poller left
    it at 'pending' with no error. Now the failure is recorded for the partner to read."""
    partner_id, unit_id = await _unit_type(committed_conn)
    await committed_conn.execute(
        "INSERT INTO ical_subscription (unit_type_id, url) VALUES ($1, $2)", unit_id, url
    )
    headers = {"Authorization": f"Bearer {create_access_token(partner_id, scope='partner')}"}

    with TestClient(create_app()) as client:
        response = client.post(
            f"/v1/partner/unit-types/{unit_id}/ical-import/sync", headers=headers
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "error"
    row = await committed_conn.fetchrow(
        "SELECT last_status, last_error FROM ical_subscription WHERE unit_type_id = $1", unit_id
    )
    assert row["last_status"] == "error" and row["last_error"]
