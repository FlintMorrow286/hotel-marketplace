"""The cancellation rule as dates; the money side is `payment.service.refund_booking`."""

from __future__ import annotations

import datetime as dt

from app.config import legal


def free_cancellation_last_day(checkin: dt.date) -> dt.date:
    """The last calendar day, by the property's own clock, on which cancelling is free.

    The refund and the letters that name the deadline both read it from here, so they cannot
    disagree about when it passes.
    """
    return checkin - dt.timedelta(hours=legal.CANCELLATION_FREE_BEFORE_HOURS)
