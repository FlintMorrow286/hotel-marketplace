"""Pin partner-supplied HTTP targets to the addresses validated before connecting."""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import NamedTuple
from urllib.parse import urlsplit

import httpcore
import httpx
from fastapi import HTTPException, status
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION, AsyncNetworkBackend, AsyncNetworkStream

from app.config.settings import settings


class UnsafeUrl(ValueError):
    """The URL points at (or resolves to) a non-public address."""


class InvalidUrl(UnsafeUrl):
    """The URL cannot be parsed or its host cannot be resolved."""


class _ResolvedTarget(NamedTuple):
    host: str
    port: int
    address: str


_TRANSLATOR_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
)
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in prefix for prefix in _TRANSLATOR_PREFIXES):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _IPV4_COMPATIBLE or ip.is_site_local:
            return False
    return ip.is_global and not ip.is_multicast


def _parse_url(url: str | httpx.URL) -> httpx.URL:
    try:
        parsed = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise InvalidUrl(f"url is not valid: {exc}") from exc
    try:
        _ = urlsplit(str(parsed)).port
    except ValueError as exc:
        raise InvalidUrl(f"url is not valid: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise InvalidUrl("url must use http or https")
    if not parsed.host:
        raise InvalidUrl("url is not valid: it has no host")
    return parsed


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _normalize_address(address: str) -> str:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).compressed
    except ValueError as exc:
        raise InvalidUrl("url hostname resolved to an invalid address") from exc


async def _resolve_target(url: str | httpx.URL) -> _ResolvedTarget:
    parsed = _parse_url(url)
    host = parsed.host
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = await _resolve(host, port)
        except (OSError, UnicodeError, ValueError) as exc:
            raise InvalidUrl("url hostname could not be resolved") from exc
    else:
        addresses = [str(address)]

    normalized = [_normalize_address(address) for address in addresses]
    if not normalized:
        raise InvalidUrl("url hostname did not resolve to any address")
    # Pin one address; IPv4 first, so a dual-stack host does not land on a family
    # this machine cannot route.
    normalized.sort(key=lambda address: (":" in address, address))
    if not settings.allow_private_targets and any(not is_public(address) for address in normalized):
        raise UnsafeUrl(f"{host} points at a non-public address")
    return _ResolvedTarget(host, port, normalized[0])


async def assert_public_url(url: str) -> None:
    """Raise UnsafeUrl unless the URL resolves only to public addresses."""
    parsed = _parse_url(url)
    if settings.allow_private_targets:
        return
    await _resolve_target(parsed)


async def require_public_url(url: str) -> None:
    """assert_public_url for a route: answers 422 instead of raising."""
    try:
        await assert_public_url(url)
    except InvalidUrl as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except UnsafeUrl as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"url must point to a public address: {exc}",
        ) from exc


class _PinnedNetworkBackend(AsyncNetworkBackend):
    def __init__(
        self,
        target: contextvars.ContextVar[_ResolvedTarget | None],
        backend: AsyncNetworkBackend | None = None,
    ) -> None:
        self._target = target
        self._backend = backend if backend is not None else AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        target = self._target.get()
        if target is None or host.casefold() != target.host.casefold() or port != target.port:
            raise httpcore.ConnectError("connection target has no validated address")
        return await self._backend.connect_tcp(
            target.address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, network_backend: AsyncNetworkBackend | None = None) -> None:
        super().__init__(trust_env=False)
        self._target = contextvars.ContextVar(f"pinned-target-{id(self)}", default=None)
        # Replacing only the TCP backend keeps HTTPX's original-host TLS and HTTP logic.
        self._pool._network_backend = _PinnedNetworkBackend(self._target, network_backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = await _resolve_target(request.url)
        token = self._target.set(target)
        try:
            return await super().handle_async_request(request)
        finally:
            self._target.reset(token)


def pinned_transport(
    *, network_backend: AsyncNetworkBackend | None = None
) -> httpx.AsyncHTTPTransport:
    return _PinnedHTTPTransport(network_backend=network_backend)


@asynccontextmanager
async def stream_request(
    method: str,
    url: str,
    *,
    timeout: float,  # noqa: ASYNC109
    max_redirects: int,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> AsyncIterator[httpx.Response]:
    """Stream one request, validating and pinning every redirect without buffering it."""
    async with httpx.AsyncClient(
        transport=pinned_transport(),
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        request = client.build_request(method, url, headers=headers, content=content)
        redirects = 0
        while True:
            response = await client.send(request, stream=True, follow_redirects=False)
            next_request = response.next_request
            if next_request is None:
                break
            if redirects >= max_redirects:
                await response.aclose()
                raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)
            await response.aclose()
            request = next_request
            redirects += 1

        try:
            yield response
        finally:
            await response.aclose()


def redact_url(url: str) -> str:
    """Return scheme, host, optional port and path without credentials or query data."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https"} or not host:
        return "<invalid-url>"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    path = parsed.path or "/"
    path = "".join(
        f"%{ord(char):02X}" if ord(char) < 32 or ord(char) == 127 else char for char in path
    )
    return f"{parsed.scheme}://{authority}{path}"
