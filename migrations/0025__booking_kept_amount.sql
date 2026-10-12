-- 0025__booking_kept_amount.sql
-- What the partner keeps of a booking that was cancelled late.
--
-- A late cancellation refunds only part of the stay (app.config.legal:
-- CANCELLATION_PENALTY_RATE); the rest stays with the partner and the platform
-- earns its commission on that share. The commission report reads this column
-- instead of recomputing the split from rules that may change later.
--
-- NULL while the booking is not refunded. 0 when it was refunded in full.

ALTER TABLE booking
    ADD COLUMN IF NOT EXISTS kept_amount numeric(12,2)
        CHECK (kept_amount >= 0);

COMMENT ON COLUMN booking.kept_amount IS
    'Сумма, оставшаяся у партнёра после отмены (штраф); NULL, пока бронь не возвращена';
