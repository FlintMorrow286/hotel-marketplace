"""The text of the letters. Plain text, Russian, one function per kind.

A letter says what happened and what the reader can do about it, and nothing that works as a key:
the booking's access token never appears (it pays for, cancels and refunds the booking), and the
link leads to the page where the code and the e-mail address are asked for.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.config import legal
from app.modules.booking.cancellation import free_cancellation_last_day

KIND_GUEST_CONFIRMED = "guest_booking_confirmed"
KIND_PARTNER_CONFIRMED = "partner_booking_confirmed"
KIND_GUEST_CANCELLED = "guest_booking_cancelled"
KIND_PARTNER_CANCELLED = "partner_booking_cancelled"
KINDS = (
    KIND_GUEST_CONFIRMED,
    KIND_PARTNER_CONFIRMED,
    KIND_GUEST_CANCELLED,
    KIND_PARTNER_CANCELLED,
)

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_NBSP = "\u00a0"


@dataclass(frozen=True)
class BookingMail:
    """Everything a letter about one booking may say."""

    code: str
    status: str
    origin: str
    guest_name: str
    guest_email: str
    guest_phone: str
    checkin: dt.date
    checkout: dt.date
    checkin_time: dt.time
    checkout_time: dt.time
    total: float
    commission: float
    kept: float | None
    refunded: float | None
    docs_version: str | None
    property_name: str
    city: str
    unit_name: str
    partner_name: str
    partner_email: str

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days


@dataclass(frozen=True)
class Rendered:
    to: str
    subject: str
    body: str


def date_ru(day: dt.date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def money(amount: float) -> str:
    """13 000 ₽, or 3 250,50 ₽ when there are kopecks."""
    cents = round(amount * 100)
    whole, kopecks = divmod(cents, 100)
    digits = f"{whole:,}".replace(",", _NBSP)
    tail = f",{kopecks:02d}" if kopecks else ""
    return f"{digits}{tail}{_NBSP}₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _clock(value: dt.time) -> str:
    return value.strftime("%H:%M")


def _line(text: str) -> str:
    """A header line: one line, however the name was typed."""
    return " ".join(text.split())


def _where(m: BookingMail) -> str:
    place = f"{m.property_name}, {m.unit_name}"
    return f"{place}, {m.city}" if m.city else place


def _stay(m: BookingMail) -> str:
    n = m.nights
    return f"{n}{_NBSP}{plural(n, 'ночь', 'ночи', 'ночей')}"


def _closing(reply_to: str) -> str:
    if reply_to:
        return f"Вопросы по брони можно задать на {reply_to}."
    return "Письмо отправлено автоматически, отвечать на него не нужно."


def render(kind: str, m: BookingMail, *, base_url: str, reply_to: str = "") -> Rendered:
    base = base_url.rstrip("/")
    if kind == KIND_GUEST_CONFIRMED:
        return _guest_confirmed(m, base, reply_to)
    if kind == KIND_GUEST_CANCELLED:
        return _guest_cancelled(m, base, reply_to)
    if kind == KIND_PARTNER_CONFIRMED:
        return _partner_confirmed(m)
    if kind == KIND_PARTNER_CANCELLED:
        return _partner_cancelled(m)
    raise ValueError(f"no letter for kind {kind!r}")


def _guest_confirmed(m: BookingMail, base: str, reply_to: str) -> Rendered:
    last_free_day = date_ru(free_cancellation_last_day(m.checkin))
    penalty = round(legal.CANCELLATION_PENALTY_RATE * 100)
    lines = [
        f"Здравствуйте, {_line(m.guest_name)}!",
        "",
        "Бронь подтверждена и оплачена.",
        "",
        f"Код брони: {m.code}",
        f"Объект: {_where(m)}",
        f"Заезд: {date_ru(m.checkin)}, с {_clock(m.checkin_time)}",
        f"Выезд: {date_ru(m.checkout)}, до {_clock(m.checkout_time)}",
        f"{_stay(m)}, оплачено {money(m.total)}",
        "",
        f"Отмена бесплатна до конца дня {last_free_day} по времени объекта. "
        f"Позже удерживается {penalty}% стоимости.",
        "",
        f"Найти бронь или отменить её можно здесь: {base}/my-booking",
        "Понадобятся код брони и адрес этой почты.",
    ]
    if m.docs_version:
        lines += [
            "",
            "Вы приняли публичную оферту и дали согласие на обработку персональных данных "
            f"(версия документов {m.docs_version}): {base}/legal/offer",
        ]
    lines += ["", _closing(reply_to)]
    subject = _line(f"Бронь {m.code} подтверждена — {m.property_name}")
    return Rendered(m.guest_email, subject, "\n".join(lines) + "\n")


def _guest_cancelled(m: BookingMail, base: str, reply_to: str) -> Rendered:
    kept = m.kept or 0.0
    back = m.refunded if m.refunded is not None else max(m.total - kept, 0.0)
    lines = [
        f"Здравствуйте, {_line(m.guest_name)}!",
        "",
        f"Бронь {m.code} отменена: {_where(m)}, {date_ru(m.checkin)} — {date_ru(m.checkout)}.",
        "",
    ]
    if kept > 0:
        lines.append(f"Возврат: {money(back)}. По условиям отмены удержано {money(kept)}.")
    else:
        lines.append(f"Возврат: {money(back)}, отмена была бесплатной.")
    lines += [
        "Деньги вернутся тем способом, которым вы платили; срок зависит от вашего банка.",
        "",
        f"Статус брони виден здесь: {base}/my-booking",
        "",
        _closing(reply_to),
    ]
    return Rendered(m.guest_email, _line(f"Бронь {m.code} отменена"), "\n".join(lines) + "\n")


def _partner_confirmed(m: BookingMail) -> Rendered:
    lines = [
        f"Новая бронь {m.code}",
        "",
        f"Объект: {m.property_name}, {m.unit_name}",
        f"Заезд: {date_ru(m.checkin)}, выезд: {date_ru(m.checkout)} ({_stay(m)})",
        f"Гость: {_line(m.guest_name)}, {_line(m.guest_phone)}, {m.guest_email}",
        f"Сумма: {money(m.total)}, комиссия платформы {money(m.commission)}",
        "",
        "Бронь уже оплачена гостем и видна в шахматке кабинета.",
    ]
    subject = _line(
        f"Новая бронь {m.code}: {m.property_name}, {date_ru(m.checkin)} — {date_ru(m.checkout)}"
    )
    return Rendered(m.partner_email, subject, "\n".join(lines) + "\n")


def _partner_cancelled(m: BookingMail) -> Rendered:
    kept = m.kept or 0.0
    lines = [
        f"Бронь {m.code} отменена",
        "",
        f"Объект: {m.property_name}, {m.unit_name}",
        f"Даты: {date_ru(m.checkin)} — {date_ru(m.checkout)} ({_stay(m)})",
        f"Гость: {_line(m.guest_name)}",
        "",
    ]
    if kept > 0:
        lines.append(
            f"Отмена поздняя: по условиям отмены у вас остаётся {money(kept)}, "
            "остальное возвращено гостю."
        )
    else:
        lines.append("Отмена бесплатная: деньги возвращены гостю полностью.")
    lines.append("Ночи снова свободны для бронирования.")
    return Rendered(m.partner_email, _line(f"Бронь {m.code} отменена"), "\n".join(lines) + "\n")
