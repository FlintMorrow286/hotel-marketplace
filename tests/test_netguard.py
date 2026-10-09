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
from app.modules.sync import ical_import
from app.utils import netguard

PUBLIC_IP = "93.184.216.34"


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
    ["8.8.8.8", PUBLIC_IP, "2001:4860:4860::8888"],
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
