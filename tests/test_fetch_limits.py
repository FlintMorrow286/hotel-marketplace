from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from app.modules.outbox import deliver
from app.modules.sync import ical_import
from app.utils import netguard


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _mock_http(monkeypatch, handler) -> None:
    monkeypatch.setattr(netguard, "pinned_transport", lambda: httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_ical_limit_counts_decoded_body_bytes(monkeypatch) -> None:
    compressed = gzip.compress(b"a" * 100)
    assert len(compressed) < 32
    stream = _Chunks([compressed])
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
        ),
    )
    monkeypatch.setattr(ical_import, "FETCH_MAX_BYTES", 32)

    with pytest.raises(ical_import.FetchLimitError, match="decoded bytes"):
        await ical_import._fetch("https://calendar.example/feed.ics")


@pytest.mark.asyncio
async def test_ical_accepts_a_normal_stream(monkeypatch) -> None:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR"),
    )

    assert await ical_import._fetch("https://calendar.example/feed.ics") == (
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR"
    )


@pytest.mark.asyncio
async def test_ical_total_deadline_rejects_a_trickling_body(monkeypatch) -> None:
    stream = _Chunks([b"one", b"two"], delay=0.03)
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, stream=stream),
    )
    monkeypatch.setattr(ical_import, "FETCH_TIMEOUT_SEC", 1)
    monkeypatch.setattr(ical_import, "FETCH_DEADLINE_SEC", 0.01)

    with pytest.raises(httpx.TimeoutException, match="deadline"):
        await ical_import._fetch("https://calendar.example/feed.ics")


@pytest.mark.asyncio
async def test_webhook_reads_at_most_the_error_excerpt_limit(monkeypatch) -> None:
    stream = _Chunks([b"12", b"34", b"56"])
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(500, stream=stream),
    )
    monkeypatch.setattr(deliver, "ERROR_BODY_LIMIT_BYTES", 4)

    ok, status_code, error = await deliver.deliver(
        "https://hooks.example/hook",
        "signing-secret",
        {"id": "event-1", "event_type": "booking.confirmed"},
    )

    assert (ok, status_code) == (False, 500)
    assert error == "HTTP 500 1234"
    assert stream.bytes_yielded == 4


@pytest.mark.asyncio
async def test_webhook_total_deadline_rejects_a_trickling_body(monkeypatch) -> None:
    stream = _Chunks([b"one", b"two"], delay=0.03)
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(500, stream=stream),
    )
    monkeypatch.setattr(deliver, "DELIVERY_TIMEOUT_SEC", 1)
    monkeypatch.setattr(deliver, "DELIVERY_DEADLINE_SEC", 0.01)

    ok, status_code, error = await deliver.deliver(
        "https://hooks.example/hook",
        "signing-secret",
        {"id": "event-1", "event_type": "booking.confirmed"},
    )

    assert (ok, status_code) == (False, None)
    assert "deadline" in (error or "")


@pytest.mark.asyncio
async def test_webhook_accepts_a_normal_response(monkeypatch) -> None:
    _mock_http(monkeypatch, lambda request: httpx.Response(204))

    assert await deliver.deliver(
        "https://hooks.example/hook",
        "signing-secret",
        {"id": "event-1", "event_type": "booking.confirmed"},
    ) == (True, 204, None)
