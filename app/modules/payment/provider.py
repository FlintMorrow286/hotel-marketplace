"""Payment providers.

The booking core never talks to a bank: it only calls a `PaymentProvider`, and the concrete
implementation decides what actually happens. `PAYMENT_MODE` selects which one is wired up.

Modes:
  stub           — instant success (default; for developing the core)
  delayed_stub   — success after PAYMENT_STUB_DELAY_SEC (hold-TTL testing)
  fail           — always fails (error-path testing)
  mock_acquiring — a stand-in for an acquirer: session, payment page, notification
                   (app/modules/payment/mock_acquirer.py; for working without keys)
  tinkoff        — real acquiring (not implemented yet)

Two kinds of provider:
  PaymentProvider        answers `pay()` at once; the core charges and confirms in one call.
  HostedPaymentProvider  an acquirer: the guest pays on the provider's page and the answer
                         comes later. This is what Tinkoff or YooKassa will implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.config.payment import PaymentMode
from app.config.settings import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

PaymentStatus = Literal["pending", "succeeded", "failed", "refunded"]


class PaymentResult:
    def __init__(
        self,
        status: PaymentStatus,
        external_id: str | None = None,
        amount: float | None = None,
    ) -> None:
        self.status = status
        self.external_id = external_id
        # What the provider says it paid back, when it says. A repeated refund is answered with
        # the first one, and the core records that amount, not the one it asked for this time.
        self.amount = amount


class PaymentProvider(ABC):
    """Interface every payment implementation must satisfy."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        """Charge the guest. Must be idempotent on booking_id.

        A booking whose charge was returned is closed (see payment.service), so a new
        charge always arrives under a new booking_id.
        """
        ...

    @abstractmethod
    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        """Return a charge. Must be idempotent on booking_id.

        The booking core writes the provider's answer down and does not repeat the call,
        but a crash between the answer and that record can still repeat it; the provider
        must then answer the first refund again instead of paying back twice.
        """
        ...


@dataclass(frozen=True)
class PaymentSession:
    """A payment the guest has not made yet: where the provider wants them to go."""

    external_id: str
    payment_url: str


@dataclass(frozen=True)
class ProviderPayment:
    """What the provider says about one payment right now."""

    external_id: str
    status: Literal["pending", "succeeded", "failed"]
    amount: float


class NotificationError(Exception):
    """A notification that is not genuine or cannot be read."""


class UnknownPayment(Exception):
    """The provider has no payment with this id."""


class HostedPaymentProvider(PaymentProvider):
    """An acquirer: the guest pays on the provider's own page.

    The core opens a session with `create_session` and sends the guest to its address. The
    result then reaches it two ways that must agree: the provider's notification, and the
    provider's own answer to `fetch`. A notification only says *which* payment to look at
    (`read_notification`); the status the core acts on is always what `fetch` returns, so a
    forged or replayed notification cannot confirm a booking.

    To connect a real acquirer: implement the four abstract methods, add the provider's name
    to PaymentMode and payment_provider_check, and teach `get_payment_provider` about it.
    """

    @abstractmethod
    async def create_session(
        self,
        booking_id: str,
        amount: float,
        *,
        return_url: str,
        description: str,
        ttl_sec: int,
    ) -> PaymentSession:
        """Open a payment for the booking and say where the guest pays it.

        `return_url` is where the provider sends the guest back; `ttl_sec` is how long the
        session may stay open (the hold's lifetime).
        """
        ...

    @abstractmethod
    async def fetch(self, external_id: str) -> ProviderPayment:
        """The provider's current answer for a payment. Raises UnknownPayment."""
        ...

    @abstractmethod
    def read_notification(self, body: bytes, headers: Mapping[str, str]) -> str:
        """Check that a notification is genuine; return the id of the payment it is about.

        Raises NotificationError. Everything the core does about the payment starts from
        `fetch`, not from the notification's own fields.
        """
        ...

    @abstractmethod
    async def refund_charge(
        self, booking_id: str, amount: float, charge_id: str | None
    ) -> PaymentResult:
        """Return a charge (all of it or a part). Must be idempotent on `charge_id`."""
        ...

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        # The guest's checkout never calls this: it opens a session. A provider may still answer it
        # (the mock pays at once, for scripts with no browser); a real acquirer does not.
        raise NotImplementedError("this provider takes the payment on its own page: use a session")

    async def refund(self, booking_id: str, amount: float) -> PaymentResult:
        raise NotImplementedError("this provider returns a charge by its id: use refund_charge")


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


def provider_named(name: str) -> PaymentProvider:
    """The implementation behind a provider name written in the payment ledger.

    A refund goes back through the provider that took the money, whatever PAYMENT_MODE says
    today: switching from the stub to the mock, or on to an acquirer, must not strand the
    bookings paid before the switch.
    """
    if name == "stub":
        return StubProvider()
    if name == "fail":
        return FailProvider()
    if name == PaymentMode.MOCK_ACQUIRING:
        from app.modules.payment.mock_acquirer import MockAcquiringProvider

        return MockAcquiringProvider()
    raise ValueError(f"no implementation for the payment provider {name!r}")


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
    if mode == PaymentMode.MOCK_ACQUIRING:
        from app.modules.payment.mock_acquirer import MockAcquiringProvider

        return MockAcquiringProvider()
    raise ValueError(f"PAYMENT_MODE={mode!r} has no implementation yet")
