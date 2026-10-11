"""Minimal RFC 5545 parser for channel calendar imports.

Supports VEVENT dates and datetimes, line unfolding, UID/SUMMARY, EXDATE, and
the common RRULE subset (FREQ, INTERVAL, COUNT, UNTIL). Unbounded recurrences
are refused so a bad feed cannot create an unbounded import.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from functools import lru_cache
from math import gcd
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_UTC = dt.UTC
_MAX_INTERVAL = 1_000_000
_MAX_RECURRENCE_DATE = dt.date(2100, 1, 1)


class IcalParseError(ValueError):
    """Malformed calendar content."""


@dataclass(frozen=True)
class _Temporal:
    value: dt.date | dt.datetime
    tzid: str | None = None
    unknown_timezone: bool = False
    has_timezone: bool = False


@dataclass(frozen=True)
class _RecurrenceRule:
    frequency: str
    interval: int
    count: int | None
    until: _Temporal | None
    last_index: int


@dataclass(frozen=True)
class _EventDetails:
    start: _Temporal
    duration_days: int
    rule: _RecurrenceRule | None
    exdates: tuple[_Temporal, ...]


@dataclass(frozen=True)
class VEvent:
    uid: str
    summary: str
    start: dt.date
    end: dt.date  # exclusive; for RRULEs this remains the previous envelope
    _details: _EventDetails | None = field(default=None, repr=False, compare=False)


def _unfold(text: str) -> list[str]:
    """RFC 5545 line folding: a line beginning with space/tab continues the previous one."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t"):
            if not out:
                raise IcalParseError("continuation line without a parent line")
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _property(line: str) -> tuple[str, str, dict[str, str]]:
    """Split 'NAME;PARAM=v;PARAM2=v2:VALUE' -> (name, value, params)."""
    colon = line.find(":")
    if colon < 0:
        raise IcalParseError(f"property without a value: {line!r}")
    head, value = line[:colon], line[colon + 1 :]
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            raise IcalParseError(f"bad parameter in {line!r}")
        key, item = part.split("=", 1)
        params[key.upper()] = item
    return name, value, params


def _param(params: dict[str, str], name: str) -> str | None:
    value = params.get(name)
    return value.strip().strip('"') if value is not None else None


def _parse_temporal(
    value: str,
    name: str,
    params: dict[str, str] | None = None,
    *,
    default_tz: dt.tzinfo | None = None,
    default_tzid: str | None = None,
    default_unknown: bool = False,
    default_has_timezone: bool = False,
) -> _Temporal:
    params = params or {}
    value_type = (_param(params, "VALUE") or "").upper()
    tzid = _param(params, "TZID")
    date_match = _DATE.fullmatch(value)
    if date_match:
        if value_type == "DATE-TIME":
            raise IcalParseError(f"DATE-TIME {name} has a DATE value")
        try:
            parsed_date = dt.date(*(int(part) for part in date_match.groups()))
        except ValueError as exc:
            raise IcalParseError(f"unparsable {name}: {value!r}") from exc
        return _Temporal(parsed_date)

    datetime_match = _DATETIME.fullmatch(value)
    if not datetime_match or value_type == "DATE":
        raise IcalParseError(f"unparsable {name}: {value!r}")
    if value_type and value_type != "DATE-TIME":
        raise IcalParseError(f"unsupported VALUE for {name}: {value_type!r}")

    try:
        parts = [int(part) for part in datetime_match.groups()[:6]]
        parsed_datetime = dt.datetime(*parts)
    except ValueError as exc:
        raise IcalParseError(f"unparsable {name}: {value!r}") from exc

    is_utc = datetime_match.group(7) == "Z"
    if is_utc and tzid:
        raise IcalParseError(f"{name} cannot have both TZID and UTC suffix")
    if is_utc:
        return _Temporal(parsed_datetime.replace(tzinfo=_UTC), has_timezone=True)
    if tzid:
        try:
            zone = ZoneInfo(tzid)
            unknown_timezone = False
        except (ZoneInfoNotFoundError, OSError, ValueError, TypeError):
            zone = _UTC
            unknown_timezone = True
        return _Temporal(
            parsed_datetime.replace(tzinfo=zone),
            tzid=tzid,
            unknown_timezone=unknown_timezone,
            has_timezone=True,
        )
    if default_tz is not None:
        return _Temporal(
            parsed_datetime.replace(tzinfo=default_tz),
            tzid=default_tzid,
            unknown_timezone=default_unknown,
            has_timezone=default_has_timezone,
        )
    return _Temporal(parsed_datetime.replace(tzinfo=_UTC))


