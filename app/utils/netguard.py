"""Refuse to call the platform's own network on a partner's behalf.

Partners hand us URLs (webhook receivers, calendar feeds) that our servers then
request. Left unchecked, a partner can aim one at the loopback, the cloud
metadata address (169.254.169.254) or an internal host, and part of the reply
comes back to them in the delivery or sync error. So a URL must resolve to
public addresses only.

It is checked when the URL is stored and again on every request, redirects
included (httpx runs request hooks per hop), because DNS can change after a
URL was accepted. A name that does not resolve yet is let through: there is
nothing to reach, and the request-time check covers it later. The window
between this lookup and the connection (DNS rebinding) is not closed here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status

from app.config.settings import settings


class UnsafeUrl(ValueError):
    """The URL points at (or resolves to) a non-public address."""


class InvalidUrl(UnsafeUrl):
    """The URL cannot be parsed or looked up, so it cannot be shown to be public.

    An UnsafeUrl on purpose: delivery and sync already catch that, so a malformed
    stored URL is a failed delivery (recorded, retried), not a silent stall.
    """


# IPv6 prefixes whose low 32 bits are an IPv4 address: a translator (NAT64, SIIT) forwards
# the request to that IPv4 host, so that is the address that has to be public. Python
# calls the NAT64 prefix globally reachable, whatever sits behind it.
_TRANSLATOR_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
)
# Deprecated forms that were never public: `::a.b.c.d` and site-local `fec0::/10`.
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])  # drop an IPv6 scope id
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in prefix for prefix in _TRANSLATOR_PREFIXES):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _IPV4_COMPATIBLE or ip.is_site_local:
            return False
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def assert_public_url(url: str) -> None:
    """Raise UnsafeUrl unless every address the URL's host resolves to is public.

    A URL that cannot be parsed (bad port, unbalanced bracket), that httpx refuses to
    build (a control character, more than 64 KB) or whose host name cannot be encoded
    for a lookup (empty or over-long label) raises InvalidUrl: nothing can request it.
    """
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
        httpx.URL(url)  # urlparse accepts what httpx's own parser then refuses
    except (ValueError, httpx.InvalidURL) as exc:
        raise InvalidUrl(f"url is not valid: {exc}") from exc
    if not host:
        raise InvalidUrl("url is not valid: it has no host")
    if settings.allow_private_targets:
        return
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await _resolve(host, port or (443 if parsed.scheme == "https" else 80))
        except OSError:
            return  # does not resolve (yet); the request-time check runs again
        except ValueError as exc:  # UnicodeError: an empty or over-long label
            raise InvalidUrl(f"url is not valid: {exc}") from exc
    if any(not is_public(a) for a in addresses):
        raise UnsafeUrl(f"{host} points at a non-public address")


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


async def _guard_request(request: httpx.Request) -> None:
    await assert_public_url(str(request.url))


# Pass as `event_hooks=` to an httpx client: runs for the first request and for
# every redirect it follows.
GUARD_HOOKS = {"request": [_guard_request]}
