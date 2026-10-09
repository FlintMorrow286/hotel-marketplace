"""Rate limits for the public endpoints that take guesses.

A login takes a password guess and a booking lookup takes a code guess, so both
are throttled per client address and, to slow a guess spread across many
addresses, per account or per code. Over the limit the caller gets 429 with a
Retry-After, the same answer the channel API gives (see app/utils/ratelimit.py:
fixed window in Redis, failing open when Redis is down).

Behind a reverse proxy the app has to see the real client address (uvicorn
`--proxy-headers` with `--forwarded-allow-ips`); otherwise every user shares the
proxy's bucket.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.config.settings import settings
from app.utils import ratelimit

# One account (or one code) is attacked from many addresses at once, so its
# bucket is roomier than a single address's, but still a hard ceiling.
SUBJECT_FACTOR = 3


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def enforce(request: Request, bucket: str, limit: int, *, subject: str | None = None) -> None:
    """Count one attempt; answer 429 once `limit` is passed within the window.

    Keyed by the client address, or by `subject` (an email, a booking code).
    """
    key = f"rl:{bucket}:{subject.lower() if subject else client_ip(request)}"
    allowed, retry_after = await ratelimit.acquire(key, limit, settings.rate_limit_window_sec)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, try again later",
            headers={"Retry-After": str(retry_after)},
        )
