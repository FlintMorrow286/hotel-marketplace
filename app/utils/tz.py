"""Calendar dates at a property.

A guest in Crimea books "today" at 00:30 local time while the server's clock still says
yesterday; a deadline "24:00 the day before arrival" is the property's midnight, not the
server's. Everything that compares a stay with today goes through here.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def today_at(tz_name: str) -> dt.date:
    """The calendar date it is right now in `tz_name` (an unknown zone reads as UTC)."""
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return dt.datetime.now(zone).date()
