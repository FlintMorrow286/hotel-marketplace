"""Public facts the pages need to be honest with the guest."""

from __future__ import annotations

from fastapi import APIRouter

from app.config.payment import NO_MONEY_MODES
from app.config.settings import settings

router = APIRouter(prefix="/v1", tags=["site"])


@router.get("/config")
async def public_config() -> dict:
    """What the pages may promise.

    `payment_test_mode`: nobody is really charged, so the checkout says so.
    `email_enabled`: letters are really delivered; otherwise the pages must not promise one.
    """
    return {
        "payment_test_mode": settings.payment_mode in NO_MONEY_MODES,
        "email_enabled": settings.mail_backend == "smtp",
    }
