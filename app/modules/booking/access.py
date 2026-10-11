"""Booking access token: the credential for paying, cancelling or refunding one booking.

The booking id travels in URLs, access logs and browser history, so it cannot be
what authorises an action that moves money. The token is an HMAC of the id under
a key derived from JWT_SECRET. It is handed to whoever creates the hold, and to
whoever finds the booking by code and email; it is sent in the X-Booking-Token
header, never in a URL, and checked in constant time.

It is stateless, so a lookup returns the very token the hold did. Rotating
JWT_SECRET invalidates every token; a guest gets a fresh one by looking the
booking up again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config.settings import settings

HEADER = "X-Booking-Token"
_PURPOSE = b"booking-access-v1"


def _key() -> bytes:
    return hmac.new(settings.jwt_secret.encode(), _PURPOSE, hashlib.sha256).digest()


def issue(booking_id: str) -> str:
    mac = hmac.new(_key(), str(booking_id).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def verify(booking_id: str, token: str) -> bool:
    return hmac.compare_digest(issue(booking_id).encode(), token.encode())


async def require_token(booking_id: str, x_booking_token: Annotated[str, Header()]) -> None:
    """Route dependency: 404 unless the header carries this booking's token.

    404, not 403: a wrong token and an unknown id must look the same.
    """
    if not verify(booking_id, x_booking_token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="booking not found")
