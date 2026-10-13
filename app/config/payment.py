"""Payment modes.

Phase 1 runs on STUB: the booking core must work end-to-end without a real
acquiring provider. Real acquiring (Tinkoff / YooKassa) plugs in later by
implementing `HostedPaymentProvider` (app/modules/payment/provider.py): a session, a page
of the provider, a notification. Until there are keys, MOCK_ACQUIRING plays the acquirer
with the same steps, so the core runs the flow it will run in production.

`PAYMENT_MODE` selects the provider implementation at runtime.
"""

from enum import StrEnum


class PaymentMode(StrEnum):
    """`PAYMENT_MODE` values."""

    STUB = "stub"  # мгновенный успех — для разработки ядра
    DELAYED_STUB = "delayed_stub"  # успех через N секунд — для тестов hold TTL
    FAIL = "fail"  # всегда failed — для тестов веток ошибок
    MOCK_ACQUIRING = "mock_acquiring"  # имитация эквайринга: сессия, страница оплаты, уведомление
    TINKOFF = "tinkoff"  # реальный эквайринг (Phase 1.5+)


# Время искусственной задержки для DELAYED_STUB, секунд.
PAYMENT_STUB_DELAY_SEC = 30

VALID_MODES = {m.value for m in PaymentMode}

# Modes in which nobody is charged: the checkout says so, and outside development the
# app warns at startup that bookings are confirmed for free.
NO_MONEY_MODES = {
    PaymentMode.STUB,
    PaymentMode.DELAYED_STUB,
    PaymentMode.FAIL,
    PaymentMode.MOCK_ACQUIRING,
}
