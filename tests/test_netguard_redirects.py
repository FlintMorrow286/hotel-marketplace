from __future__ import annotations

import json

import httpx
import pytest

from app.modules.outbox import deliver
from app.modules.sync import ical_import
from app.utils import netguard


def _mock_http(monkeypatch, handler) -> None:
    monkeypatch.setattr(netguard, "pinned_transport", lambda: httpx.MockTransport(handler))


def _event() -> dict[str, str]:
    return {"id": "event-1", "event_type": "booking.confirmed"}


@pytest.mark.asyncio
async def test_webhook_refuses_https_to_http_downgrade(monkeypatch) -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(307, headers={"Location": "http://hooks.example/receive"})

    _mock_http(monkeypatch, handler)

    ok, status_code, error = await deliver.deliver(
        "https://hooks.example/hook", "webhook-secret", _event()
    )

    assert (ok, status_code) == (False, None)
    assert "HTTPS-to-HTTP downgrade" in (error or "")
    assert [str(request.url) for request in requested] == ["https://hooks.example/hook"]


@pytest.mark.asyncio
async def test_ical_refuses_https_to_http_downgrade(monkeypatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://calendar.example/feed.ics"})

    _mock_http(monkeypatch, handler)

    with pytest.raises(netguard.UnsafeUrl, match="HTTPS-to-HTTP downgrade"):
        await ical_import._fetch("https://calendar.example/feed.ics")

    assert requested == ["https://calendar.example/feed.ics"]


@pytest.mark.asyncio
async def test_webhook_307_cannot_send_signature_or_body_to_another_origin(monkeypatch) -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example/receive"})

    _mock_http(monkeypatch, handler)
    event = _event()
    secret = "webhook-secret"

    ok, status_code, error = await deliver.deliver("https://hooks.example/hook", secret, event)

    expected_body = json.dumps(event, default=str).encode()
    assert (ok, status_code) == (False, None)
    assert "same host and port" in (error or "")
    assert len(requested) == 1
    assert str(requested[0].url) == "https://hooks.example/hook"
    assert requested[0].content == expected_body
    assert requested[0].headers["X-Signature"] == deliver.sign(expected_body, secret)


@pytest.mark.asyncio
async def test_webhook_follows_same_host_http_to_https_default_port_upgrade(monkeypatch) -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        if len(requested) == 1:
            return httpx.Response(307, headers={"Location": "https://hooks.example/hook"})
        return httpx.Response(204)

    _mock_http(monkeypatch, handler)
    event = _event()
    secret = "webhook-secret"

    assert await deliver.deliver("http://hooks.example/hook", secret, event) == (
        True,
        204,
        None,
    )

    expected_body = json.dumps(event, default=str).encode()
    assert [str(request.url) for request in requested] == [
        "http://hooks.example/hook",
        "https://hooks.example/hook",
    ]
    assert [request.method for request in requested] == ["POST", "POST"]
    assert all(request.content == expected_body for request in requested)
    assert all(
        request.headers["X-Signature"] == deliver.sign(expected_body, secret)
        for request in requested
    )


@pytest.mark.asyncio
async def test_ical_follows_cross_host_https_redirect(monkeypatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if len(requested) == 1:
            return httpx.Response(
                302, headers={"Location": "https://www.calendar.example/feed.ics"}
            )
        return httpx.Response(200, content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR")

    _mock_http(monkeypatch, handler)

    assert await ical_import._fetch("https://calendar.example/feed.ics") == (
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR"
    )
    assert requested == [
        "https://calendar.example/feed.ics",
        "https://www.calendar.example/feed.ics",
    ]


@pytest.mark.asyncio
async def test_redirect_loop_still_obeys_max_redirects(monkeypatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/loop"})

    _mock_http(monkeypatch, handler)

    with pytest.raises(httpx.TooManyRedirects):
        async with netguard.stream_request(
            "GET", "https://loop.example/loop", timeout=2, max_redirects=1
        ):
            pass

    assert requested == ["https://loop.example/loop"] * 2
