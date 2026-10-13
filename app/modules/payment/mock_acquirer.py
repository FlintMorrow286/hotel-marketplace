"""A stand-in for an acquirer: until there are keys, it plays the bank.

It goes through the steps Tinkoff Kassa or YooKassa do, so the booking core runs the flow it
will run in production and only the provider behind it changes:

  1. the core opens a session (`create_session`) and gets the address of a payment page;
  2. the guest pays on that page, served here (`/mock-acquirer/pay/{id}`), or does not;
  3. the "bank" reports the result with a signed notification to the core and sends the guest
     back to the shop;
  4. the core can ask for the state of a payment at any time (`fetch`) and give money back
     (`refund_charge`).

The page offers what a tester needs: pay, a bank refusal, and a payment whose notification
never arrives (the core must find it by asking). The provider's state is its own table,
`mock_acquirer_payment`: it shares no rows with the shop, as a real provider would not.

Nobody is charged. Outside development the app warns at startup that bookings are confirmed on
a test payment page, the same as it does for the stub.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
from collections.abc import Mapping
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config.payment import PaymentMode
from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.payment.provider import (
    HostedPaymentProvider,
    NotificationError,
    PaymentResult,
    PaymentSession,
    ProviderPayment,
    UnknownPayment,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

SIGNATURE_HEADER = "X-Mock-Signature"
NOTIFICATION_PATH = "/v1/payments/notifications/mock_acquiring"

# What the payment page can do, and whether the shop hears about it.
_OUTCOMES = {
    "pay": ("CONFIRMED", True),
    "decline": ("REJECTED", True),
    "pay_silently": ("CONFIRMED", False),
}


def sign(body: bytes) -> str:
    """The signature a notification carries; the provider and the shop share the key."""
    key = hashlib.sha256(b"mock-acquirer-notification|" + settings.jwt_secret.encode()).digest()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _page_url(payment_id: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/mock-acquirer/pay/{payment_id}"


class MockAcquiringProvider(HostedPaymentProvider):
    @property
    def name(self) -> str:
        return PaymentMode.MOCK_ACQUIRING.value

    async def create_session(
        self,
        booking_id: str,
        amount: float,
        *,
        return_url: str,
        description: str,
        ttl_sec: int,
    ) -> PaymentSession:
        payment_id = f"mock_{secrets.token_hex(12)}"
        await get_pool().execute(
            """
            INSERT INTO mock_acquirer_payment
                (id, order_id, amount, description, return_url, expires_at)
            VALUES ($1, $2, $3::numeric, $4, $5, now() + make_interval(secs => $6))
            """,
            payment_id,
            booking_id,
            amount,
            description,
            return_url,
            float(ttl_sec),
        )
        return PaymentSession(payment_id, _page_url(payment_id))

    async def pay(self, booking_id: str, amount: float) -> PaymentResult:
        """Pay at once, as if the guest had done it on the page.

        For the demo seed and scripts, which have no browser to send to a page. Idempotent on
        the booking, as the interface asks; the guest's own checkout never comes here.
        """
        pool = get_pool()
        paid = await pool.fetchval(
            "SELECT id FROM mock_acquirer_payment WHERE order_id = $1 AND status = 'CONFIRMED'",
            booking_id,
        )
        if paid is None:
            paid = f"mock_{secrets.token_hex(12)}"
            await pool.execute(
                """
                INSERT INTO mock_acquirer_payment
                    (id, order_id, amount, description, return_url, status, expires_at, decided_at)
                VALUES ($1, $2, $3::numeric, 'paid at once', '', 'CONFIRMED', now(), now())
                """,
                paid,
                booking_id,
                amount,
            )
        return PaymentResult("succeeded", external_id=paid)

    async def fetch(self, external_id: str) -> ProviderPayment:
        row = await get_pool().fetchrow(
            """
            SELECT status, amount::float8 AS amount, expires_at <= now() AS expired
            FROM mock_acquirer_payment WHERE id = $1
            """,
            external_id,
        )
        if row is None:
            raise UnknownPayment(external_id)
        if row["status"] == "CONFIRMED":
            state = "succeeded"
        elif row["status"] == "REJECTED" or row["expired"]:
            state = "failed"
        else:
            state = "pending"
        return ProviderPayment(external_id, state, row["amount"])

    def read_notification(self, body: bytes, headers: Mapping[str, str]) -> str:
        given = headers.get(SIGNATURE_HEADER, "")
        if not hmac.compare_digest(given.encode(), sign(body).encode()):
            raise NotificationError("bad signature")
        try:
            payment_id = json.loads(body)["payment_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise NotificationError("unreadable notification") from exc
        if not isinstance(payment_id, str) or not payment_id:
            raise NotificationError("unreadable notification")
        return payment_id

    async def refund_charge(
        self, booking_id: str, amount: float, charge_id: str | None
    ) -> PaymentResult:
        if not charge_id:
            return PaymentResult("failed")
        pool = get_pool()
        refund_id = f"{charge_id}_refund"
        row = await pool.fetchrow(
            """
            UPDATE mock_acquirer_payment
            SET refunded = $2::numeric, refund_id = $3
            WHERE id = $1 AND status = 'CONFIRMED' AND refund_id IS NULL
              AND $2::numeric <= amount
            RETURNING refund_id, refunded::float8 AS refunded
            """,
            charge_id,
            amount,
            refund_id,
        )
        if row is not None:
            return PaymentResult("refunded", external_id=row["refund_id"], amount=row["refunded"])
        # Asked twice: answer the first refund again, amount included, instead of paying back twice.
        first = await pool.fetchrow(
            """
            SELECT refund_id, refunded::float8 AS refunded FROM mock_acquirer_payment
            WHERE id = $1 AND status = 'CONFIRMED'
            """,
            charge_id,
        )
        if first is None or not first["refund_id"]:
            return PaymentResult("failed")
        return PaymentResult("refunded", external_id=first["refund_id"], amount=first["refunded"])


# ---------------------------------------------------------------- the provider's own pages


def _require_mock() -> None:
    if settings.payment_mode != PaymentMode.MOCK_ACQUIRING:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


router = APIRouter(
    prefix="/mock-acquirer",
    dependencies=[Depends(_require_mock)],
    include_in_schema=False,
)

_STYLE = """
body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#f4f1ea;color:#1c1b19}
main{max-width:28rem;margin:3rem auto;padding:2rem;background:#fff;border:1px solid #d8d3c8}
.badge{display:inline-block;margin:0 0 1rem;padding:.25rem .6rem;background:#fde9a8;
font-size:.8rem;font-weight:600}
h1{margin:0 0 .5rem;font-size:1.5rem}.muted{color:#5d5a52;font-size:.9rem}
dl{margin:1.5rem 0}dt{color:#5d5a52;font-size:.85rem}dd{margin:0 0 .75rem;font-size:1.1rem}
button,.back{display:block;width:100%;box-sizing:border-box;margin:.5rem 0 0;padding:.8rem 1rem;
font:inherit;text-align:center;text-decoration:none;color:#1c1b19;background:#fff;
border:1px solid #1c1b19;cursor:pointer}
button.primary{color:#fff;background:#1c1b19}
"""


def _document(body: str) -> HTMLResponse:
    page = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex"><title>Оплата заказа</title>'
        f"<style>{_STYLE}</style></head><body><main>"
        '<p class="badge">Тестовая оплата — деньги не списываются</p>'
        f"<h1>Оплата заказа</h1>{body}</main></body></html>"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", "\u00a0").replace(".", ",") + "\u00a0₽"


def _back(return_url: str, result: str) -> str:
    return f"{return_url}{'&' if '?' in return_url else '?'}result={result}"


async def _load(payment_id: str):
    row = await get_pool().fetchrow(
        """
        SELECT id, order_id, description, return_url, status, amount::float8 AS amount,
               expires_at <= now() AS expired
        FROM mock_acquirer_payment WHERE id = $1
        """,
        payment_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found")
    return row


@router.get("/pay/{payment_id}", response_class=HTMLResponse)
async def payment_page(payment_id: str) -> HTMLResponse:
    row = await _load(payment_id)
    facts = (
        f"<dl><dt>Заказ</dt><dd>{html.escape(row['description'])}</dd>"
        f"<dt>Сумма</dt><dd><strong>{_money(row['amount'])}</strong></dd></dl>"
    )
    if row["status"] == "NEW" and not row["expired"]:
        return _document(
            '<p class="muted">Это имитация платёжной страницы банка: выберите, чем закончится '
            "платёж.</p>"
            f"{facts}"
            '<form method="post">'
            f'<button class="primary" name="action" value="pay">Оплатить {_money(row["amount"])}'
            "</button>"
            '<button name="action" value="decline">Банк отклонил платёж</button>'
            '<button name="action" value="pay_silently">Платёж прошёл, но магазин не получит '
            "уведомление</button></form>"
        )
    if row["status"] == "CONFIRMED":
        note, result = "Платёж выполнен.", "success"
    elif row["status"] == "REJECTED":
        note, result = "Платёж отклонён.", "fail"
    else:
        note, result = "Срок оплаты истёк.", "fail"
    href = html.escape(_back(row["return_url"], result), quote=True)
    return _document(
        f'{facts}<p role="status">{note}</p><a class="back" href="{href}">Вернуться</a>'
    )


@router.post("/pay/{payment_id}")
async def decide_payment(
    payment_id: str, request: Request, action: Annotated[str, Form()]
) -> RedirectResponse:
    if action not in _OUTCOMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown action")
    new_status, notify = _OUTCOMES[action]
    decided = await get_pool().fetchrow(
        """
        UPDATE mock_acquirer_payment
        SET status = $2, decided_at = now()
        WHERE id = $1 AND status = 'NEW' AND expires_at > now()
        RETURNING id, order_id, amount::text AS amount, return_url, status
        """,
        payment_id,
        new_status,
    )
    if decided is None:
        # Decided already, or out of time: the page just shows how it ended.
        row = await _load(payment_id)
        result = "success" if row["status"] == "CONFIRMED" else "fail"
        return RedirectResponse(_back(row["return_url"], result), status.HTTP_303_SEE_OTHER)
    if notify:
        await _notify(request, decided)
    result = "success" if decided["status"] == "CONFIRMED" else "fail"
    return RedirectResponse(_back(decided["return_url"], result), status.HTTP_303_SEE_OTHER)


async def _notify(request: Request, payment) -> None:
    """Tell the shop, the way a bank does: a signed POST to its notification address.

    The call goes straight into this application, with no network in between. A bank retries
    a refused notification; this one does not, and the core finds the payment by asking.
    """
    body = json.dumps(
        {
            "payment_id": payment["id"],
            "order_id": payment["order_id"],
            "status": payment["status"],
            "amount": payment["amount"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app), base_url="http://mock-acquirer"
        ) as client:
            response = await client.post(
                NOTIFICATION_PATH,
                content=body,
                headers={SIGNATURE_HEADER: sign(body), "Content-Type": "application/json"},
            )
        if response.status_code != 200:
            log.warning(
                "mock-acquirer-notification-refused",
                payment_id=payment["id"],
                status=response.status_code,
            )
    except Exception as exc:
        log.warning("mock-acquirer-notification-failed", payment_id=payment["id"], error=repr(exc))
