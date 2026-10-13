-- 0026__booking_docs_consent.sql
-- Which version of the public documents the guest accepted, and when.
--
-- A web guest ticks two boxes at checkout: the offer, and — as a separate consent — the
-- processing of personal data (152-FZ art. 9, separate document since 2025-09-01). The hold
-- request carries app.config.legal.LEGAL_DOCS_VERSION, and the route refuses a version that is
-- not the current one. The version and the moment are the evidence of that consent, so they are
-- written by the same INSERT as the hold itself.
--
-- NULL for channel bookings (the OTA's own terms apply, not ours) and for bookings made before
-- this migration. The two columns are NULL together or set together.

ALTER TABLE booking
    ADD COLUMN IF NOT EXISTS docs_version     text,
    ADD COLUMN IF NOT EXISTS docs_accepted_at timestamptz,
    ADD CONSTRAINT booking_docs_consent_pair
        CHECK ((docs_version IS NULL) = (docs_accepted_at IS NULL));

COMMENT ON COLUMN booking.docs_version IS
    'Версия оферты и согласия на обработку ПД, принятая гостем (app.config.legal.LEGAL_DOCS_VERSION); NULL у броней канала и до миграции 0026';
COMMENT ON COLUMN booking.docs_accepted_at IS
    'Когда гость принял документы; задан тогда и только тогда, когда задана docs_version';
