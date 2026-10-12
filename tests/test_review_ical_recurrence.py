import datetime as dt

import app.modules.sync.ical_parser as ical_parser
from app.modules.sync.ical_parser import expand_dates, parse_calendar


def _calendar(event: str) -> str:
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n{event}\nEND:VEVENT\nEND:VCALENDAR\n"


def _blocked(event: str, start: dt.date, end: dt.date) -> set[dt.date]:
    return expand_dates(parse_calendar(_calendar(event)), start, end)


def test_monthly_count_skips_months_without_the_start_day() -> None:
    event = (
        "UID:month-end\nDTSTART;VALUE=DATE:20260131\nDTEND;VALUE=DATE:20260201\n"
        "RRULE:FREQ=MONTHLY;COUNT=3"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 6, 1)) == {
        dt.date(2026, 1, 31),
        dt.date(2026, 3, 31),
        dt.date(2026, 5, 31),
    }


def test_monthly_interval_skips_invalid_candidate_months() -> None:
    event = (
        "UID:month-end-interval\nDTSTART;VALUE=DATE:20260131\nDTEND;VALUE=DATE:20260201\n"
        "RRULE:FREQ=MONTHLY;INTERVAL=2;COUNT=3"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 6, 1)) == {
        dt.date(2026, 1, 31),
        dt.date(2026, 3, 31),
        dt.date(2026, 5, 31),
    }


def test_yearly_leap_day_count_skips_nonleap_years() -> None:
    event = (
        "UID:leap-day\nDTSTART;VALUE=DATE:20240229\nDTEND;VALUE=DATE:20240301\n"
        "RRULE:FREQ=YEARLY;COUNT=2"
    )

    assert _blocked(event, dt.date(2024, 1, 1), dt.date(2029, 1, 1)) == {
        dt.date(2024, 2, 29),
        dt.date(2028, 2, 29),
    }


def test_day_fifteen_monthly_recurrence_is_unchanged() -> None:
    event = (
        "UID:mid-month\nDTSTART;VALUE=DATE:20260115\nDTEND;VALUE=DATE:20260116\n"
        "RRULE:FREQ=MONTHLY;COUNT=3"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 4, 1)) == {
        dt.date(2026, 1, 15),
        dt.date(2026, 2, 15),
        dt.date(2026, 3, 15),
    }


def test_exdate_removes_a_valid_month_end_occurrence() -> None:
    event = (
        "UID:month-end-exdate\nDTSTART;VALUE=DATE:20260131\nDTEND;VALUE=DATE:20260201\n"
        "RRULE:FREQ=MONTHLY;COUNT=3\nEXDATE;VALUE=DATE:20260331"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 6, 1)) == {
        dt.date(2026, 1, 31),
        dt.date(2026, 5, 31),
    }


def test_until_includes_valid_month_end_occurrences() -> None:
    event = (
        "UID:month-end-until\nDTSTART;VALUE=DATE:20260131\nDTEND;VALUE=DATE:20260201\n"
        "RRULE:FREQ=MONTHLY;UNTIL=20260531"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 6, 1)) == {
        dt.date(2026, 1, 31),
        dt.date(2026, 3, 31),
        dt.date(2026, 5, 31),
    }


def test_monthly_fast_forward_preserves_the_start_day(monkeypatch) -> None:
    occurrence_at = ical_parser._occurrence_at
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return occurrence_at(*args)

    monkeypatch.setattr(ical_parser, "_occurrence_at", counted)
    event = (
        "UID:old-month-end\nDTSTART;VALUE=DATE:20150131\nDTEND;VALUE=DATE:20150201\n"
        "RRULE:FREQ=MONTHLY;COUNT=500"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 2, 1)) == {dt.date(2026, 1, 31)}
    assert calls < 20


def test_yearly_fast_forward_finds_the_next_leap_day() -> None:
    event = (
        "UID:old-leap-day\nDTSTART;VALUE=DATE:20240229\nDTEND;VALUE=DATE:20240301\n"
        "RRULE:FREQ=YEARLY;COUNT=2"
    )

    assert _blocked(event, dt.date(2028, 2, 1), dt.date(2028, 3, 1)) == {dt.date(2028, 2, 29)}
