import datetime as dt

import pytest

from app.modules.sync.ical_parser import IcalParseError, expand_dates, parse_calendar


def _calendar(event: str) -> str:
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n{event}\nEND:VEVENT\nEND:VCALENDAR\n"


def test_start_timezone_conversion_overflow_is_a_parse_error() -> None:
    event = (
        "UID:start-overflow\n"
        "DTSTART;TZID=Etc/GMT+12:99991231T235959\n"
        "DTEND;TZID=Pacific/Kiritimati:99991231T235959"
    )

    with pytest.raises(IcalParseError):
        parse_calendar(_calendar(event))


def test_exdate_timezone_conversion_overflow_is_a_parse_error() -> None:
    event = (
        "UID:exdate-overflow\n"
        "DTSTART;TZID=Etc/GMT+12:99991229T120000\n"
        "DTEND;TZID=Etc/GMT+12:99991230T120000\n"
        "EXDATE;TZID=Etc/GMT+12:99991231T235959"
    )
    parsed = parse_calendar(_calendar(event))

    with pytest.raises(IcalParseError):
        expand_dates(parsed, dt.date(9999, 12, 29), dt.date(9999, 12, 30))


def test_until_timezone_conversion_overflow_is_a_parse_error() -> None:
    event = (
        "UID:until-overflow\n"
        "DTSTART;TZID=Pacific/Kiritimati:99991230T230000\n"
        "DTEND;TZID=Pacific/Kiritimati:99991231T000000\n"
        "RRULE:FREQ=DAILY;UNTIL=99991231T235959Z"
    )

    with pytest.raises(IcalParseError):
        parse_calendar(_calendar(event))
