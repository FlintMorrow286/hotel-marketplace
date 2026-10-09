from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

BookingStatus = Literal[
    "hold", "paid", "confirmed", "cancelled", "failed", "conflict", "no_show", "refunded"
]


class GuestInfo(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=32)


class HoldRequest(BaseModel):
    unit_type_id: str
    checkin: dt.date
    checkout: dt.date
    guest: GuestInfo


class BookingLookup(BaseModel):
    """What a guest types to find a booking: the code and the booking's email."""

    code: str = Field(min_length=3, max_length=32)
    email: EmailStr


class BookingLineOut(BaseModel):
    date: dt.date
    price: float


class BookingOut(BaseModel):
    id: str
    code: str
    status: BookingStatus
    total_amount: float
    hold_expires_at: dt.datetime | None = None
    checkin_date: dt.date
    checkout_date: dt.date


class PartnerBookingLineOut(BaseModel):
    """One night of a booking, as the partner sees the price split."""

    date: dt.date
    price: float


class PartnerBookingOut(BaseModel):
    """A booking on the partner's own inventory.

    Unlike the guest's BookingOut this carries the guest's contacts and where
    the booking came from — the partner has to know who arrives and how the
    row reached them. Amounts are float for the JSON layer, as elsewhere.
    """

    id: str
    code: str
    status: BookingStatus
    total_amount: float
    commission_rate: float
    commission_amount: float
    origin: Literal["web", "channel"]
    source_channel: str | None = None
    guest_name: str
    guest_email: str
    guest_phone: str
    property_id: str
    property_name: str
    unit_type_id: str
    unit_type_name: str
    checkin_date: dt.date
    checkout_date: dt.date
    created_at: dt.datetime
    lines: list[PartnerBookingLineOut] = []


class HoldConflict(BaseModel):
    detail: str
    reason: Literal["not_available", "conflict"] = "not_available"
