"""Payments on a provider's page: find the results that never arrived.

A provider reports a payment with a notification, and the guest's browser asks when it comes
back. Both can be lost (the provider cannot reach the shop, the guest closes the tab), so on a
fixed cadence the core asks the provider about the payments that are still open. Under any other
payment mode a sweep does nothing.
"""

from __future__ import annotations

import asyncio

from app.modules.payment import service
from app.utils.logger import get_logger

log = get_logger(__name__)

RECONCILE_INTERVAL_SEC = 30


async def reconcile_loop() -> None:
    """Settle open payments on a fixed cadence until cancelled."""
    log.info("payment-reconcile-started", interval_sec=RECONCILE_INTERVAL_SEC)
    while True:
        # Sleep first: a payment younger than the sweep's minimum age is left to its notification.
        await asyncio.sleep(RECONCILE_INTERVAL_SEC)
        try:
            await service.reconcile_pending()
        except Exception as exc:
            log.error("payment-reconcile-failed", error=repr(exc))
