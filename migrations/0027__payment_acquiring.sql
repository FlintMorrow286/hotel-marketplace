-- 0027__payment_acquiring.sql
-- Payment on the provider's own page (acquiring), and a stand-in for the provider.
--
-- The guest pays on a page of the acquirer, so a payment now has a life before it succeeds: the
-- core opens a session (a `pending` row with the provider's payment id and the address of the
-- page), and the provider's notification, or the core's own question to it, closes the session
-- with a `succeeded` or `failed` row. A row is one event and is never updated.
--
-- mock_acquirer_payment is the other side of that conversation while there are no keys: what the
-- mock acquirer (app/modules/payment/mock_acquirer.py, PAYMENT_MODE=mock_acquiring) remembers
-- about its payments. It is not shop data: no foreign keys, nothing reads it but the mock, and
-- it stays empty under any other mode. Drop it together with the mock.

ALTER TABLE payment DROP CONSTRAINT IF EXISTS payment_provider_check;
ALTER TABLE payment
    ADD CONSTRAINT payment_provider_check
    CHECK (provider IN ('stub','fail','tinkoff','yookassa','mock_acquiring'));

ALTER TABLE payment ADD COLUMN IF NOT EXISTS payment_url text;

-- A notification names the provider's payment id; the booking is found through this row.
CREATE INDEX IF NOT EXISTS idx_payment_provider_external
    ON payment (provider, external_id) WHERE external_id IS NOT NULL;

COMMENT ON COLUMN payment.payment_url IS
    'Адрес страницы оплаты у провайдера; задан у строки pending, открывающей сессию';

CREATE TABLE IF NOT EXISTS mock_acquirer_payment (
    id          text          PRIMARY KEY,
    order_id    text          NOT NULL,
    amount      numeric(12,2) NOT NULL CHECK (amount >= 0),
    description text          NOT NULL DEFAULT '',
    return_url  text          NOT NULL,
    status      text          NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW','CONFIRMED','REJECTED')),
    refunded    numeric(12,2) NOT NULL DEFAULT 0 CHECK (refunded >= 0 AND refunded <= amount),
    refund_id   text,
    expires_at  timestamptz   NOT NULL,
    created_at  timestamptz   NOT NULL DEFAULT now(),
    decided_at  timestamptz
);

COMMENT ON TABLE mock_acquirer_payment IS
    'Состояние имитации эквайринга (PAYMENT_MODE=mock_acquiring); пуста при любом другом режиме';
