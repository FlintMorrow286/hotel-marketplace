from __future__ import annotations

import asyncio
import contextvars
import socket
import ssl
from collections.abc import Iterable

import httpcore
import httpx
import pytest
from httpcore._backends.base import SOCKET_OPTION, AsyncNetworkBackend, AsyncNetworkStream

from app.config.settings import settings
from app.utils import netguard

PUBLIC_IP = "93.184.216.34"
SECOND_PUBLIC_IP = "93.184.216.35"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"
PRIVATE_IP = "10.0.0.7"
PRIVATE_IPV6 = "fd00::7"


class _RecordingStream(AsyncNetworkStream):
    def __init__(self, response: bytes) -> None:
        self._incoming = bytearray(response)
        self.written = bytearray()
        self.server_hostnames: list[str | None] = []
        self.ssl_settings: list[tuple[ssl.VerifyMode, bool]] = []

    async def read(
        self,
        max_bytes: int,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> bytes:
        chunk = bytes(self._incoming[:max_bytes])
        del self._incoming[:max_bytes]
        return chunk

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None:
        self.written.extend(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> AsyncNetworkStream:
        self.server_hostnames.append(server_hostname)
        self.ssl_settings.append((ssl_context.verify_mode, ssl_context.check_hostname))
        return self

    def get_extra_info(self, info: str):
        return None


class _RecordingBackend(AsyncNetworkBackend):
    """Records every attempt; `down` refuses at once, `blackholed` burns its whole timeout."""

    def __init__(
        self,
        responses: list[bytes],
        *,
        down: Iterable[str] = (),
        blackholed: Iterable[str] = (),
    ) -> None:
        self.responses = iter(responses)
        self.down = set(down)
        self.blackholed = set(blackholed)
        self.connections: list[tuple[str, int]] = []
        self.timeouts: list[float | None] = []
        self.streams: list[_RecordingStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        self.connections.append((host, port))
        self.timeouts.append(timeout)
        if host in self.blackholed:
            await asyncio.sleep(timeout or 0)
            raise httpcore.ConnectTimeout(f"{host} did not answer")
        if host in self.down:
            raise httpcore.ConnectError(f"{host} refused the connection")
        stream = _RecordingStream(next(self.responses))
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        raise AssertionError("URL requests must use TCP")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _response(status: int = 204, headers: bytes = b"") -> bytes:
    return (
        f"HTTP/1.1 {status} Test\r\n".encode()
        + headers
        + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )


def _install_backend(monkeypatch, backend: _RecordingBackend) -> None:
    factory = netguard.pinned_transport
    monkeypatch.setattr(
        netguard,
        "pinned_transport",
        lambda: factory(network_backend=backend),
    )


def _answer(monkeypatch, *addresses: str) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    monkeypatch.setattr(netguard, "_resolve", resolve)


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "allow_private_targets", False)


@pytest.mark.asyncio
async def test_dns_answer_is_pinned_without_a_second_lookup(monkeypatch) -> None:
    lookups: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        lookups.append((host, port))
        return [PUBLIC_IP if len(lookups) == 1 else PRIVATE_IP]

    monkeypatch.setattr(netguard, "_resolve", resolve)
    backend = _RecordingBackend([_response()])
    _install_backend(monkeypatch, backend)

    async with netguard.stream_request(
        "GET", "https://hooks.example/hook", timeout=2, max_redirects=1
    ) as response:
        assert response.status_code == 204

    assert lookups == [("hooks.example", 443)]
    assert backend.connections == [(PUBLIC_IP, 443)]
    stream = backend.streams[0]
    assert stream.server_hostnames == ["hooks.example"]
    assert stream.ssl_settings == [(ssl.CERT_REQUIRED, True)]
    assert b"host: hooks.example\r\n" in bytes(stream.written).lower()


@pytest.mark.asyncio
async def test_private_dns_answer_is_refused_before_connecting(monkeypatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return [PRIVATE_IP]

    monkeypatch.setattr(netguard, "_resolve", resolve)
    backend = _RecordingBackend([_response()])
    _install_backend(monkeypatch, backend)

    with pytest.raises(netguard.UnsafeUrl):
        async with netguard.stream_request(
            "GET", "https://hooks.example/hook", timeout=2, max_redirects=1
        ):
            pass

    assert backend.connections == []


@pytest.mark.asyncio
async def test_dns_lookup_error_fails_closed(monkeypatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        raise socket.gaierror("temporary lookup failure")

    monkeypatch.setattr(netguard, "_resolve", resolve)
    backend = _RecordingBackend([_response()])
    _install_backend(monkeypatch, backend)

    with pytest.raises(netguard.InvalidUrl):
        async with netguard.stream_request(
            "GET", "https://hooks.example/hook", timeout=2, max_redirects=1
        ):
            pass

    assert backend.connections == []


@pytest.mark.asyncio
async def test_redirect_is_resolved_and_pinned_again(monkeypatch) -> None:
    lookups: list[tuple[str, int]] = []
    answers = {"hooks.example": PUBLIC_IP, "redirect.example": "1.1.1.1"}

    async def resolve(host: str, port: int) -> list[str]:
        lookups.append((host, port))
        return [answers[host]]

    monkeypatch.setattr(netguard, "_resolve", resolve)
    backend = _RecordingBackend(
        [
            _response(
                302,
                b"Location: https://redirect.example/next\r\n",
            ),
            _response(),
        ]
    )
    _install_backend(monkeypatch, backend)

    async with netguard.stream_request(
        "GET",
        "https://hooks.example/hook",
        timeout=2,
        max_redirects=1,
        redirect_policy="any-origin",
    ) as response:
        assert response.status_code == 204

    assert lookups == [("hooks.example", 443), ("redirect.example", 443)]
    assert backend.connections == [(PUBLIC_IP, 443), ("1.1.1.1", 443)]
    assert [stream.server_hostnames for stream in backend.streams] == [
        ["hooks.example"],
        ["redirect.example"],
    ]


@pytest.mark.asyncio
async def test_idn_host_connects_to_its_pinned_address_with_ascii_host(monkeypatch) -> None:
    lookups: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        lookups.append((host, port))
        return [PUBLIC_IP]

    monkeypatch.setattr(netguard, "_resolve", resolve)
    backend = _RecordingBackend([_response()])
    _install_backend(monkeypatch, backend)

    async with netguard.stream_request(
        "GET", "https://BÜCHER.example/hook", timeout=2, max_redirects=0
    ) as response:
        assert response.status_code == 204

    assert lookups == [("bücher.example", 443)]
    assert backend.connections == [(PUBLIC_IP, 443)]
    assert backend.streams[0].server_hostnames == ["xn--bcher-kva.example"]
    assert b"host: xn--bcher-kva.example\r\n" in bytes(backend.streams[0].written).lower()


@pytest.mark.asyncio
async def test_getaddrinfo_receives_unicode_hostname(monkeypatch) -> None:
    calls: list[tuple[str, int, int]] = []

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        calls.append((host, port, type))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    assert await netguard._resolve("bücher.example", 443) == [PUBLIC_IP]
    assert calls == [("bücher.example", 443, socket.SOCK_STREAM)]


@pytest.mark.asyncio
async def test_trailing_dot_hostname_remains_significant(monkeypatch) -> None:
    """Trailing-dot FQDNs remain distinct and are matched with their trailing dot."""
    lookups: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        lookups.append((host, port))
        return [PUBLIC_IP]

    monkeypatch.setattr(netguard, "_resolve", resolve)
    backend = _RecordingBackend([_response()])
    _install_backend(monkeypatch, backend)

    async with netguard.stream_request(
        "GET", "https://Example.COM./hook", timeout=2, max_redirects=0
    ) as response:
        assert response.status_code == 204

    assert lookups == [("example.com.", 443)]
    assert backend.connections == [(PUBLIC_IP, 443)]
    assert backend.streams[0].server_hostnames == ["example.com."]
    assert b"host: example.com.\r\n" in bytes(backend.streams[0].written).lower()


@pytest.mark.asyncio
async def test_pinned_backend_refuses_a_different_host() -> None:
    target: contextvars.ContextVar[netguard._ResolvedTarget | None] = contextvars.ContextVar(
        "pinned-target", default=None
    )
    target.set(netguard._ResolvedTarget("hooks.example", 443, (PUBLIC_IP,)))
    backend = _RecordingBackend([_response()])
    guarded = netguard._PinnedNetworkBackend(target, backend)

    with pytest.raises(httpcore.ConnectError, match="no validated address"):
        await guarded.connect_tcp("other.example", 443)

    assert backend.connections == []


# ---------------------------------------------------------------- fallback


@pytest.mark.asyncio
async def test_next_validated_address_is_tried_when_the_first_is_down(monkeypatch) -> None:
    _answer(monkeypatch, SECOND_PUBLIC_IP, PUBLIC_IP)
    backend = _RecordingBackend([_response()], down=[PUBLIC_IP])
    _install_backend(monkeypatch, backend)

    async with netguard.stream_request(
        "GET", "https://hooks.example/hook", timeout=2, max_redirects=0
    ) as response:
        assert response.status_code == 204

    assert backend.connections == [(PUBLIC_IP, 443), (SECOND_PUBLIC_IP, 443)]
    assert backend.streams[0].server_hostnames == ["hooks.example"]


@pytest.mark.asyncio
async def test_ipv6_is_the_fallback_of_a_dual_stack_host(monkeypatch) -> None:
    _answer(monkeypatch, PUBLIC_IPV6, PUBLIC_IP)
    backend = _RecordingBackend([_response()], down=[PUBLIC_IP])
    _install_backend(monkeypatch, backend)

    async with netguard.stream_request(
        "GET", "https://hooks.example/hook", timeout=2, max_redirects=0
    ) as response:
        assert response.status_code == 204

    assert backend.connections == [(PUBLIC_IP, 443), (PUBLIC_IPV6, 443)]


@pytest.mark.asyncio
async def test_connect_error_surfaces_after_the_last_attempt(monkeypatch) -> None:
    addresses = [f"93.184.216.{n}" for n in range(30, 36)]
    _answer(monkeypatch, *addresses)
    backend = _RecordingBackend([], down=addresses)
    _install_backend(monkeypatch, backend)

    with pytest.raises(httpx.ConnectError, match="refused"):
        async with netguard.stream_request(
            "GET", "https://hooks.example/hook", timeout=2, max_redirects=0
        ):
            pass

    assert len(addresses) > netguard._MAX_CONNECT_ATTEMPTS
    assert backend.connections == [
        (address, 443) for address in addresses[: netguard._MAX_CONNECT_ATTEMPTS]
    ]


@pytest.mark.asyncio
async def test_a_private_address_in_the_answer_refuses_all_of_it(monkeypatch) -> None:
    """The fallback must not be a way to reach the private half of a mixed answer."""
    # IPv4 sorts first, so the private address is the one a fallback would reach last.
    _answer(monkeypatch, PUBLIC_IP, PRIVATE_IPV6)
    backend = _RecordingBackend([_response()], down=[PUBLIC_IP])
    _install_backend(monkeypatch, backend)

    with pytest.raises(netguard.UnsafeUrl):
        async with netguard.stream_request(
            "GET", "https://hooks.example/hook", timeout=2, max_redirects=0
        ):
            pass

    assert backend.connections == []


@pytest.mark.asyncio
async def test_attempts_share_one_connect_timeout(monkeypatch) -> None:
    _answer(monkeypatch, PUBLIC_IP, SECOND_PUBLIC_IP)
    backend = _RecordingBackend([], blackholed=[PUBLIC_IP, SECOND_PUBLIC_IP])
    _install_backend(monkeypatch, backend)

    with pytest.raises(httpx.ConnectTimeout):
        async with netguard.stream_request(
            "GET", "https://hooks.example/hook", timeout=0.4, max_redirects=0
        ):
            pass

    assert len(backend.timeouts) == 2
    assert backend.timeouts[0] == pytest.approx(0.2, abs=0.05)
    # Each blackholed attempt burned its whole share, so the shares add up to the timeout.
    assert sum(backend.timeouts) <= 0.4


@pytest.mark.asyncio
async def test_real_sockets_fall_back_to_the_address_that_answers(monkeypatch) -> None:
    """The real backend's refusal is what the fallback has to catch, not just the fake's."""
    monkeypatch.setattr(settings, "allow_private_targets", True)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(_response())
        await writer.drain()
        writer.close()

    try:
        server = await asyncio.start_server(serve, "127.0.0.2", 0)
    except OSError:
        pytest.skip("this host has no 127.0.0.2 loopback address")
    port = server.sockets[0].getsockname()[1]
    # Nothing listens on 127.0.0.1:port, so the first attempt is refused for real.
    _answer(monkeypatch, "127.0.0.1", "127.0.0.2")

    async with server:
        async with netguard.stream_request(
            "GET", f"http://hooks.example:{port}/hook", timeout=2, max_redirects=0
        ) as response:
            assert response.status_code == 204


def test_redacted_url_drops_userinfo_query_and_fragment() -> None:
    result = netguard.redact_url(
        "https://user:secret@hooks.example:8443/path?token=secret#fragment"
    )
    assert result == "https://hooks.example:8443/path"
    assert "secret" not in result
