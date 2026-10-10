"""Rate limits for the public endpoints that take guesses.

A login takes a password guess and a booking lookup takes a code guess, so both
are throttled per client address and, to slow a guess spread across many
addresses, per account or per code. Over the limit the caller gets 429 with a
Retry-After, the same answer the channel API gives (see app/utils/ratelimit.py:
fixed window in Redis, failing open when Redis is down).

Behind a reverse proxy the app has to see the real client address. uvicorn reads
X-Forwarded-For only from the peers in --forwarded-allow-ips (default 127.0.0.1,
::1), so a proxy on another host must be listed there or every user shares the
proxy's bucket; with '*' anyone can forge an address and skip the per-address
limit (the per-account and per-code ceilings still hold).

Those ceilings are shared by everyone who tries the account or code, so someone who
exhausts one also makes its owner wait out the window. That is the price of
bounding a guess spread over many addresses.
"""

from __future__ import annotations

import hashlib
import unicodedata

from fastapi import HTTPException, Request, status

from app.config.settings import settings
from app.utils import ratelimit

# One account (or one code) is attacked from many addresses at once, so its
# bucket is roomier than a single address's, but still a hard ceiling.
SUBJECT_FACTOR = 3


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _subject_key(subject: str) -> str:
    """The bucket key for an account email or a booking code.

    Postgres treats more strings as one account or code than `.lower()` does: `citext` turns
    `İ` into `i`, and `UPPER` turns `ı` and `ſ` into `I` and `S`. A key built from the raw
    text gave each spelling its own bucket, so a guess could spread over spellings and never
    meet the ceiling. They are folded together here; lookalikes sharing a bucket is harmless.

    It is hashed, so the email or code is not left in Redis, nor in the limiter's warning
    when Redis is down.
    """
    folded = unicodedata.normalize("NFKD", subject.casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c)).replace("ı", "i")
    return hashlib.sha256(plain.encode()).hexdigest()[:32]


async def enforce(request: Request, bucket: str, limit: int, *, subject: str | None = None) -> None:
    """Count one attempt; answer 429 once `limit` is passed within the window.

    Keyed by the client address, or by `subject` (an email, a booking code).
    """
    key = f"rl:{bucket}:{_subject_key(subject) if subject else client_ip(request)}"
    allowed, retry_after = await ratelimit.acquire(key, limit, settings.rate_limit_window_sec)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, try again later",
            headers={"Retry-After": str(retry_after)},
        )
