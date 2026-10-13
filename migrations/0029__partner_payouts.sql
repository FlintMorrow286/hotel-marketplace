-- 0029__partner_payouts.sql
-- Settlements with partners: what the platform owes a partner for the bookings it collected the
-- money for, grouped into payouts that an operator reviews, approves and marks as paid.
--
-- A payout is a document: draft -> approved -> paid, or cancelled (a draft is thrown away
-- instead). Its lines are not whole bookings but DIFFERENCES: what a booking earns the partner now,
-- minus what the approved and paid payouts already gave for it. A booking is therefore paid out
-- once (its first line is its whole amount), and a refund after a payout shows up as a negative
-- adjustment line in the next one. The sum over a booking's lines in approved and paid payouts is
-- what the partner has been paid for it; no booking can be paid twice.
--
-- A draft is a workspace: at most one per partner and currency, rebuilt on demand, counted by
-- nothing. Approving freezes its lines.
--
-- Money columns are numeric(12,2) and every line and payout keeps net = gross - commission, so a
-- payout's figures can be checked against its lines with plain sums (scripts/check_invariants.py).
--
-- Not stored: the partner's bank details and tax status. Paying is done outside the system; the
-- operator records the bank's payment reference when marking a payout paid.

CREATE TABLE IF NOT EXISTS payout (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    number            bigint        GENERATED ALWAYS AS IDENTITY UNIQUE,
    partner_id        uuid          NOT NULL REFERENCES partner(id),
    currency          text          NOT NULL,
    status            text          NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'approved', 'paid', 'cancelled')),
    gross_amount      numeric(12,2) NOT NULL DEFAULT 0,
    commission_amount numeric(12,2) NOT NULL DEFAULT 0,
    net_amount        numeric(12,2) NOT NULL DEFAULT 0,
    line_count        integer       NOT NULL DEFAULT 0 CHECK (line_count >= 0),
    reference         text,
    created_at        timestamptz   NOT NULL DEFAULT now(),
    approved_at       timestamptz,
    approved_by       text,
    paid_at           timestamptz,
    paid_by           text,
    cancelled_at      timestamptz,
    cancelled_by      text,
    CONSTRAINT payout_net_is_gross_minus_commission
        CHECK (net_amount = gross_amount - commission_amount),
    CONSTRAINT payout_approved_has_money
        CHECK (status NOT IN ('approved', 'paid') OR (net_amount > 0 AND approved_at IS NOT NULL)),
    CONSTRAINT payout_paid_has_reference
        CHECK (status <> 'paid' OR (paid_at IS NOT NULL AND coalesce(reference, '') <> ''))
);

-- One workspace per partner and currency.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payout_one_draft
    ON payout (partner_id, currency) WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_payout_partner ON payout (partner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payout_status  ON payout (status, created_at DESC);

CREATE TABLE IF NOT EXISTS payout_line (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_id         uuid          NOT NULL REFERENCES payout(id) ON DELETE CASCADE,
    booking_id        uuid          NOT NULL REFERENCES booking(id),
    -- 'booking': the first time this booking is paid out; 'adjustment': a later correction.
    kind              text          NOT NULL CHECK (kind IN ('booking', 'adjustment')),
    gross_amount      numeric(12,2) NOT NULL,
    commission_amount numeric(12,2) NOT NULL,
    net_amount        numeric(12,2) NOT NULL,
    UNIQUE (payout_id, booking_id),
    CONSTRAINT payout_line_net_is_gross_minus_commission
        CHECK (net_amount = gross_amount - commission_amount),
    CONSTRAINT payout_line_is_not_empty
        CHECK (gross_amount <> 0 OR commission_amount <> 0)
);

CREATE INDEX IF NOT EXISTS idx_payout_line_booking ON payout_line (booking_id);

COMMENT ON TABLE payout IS
    'Выплата партнёру: документ из строк-разниц по броням; draft -> approved -> paid | cancelled';
COMMENT ON TABLE payout_line IS
    'Строка выплаты: разница между тем, что бронь приносит партнёру сейчас, и уже включённым в утверждённые и выплаченные выплаты';
COMMENT ON COLUMN payout.reference IS
    'Номер платёжного поручения банка; обязателен у paid';
