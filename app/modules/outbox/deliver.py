"""Deliver an outbox event to one webhook subscription.

Signed with HMAC-SHA256 over the raw body so the receiver can prove the
message is ours. Events carry a stable id and Idempotency-Key header:
receivers may see an event twice (retry), so they must dedupe on the id.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from app.utils.logger import get_logger
from app.utils.netguard import GUARD_HOOKS, UnsafeUrl

log = get_logger(__name__)

DELIVERY_TIMEOUT_SEC = 10
MAX_REDIRECTS = 2
USER_AGENT = "hotel-marketplace-webhook/1.0"


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 hex digest, sent as X-Signature."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(url: str, secret: str, event: dict) -> tuple[bool, int | None, str | None]:
    """POST the event. Returns (ok, status_code, error).

    ok is True only for a 2xx. A 4xx is the receiver's problem and still counts
    as a delivery attempt — the error text goes into the log so the partner can
    see why their hook is rejected.
    """
    body = json.dumps(event, default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Signature": sign(body, secret),
        "Idempotency-Key": event["id"],
        "X-Event-Type": event["event_type"],
    }

    try:
        async with httpx.AsyncClient(
            timeout=DELIVERY_TIMEOUT_SEC,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            event_hooks=GUARD_HOOKS,
        ) as client:
            response = await client.post(url, content=body, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, UnsafeUrl) as exc:
        return False, None, str(exc)

    if 200 <= response.status_code < 300:
        return True, response.status_code, None

    detail = response.text[:200] if response.text else ""
    return False, response.status_code, f"HTTP {response.status_code} {detail}".strip()
