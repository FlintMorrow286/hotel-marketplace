-- 0028__email_queue.sql
-- Outgoing e-mail: a queue that is written together with the booking change that causes a letter,
-- and a sender (app/jobs/mail.py) that delivers it later.
--
-- A letter is a row, so a booking that was confirmed always has its letters waiting, and a mail
-- server that is down delays them instead of losing them. The row names the booking and the kind of
-- letter; the sender writes the text when it sends, from the booking as it is then.
--
-- dedupe_key is "<kind>:<booking id>": a booking event owes each recipient one letter however often
-- the code that records it runs again. booking_id is not a foreign key on purpose: the queue is a
-- log of what was sent and must outlive, and never block, a booking's own lifecycle.
--
-- 'skipped': the letter was overtaken by events and is no longer true (a confirmation that was still
-- waiting for a retry when the booking was cancelled); the cancellation letter says what happened.
--
-- to_email, subject and delivery are written when the letter is sent. body is kept only when
-- delivery = 'log' (nothing was delivered: the text is what a developer reads instead of a mailbox);
-- after a real delivery it is dropped, as is the whole row after MAIL_SENT_RETENTION_DAYS.

CREATE TABLE IF NOT EXISTS email_message (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    dedupe_key      text        NOT NULL UNIQUE,
    kind            text        NOT NULL
                    CHECK (kind IN ('guest_booking_confirmed', 'partner_booking_confirmed',
                                    'guest_booking_cancelled', 'partner_booking_cancelled')),
    booking_id      uuid        NOT NULL,
    to_email        text,
    subject         text,
    body            text,
    delivery        text        CHECK (delivery IN ('log', 'smtp')),
    status          text        NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'sent', 'failed', 'skipped')),
    attempts        integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);

-- What the sender looks for: queued letters that are due.
CREATE INDEX IF NOT EXISTS idx_email_message_due
    ON email_message (next_attempt_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_email_message_booking ON email_message (booking_id);
CREATE INDEX IF NOT EXISTS idx_email_message_created ON email_message (created_at DESC);

COMMENT ON TABLE email_message IS
    'Очередь исходящих писем: пишется вместе с изменением брони, отправляется джобом app/jobs/mail.py';
COMMENT ON COLUMN email_message.body IS
    'Текст письма; хранится только при delivery = log (письмо никому не ушло), после реальной отправки очищается';
