"""Payment providers.

Phase 1 runs on a stub. The booking core never talks to a bank: it only calls
`PaymentProvider.pay()`, and the concrete implementation decides what actually
happens. `PAYMENT_MODE` selects which implementation is wired up.

Modes:
  stub          — instant success (default; for developing the core)
  delayed_stub  — success after PAYMENT_STUB_DELAY_SEC (hold-TTL testing)
  fail          — always fails (error-path testing)
  tinkoff       — real acquiring (not implemented in Phase 1)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from app.config.payment import PaymentMode
from app.config.settings import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

PaymentStatus = Literal["pending", "succeeded", "failed", "refunded"]


class PaymentResult:
    def __init__(self, status: PaymentStatus, external_id: str | None = None) -> None:
        self.status = status
        self.external_id = external_id


class PaymentProvider(ABC):
    """Interface every payment implementation must satisfy."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        """Charge the guest. Must be idempotent on booking_id."""
        ...

    @abstractmethod
    async def refund(self, booking_id: str, amount: float) -> PaymentResult: ...


class StubProvider(PaymentProvider):
    """Instant success. Used during core development."""

    @property
    def name(self) -> str:
        return "stub"

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        log.info("stub-payment-success", booking_id=booking_id, amount=amount)
        return PaymentResult("succeeded", external_id=f"stub-{booking_id[:8]}")

    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        log.info("stub-refund-success", booking_id=booking_id, amount=amount)
        return PaymentResult("refunded", external_id=f"stub-refund-{booking_id[:8]}")


class FailProvider(PaymentProvider):
    """Always fails — for testing the failure path of the booking core."""

    @property
    def name(self) -> str:
        return "fail"

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        log.info("fail-payment-by-design", booking_id=booking_id)
        return PaymentResult("failed")

    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        return PaymentResult("failed")


def get_payment_provider() -> PaymentProvider:
    """Pick the provider from PAYMENT_MODE.

    A mode with no implementation (tinkoff, a typo) raises: falling back to the
    stub would confirm bookings without charging anyone.
    """
    mode = settings.payment_mode
    if mode == PaymentMode.FAIL:
        return FailProvider()
    if mode in {PaymentMode.STUB, PaymentMode.DELAYED_STUB}:
        return StubProvider()
    raise ValueError(f"PAYMENT_MODE={mode!r} has no implementation yet")
