import datetime as dt

import pytest

import app.modules.sync.ical_parser as ical_parser
from app.modules.sync.ical_parser import IcalParseError, expand_dates, parse_calendar


def _calendar(event: str) -> str:
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n{event}\nEND:VEVENT\nEND:VCALENDAR\n"


def _blocked(event: str, start: dt.date, end: dt.date) -> set[dt.date]:
    return expand_dates(parse_calendar(_calendar(event)), start, end)


def test_weekly_count_blocks_only_its_individual_occurrences() -> None:
    event = (
        "UID:weekly\nDTSTART;VALUE=DATE:20260105\nDTEND;VALUE=DATE:20260106\n"
        "RRULE:FREQ=WEEKLY;COUNT=2"
    )
    parsed = parse_calendar(_calendar(event))

    assert len(parsed) == 1
    assert parsed[0].start == dt.date(2026, 1, 5)
    assert parsed[0].end == dt.date(2026, 1, 13)
    assert expand_dates(parsed, dt.date(2026, 1, 1), dt.date(2026, 1, 20)) == {
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 12),
    }


def test_exdate_date_lists_and_repeated_lines_remove_only_named_dates() -> None:
    event = (
        "UID:excluded\nDTSTART;VALUE=DATE:20260105\nDTEND;VALUE=DATE:20260106\n"
        "RRULE:FREQ=WEEKLY;COUNT=5\n"
        "EXDATE;VALUE=DATE:20260112,20260126\n"
        "EXDATE;VALUE=DATE:20260202"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 2, 10)) == {
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 19),
    }


def test_exdate_datetime_honors_tzid_and_value_parameter() -> None:
    event = (
        "UID:timed-exdate\nDTSTART;TZID=America/New_York:20260301T090000\n"
        "DTEND;TZID=America/New_York:20260302T090000\n"
        "RRULE:FREQ=WEEKLY;COUNT=4\n"
        "EXDATE;TZID=America/New_York;VALUE=DATE-TIME:20260308T090000,20260322T090000"
    )

    assert _blocked(event, dt.date(2026, 3, 1), dt.date(2026, 4, 1)) == {
        dt.date(2026, 3, 1),
        dt.date(2026, 3, 15),
    }


def test_tzid_recurrence_compares_until_as_an_inclusive_utc_instant() -> None:
    event = (
        "UID:tz-until\nDTSTART;TZID=Pacific/Kiritimati:20260108T003000\n"
        "DTEND;TZID=Pacific/Kiritimati:20260109T003000\n"
        "RRULE:FREQ=DAILY;UNTIL=20260109T103000Z"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 1, 15)) == {
        dt.date(2026, 1, 8),
        dt.date(2026, 1, 9),
        dt.date(2026, 1, 10),
    }


def test_utc_exdate_matches_the_same_instant_in_a_tzid_recurrence() -> None:
    event = (
        "UID:utc-exdate\nDTSTART;TZID=Pacific/Kiritimati:20260108T003000\n"
        "DTEND;TZID=Pacific/Kiritimati:20260109T003000\n"
        "RRULE:FREQ=DAILY;COUNT=3\nEXDATE:20260109T103000Z"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 1, 15)) == {
        dt.date(2026, 1, 8),
        dt.date(2026, 1, 9),
    }


def test_utc_until_time_is_not_rounded_up_to_its_calendar_date() -> None:
    event = (
        "UID:utc-until\nDTSTART:20260101T230000Z\nDTEND:20260102T230000Z\n"
        "RRULE:FREQ=DAILY;UNTIL=20260103T220000Z"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 1, 5)) == {
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 2),
    }


def test_date_until_is_inclusive_and_no_occurrence_precedes_dtstart() -> None:
    event = (
        "UID:date-until\nDTSTART;VALUE=DATE:20260110\nDTEND;VALUE=DATE:20260111\n"
        "RRULE:FREQ=DAILY;UNTIL=20260112"
    )
    parsed = parse_calendar(_calendar(event))

    assert expand_dates(parsed, dt.date(2026, 1, 1), dt.date(2026, 1, 10)) == set()
    assert expand_dates(parsed, dt.date(2026, 1, 10), dt.date(2026, 1, 15)) == {
        dt.date(2026, 1, 10),
        dt.date(2026, 1, 11),
        dt.date(2026, 1, 12),
    }


def test_unknown_tzid_degrades_to_utc_without_dropping_dates() -> None:
    event = (
        "UID:unknown-zone\nDTSTART;TZID=No/Such_Zone:20260101T100000\n"
        "DTEND;TZID=No/Such_Zone:20260102T100000\nRRULE:FREQ=DAILY;COUNT=2"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 1, 5)) == {
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 2),
    }


def test_unknown_tzid_uses_a_conservative_until_cutoff() -> None:
    event = (
        "UID:unknown-zone-until\nDTSTART;TZID=No/Such_Zone:20260101T230000\n"
        "DTEND;TZID=No/Such_Zone:20260102T230000\n"
        "RRULE:FREQ=DAILY;UNTIL=20260103T000000Z"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 1, 5)) == {
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 2),
        dt.date(2026, 1, 3),
    }


@pytest.mark.parametrize(
    "rule",
    [
        f"FREQ=DAILY;INTERVAL={10**100};COUNT=1",
        f"FREQ=DAILY;COUNT={'9' * 5000}",
    ],
    ids=("overflowing-interval", "oversized-count"),
)
def test_absurd_rrule_numbers_raise_ical_parse_error(rule: str) -> None:
    event = f"UID:absurd\nDTSTART;VALUE=DATE:20260101\nRRULE:{rule}"

    with pytest.raises(IcalParseError):
        parse_calendar(_calendar(event))


def test_old_daily_recurrence_fast_forwards_to_requested_horizon(monkeypatch) -> None:
    """A feed whose event began years ago must not be walked occurrence by occurrence."""
    occurrence_at = ical_parser._occurrence_at
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return occurrence_at(*args)

    monkeypatch.setattr(ical_parser, "_occurrence_at", counted)
    event = (
        "UID:old-daily\nDTSTART;VALUE=DATE:20150101\nDTEND;VALUE=DATE:20150102\n"
        "RRULE:FREQ=DAILY;COUNT=5000"
    )

    assert _blocked(event, dt.date(2026, 1, 1), dt.date(2026, 1, 4)) == {
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 2),
        dt.date(2026, 1, 3),
    }
    assert calls < 20, f"{calls} occurrences computed for a 3-day horizon"


def test_weekly_recurrence_keeps_local_time_across_dst_change() -> None:
    event = (
        "UID:dst-weekly\nDTSTART;TZID=America/New_York:20260301T090000\n"
        "DTEND;TZID=America/New_York:20260302T090000\nRRULE:FREQ=WEEKLY;COUNT=5"
    )

    assert _blocked(event, dt.date(2026, 3, 1), dt.date(2026, 4, 1)) == {
        dt.date(2026, 3, 1),
        dt.date(2026, 3, 8),
        dt.date(2026, 3, 15),
        dt.date(2026, 3, 22),
        dt.date(2026, 3, 29),
    }