def parse_calendar(text: str) -> list[VEvent]:
    """Parse VEVENTs, refusing unbounded and unsupported recurrence frequencies."""
    lines = _unfold(text)
    events: list[VEvent] = []
    props: list[tuple[str, str, dict[str, str]]] = []
    depth = 0

    for line in lines:
        if not line.strip():
            continue
        if line == "BEGIN:VEVENT":
            depth += 1
            if depth != 1:
                raise IcalParseError("nested VEVENT")
            props = []
            continue
        if line == "END:VEVENT":
            depth -= 1
            if depth != 0:
                raise IcalParseError("unbalanced VEVENT")
            events.append(_build_event(props))
            continue
        if depth == 1:
            props.append(_property(line))

    if depth != 0:
        raise IcalParseError("unterminated VEVENT")
    return events


def _is_datetime(value: dt.date | dt.datetime) -> bool:
    return isinstance(value, dt.datetime)


def _calendar_date(temporal: _Temporal) -> dt.date:
    value = temporal.value
    return value.date() if isinstance(value, dt.datetime) else value


def _to_timezone(value: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    try:
        return value.astimezone(zone)
    except (OverflowError, ValueError) as exc:
        raise IcalParseError("DATE-TIME timezone conversion out of range") from exc


def _event_after(left: _Temporal, right: _Temporal) -> bool:
    if _is_datetime(left.value) != _is_datetime(right.value):
        return False
    if isinstance(left.value, dt.datetime) and isinstance(right.value, dt.datetime):
        return _to_timezone(left.value, _UTC) > _to_timezone(right.value, _UTC)
    return left.value > right.value


def _add_one_day(start: _Temporal, uid: str) -> _Temporal:
    try:
        value = start.value + dt.timedelta(days=1)
    except (OverflowError, ValueError) as exc:
        raise IcalParseError(f"DTEND out of range for {uid!r}") from exc
    return _Temporal(value, start.tzid, start.unknown_timezone, start.has_timezone)


def _build_event(props: list[tuple[str, str, dict[str, str]]]) -> VEvent:
    bag = {name: (value, params) for name, value, params in props}
    if "DTSTART" not in bag:
        raise IcalParseError("VEVENT without DTSTART")

    uid = bag.get("UID", ("", {}))[0]
    summary = bag.get("SUMMARY", ("", {}))[0]
    start = _parse_temporal(bag["DTSTART"][0], "DTSTART", bag["DTSTART"][1])

    if "DTEND" in bag:
        default_tz = start.value.tzinfo if isinstance(start.value, dt.datetime) else None
        end = _parse_temporal(
            bag["DTEND"][0],
            "DTEND",
            bag["DTEND"][1],
            default_tz=default_tz,
            default_tzid=start.tzid,
            default_unknown=start.unknown_timezone,
            default_has_timezone=start.has_timezone,
        )
    else:
        end = _add_one_day(start, uid)

    if _is_datetime(start.value) != _is_datetime(end.value) or not _event_after(end, start):
        raise IcalParseError(f"DTEND not after DTSTART for {uid!r}")
    start_date = _calendar_date(start)
    end_date = _calendar_date(end)
    duration_days = (end_date - start_date).days
    if duration_days < 1:
        raise IcalParseError(f"DTEND has no blocked date for {uid!r}")

    # RFC RDATE is additive; only RRULE/EXDATE are handled by this importer.
    rule_props = [item for item in props if item[0] == "RRULE"]
    if len(rule_props) > 1:
        raise IcalParseError(f"multiple RRULE properties in {uid!r}")
    rule = _parse_rrule(rule_props[0][1], uid, start) if rule_props else None

    default_tz = start.value.tzinfo if isinstance(start.value, dt.datetime) else None
    exdates: list[_Temporal] = []
    for _, value, params in (item for item in props if item[0] == "EXDATE"):
        values = value.split(",")
        if any(not item for item in values):
            raise IcalParseError(f"empty EXDATE value in {uid!r}")
        for item in values:
            exdate = _parse_temporal(
                item,
                "EXDATE",
                params,
                default_tz=default_tz,
                default_tzid=start.tzid,
                default_unknown=start.unknown_timezone,
                default_has_timezone=start.has_timezone,
            )
            if _is_datetime(exdate.value) != _is_datetime(start.value):
                raise IcalParseError(f"EXDATE value type differs from DTSTART for {uid!r}")
            exdates.append(exdate)

    last_start = (
        _occurrence_at(start, rule.frequency, rule.interval, rule.last_index) if rule else start
    )
    try:
        envelope_end = _calendar_date(last_start) + dt.timedelta(days=duration_days)
    except (OverflowError, ValueError) as exc:
        raise IcalParseError(f"RRULE end out of range for {uid!r}") from exc

    details = _EventDetails(start, duration_days, rule, tuple(exdates)) if rule or exdates else None
    return VEvent(uid, summary, start_date, envelope_end, details)


def _parse_positive_int(value: str, name: str, uid: str) -> int:
    try:
        parsed = int(value)
    except (ValueError, OverflowError) as exc:
        raise IcalParseError(f"bad RRULE {name} in {uid!r}") from exc
    if parsed < 1:
        raise IcalParseError(f"bad RRULE {name} in {uid!r}")
    return parsed


def _parse_rrule(rule: str, uid: str, start: _Temporal) -> _RecurrenceRule:
    parts: dict[str, str] = {}
    for chunk in rule.split(";"):
        if "=" not in chunk:
            raise IcalParseError(f"bad RRULE chunk in {uid!r}: {chunk!r}")
        key, value = chunk.split("=", 1)
        parts[key.upper()] = value.upper()

    frequency = parts.get("FREQ")
    if frequency not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise IcalParseError(f"unsupported RRULE FREQ in {uid!r}: {frequency!r}")
    interval = _parse_positive_int(parts.get("INTERVAL", "1"), "INTERVAL", uid)
    if interval > _MAX_INTERVAL:
        raise IcalParseError(f"RRULE INTERVAL too large in {uid!r}")

    count = _parse_positive_int(parts["COUNT"], "COUNT", uid) if "COUNT" in parts else None
    if count is None and "UNTIL" not in parts:
        raise IcalParseError(f"unbounded RRULE in {uid!r} - needs COUNT or UNTIL")

    until = _parse_temporal(parts["UNTIL"], "UNTIL") if "UNTIL" in parts else None
    if until is not None:
        if _is_datetime(start.value) != _is_datetime(until.value):
            raise IcalParseError(f"UNTIL value type differs from DTSTART in {uid!r}")
        if start.has_timezone and isinstance(until.value, dt.datetime) and not until.has_timezone:
            raise IcalParseError(f"UNTIL must be UTC when DTSTART has a time zone in {uid!r}")

    until_index = _last_index_until(start, frequency, interval, until) if until else None
    last_index = count - 1 if count is not None else until_index
    if count is not None and until_index is not None:
        last_index = min(last_index, until_index)
    if last_index is None:
        raise IcalParseError(f"unbounded RRULE in {uid!r}")

    if last_index > 0:
        last_date = _calendar_date(_occurrence_at(start, frequency, interval, last_index))
        if last_date > _MAX_RECURRENCE_DATE:
            raise IcalParseError(f"RRULE ran past 2100 in {uid!r} - needs COUNT or UNTIL")
    return _RecurrenceRule(frequency, interval, count, until, last_index)


def _month_has_day(year: int, month: int, day: int) -> bool:
    if month == 2:
        days_in_month = 29 if calendar.isleap(year) else 28
    elif month in (4, 6, 9, 11):
        days_in_month = 30
    else:
        days_in_month = 31
    return day <= days_in_month


@lru_cache(maxsize=256)
def _valid_period_pattern(
    frequency: str, interval: int, cycle_index: int, month: int, day: int
) -> tuple[int, tuple[int, ...]]:
    cycle_size = 4800 if frequency == "MONTHLY" else 400
    period_count = cycle_size // gcd(cycle_size, interval)
    valid_periods = []
    for period in range(period_count):
        if frequency == "MONTHLY":
            month_index = cycle_index + interval * period
            year, month_index = divmod(month_index, 12)
            candidate_month = month_index + 1
        else:
            year = cycle_index + interval * period
            candidate_month = month
        if _month_has_day(year, candidate_month, day):
            valid_periods.append(period)
    return period_count, tuple(valid_periods)


def _skips_invalid_periods(start: _Temporal, frequency: str) -> bool:
    value = start.value
    if frequency == "MONTHLY":
        return value.day > 28
    return frequency == "YEARLY" and value.month == 2 and value.day == 29


def _period_pattern(start: _Temporal, frequency: str, interval: int) -> tuple[int, tuple[int, ...]]:
    value = start.value
    if frequency == "MONTHLY":
        cycle_index = (value.year % 400) * 12 + value.month - 1
    else:
        cycle_index = value.year % 400
    return _valid_period_pattern(frequency, interval, cycle_index, value.month, value.day)


def _period_index(start: _Temporal, frequency: str, interval: int, index: int) -> int:
    if not _skips_invalid_periods(start, frequency):
        return index
    cycle_size, valid_periods = _period_pattern(start, frequency, interval)
    cycles, position = divmod(index, len(valid_periods))
    return cycles * cycle_size + valid_periods[position]


def _occurrence_index_at_or_before_period(
    start: _Temporal, frequency: str, interval: int, period: int
) -> int:
    if not _skips_invalid_periods(start, frequency):
        return period
    cycle_size, valid_periods = _period_pattern(start, frequency, interval)
    cycles, offset = divmod(period, cycle_size)
    count = cycles * len(valid_periods) + bisect_right(valid_periods, offset)
    return count - 1


def _occurrence_at(start: _Temporal, frequency: str, interval: int, index: int) -> _Temporal:
    value = start.value
    months_per_step = interval if frequency == "MONTHLY" else interval * 12
    try:
        if frequency in ("DAILY", "WEEKLY"):
            days = interval * index * (7 if frequency == "WEEKLY" else 1)
            shifted = value + dt.timedelta(days=days)
        else:
            period_index = _period_index(start, frequency, interval, index)
            month_index = value.year * 12 + value.month - 1 + months_per_step * period_index
            year, month_index = divmod(month_index, 12)
            month = month_index + 1
            day = value.day
            if isinstance(value, dt.datetime):
                shifted = value.replace(year=year, month=month, day=day)
            else:
                shifted = dt.date(year, month, day)
    except (OverflowError, ValueError) as exc:
        raise IcalParseError("RRULE occurrence is out of range") from exc
    return _Temporal(shifted, start.tzid, start.unknown_timezone, start.has_timezone)


def _index_floor(start: _Temporal, frequency: str, interval: int, target: dt.date) -> int:
    start_date = _calendar_date(start)
    if frequency in ("DAILY", "WEEKLY"):
        period = interval * (7 if frequency == "WEEKLY" else 1)
        return max(0, (target - start_date).days // period)
    months = (target.year - start_date.year) * 12 + target.month - start_date.month
    period = interval if frequency == "MONTHLY" else interval * 12
    candidate_period = max(0, months // period)
    return _occurrence_index_at_or_before_period(start, frequency, interval, candidate_period)


def _until_target_date(until: _Temporal, start: _Temporal) -> dt.date:
    if isinstance(until.value, dt.date) and not isinstance(until.value, dt.datetime):
        return until.value
    assert isinstance(until.value, dt.datetime)
    if start.unknown_timezone:
        try:
            utc_date = _to_timezone(until.value, _UTC).date()
            return utc_date + dt.timedelta(days=1)
        except (OverflowError, ValueError) as exc:
            raise IcalParseError("UNTIL out of range") from exc
    assert isinstance(start.value, dt.datetime)
    assert start.value.tzinfo is not None
    return _to_timezone(until.value, start.value.tzinfo).date()


def _within_until(occurrence: _Temporal, until: _Temporal) -> bool:
    if isinstance(occurrence.value, dt.datetime) and isinstance(until.value, dt.datetime):
        occurrence_utc = _to_timezone(occurrence.value, _UTC)
        until_utc = _to_timezone(until.value, _UTC)
        if occurrence.unknown_timezone:
            try:
                # Unknown zones get a conservative margin so a valid close is not lost.
                occurrence_utc -= dt.timedelta(days=1)
            except OverflowError as exc:
                raise IcalParseError("RRULE occurrence out of range") from exc
        return occurrence_utc <= until_utc
    return _calendar_date(occurrence) <= _calendar_date(until)


def _last_index_until(start: _Temporal, frequency: str, interval: int, until: _Temporal) -> int:
    if not _within_until(start, until):
        return 0
    candidate = _index_floor(start, frequency, interval, _until_target_date(until, start))
    while candidate > 0 and not _within_until(
        _occurrence_at(start, frequency, interval, candidate), until
    ):
        candidate -= 1
    while True:
        try:
            next_occurrence = _occurrence_at(start, frequency, interval, candidate + 1)
        except IcalParseError:
            break
        if not _within_until(next_occurrence, until):
            break
        candidate += 1
    return candidate


def _first_index_at_or_after(
    start: _Temporal, frequency: str, interval: int, target: dt.date
) -> int:
    candidate = _index_floor(start, frequency, interval, target)
    while _calendar_date(_occurrence_at(start, frequency, interval, candidate)) < target:
        candidate += 1
    return candidate


def _is_excluded(occurrence: _Temporal, exdates: tuple[_Temporal, ...]) -> bool:
    for exdate in exdates:
        if isinstance(occurrence.value, dt.datetime) and isinstance(exdate.value, dt.datetime):
            if occurrence.unknown_timezone or exdate.unknown_timezone:
                if occurrence.value.replace(tzinfo=None) == exdate.value.replace(tzinfo=None):
                    return True
            elif _to_timezone(occurrence.value, _UTC) == _to_timezone(exdate.value, _UTC):
                return True
        elif occurrence.value == exdate.value:
            return True
    return False


def _add_dates(out: set[dt.date], start: dt.date, end: dt.date) -> None:
    current = start
    while current < end:
        out.add(current)
        try:
            current += dt.timedelta(days=1)
        except (OverflowError, ValueError) as exc:
            raise IcalParseError("date range out of range") from exc


def _expand_recurrence(
    event: VEvent, details: _EventDetails, date_from: dt.date, date_to: dt.date, out: set[dt.date]
) -> None:
    rule = details.rule
    last_index = rule.last_index if rule is not None else 0
    frequency = rule.frequency if rule is not None else "DAILY"
    interval = rule.interval if rule is not None else 1

    target = date_from
    if details.duration_days > 1:
        try:
            target = date_from - dt.timedelta(days=details.duration_days - 1)
        except (OverflowError, ValueError) as exc:
            raise IcalParseError(f"RRULE target out of range for {event.uid!r}") from exc
    if target > _calendar_date(_occurrence_at(details.start, frequency, interval, last_index)):
        return

    first_index = _first_index_at_or_after(details.start, frequency, interval, target)
    if first_index > last_index:
        return

    for index in range(first_index, last_index + 1):
        occurrence = _occurrence_at(details.start, frequency, interval, index)
        occurrence_date = _calendar_date(occurrence)
        if occurrence_date >= date_to:
            break
        if _is_excluded(occurrence, details.exdates):
            continue
        try:
            occurrence_end = occurrence_date + dt.timedelta(days=details.duration_days)
        except (OverflowError, ValueError) as exc:
            raise IcalParseError(f"RRULE end out of range for {event.uid!r}") from exc
        _add_dates(out, max(occurrence_date, date_from), min(occurrence_end, date_to))


def expand_dates(events: list[VEvent], date_from: dt.date, date_to: dt.date) -> set[dt.date]:
    """Blocked dates inside [date_from, date_to)."""
    out: set[dt.date] = set()
    if date_from >= date_to:
        return out
    for event in events:
        details = event._details
        if details is not None:
            _expand_recurrence(event, details, date_from, date_to, out)
            continue
        _add_dates(out, max(event.start, date_from), min(event.end, date_to))
    return out
