"""Time helpers. Storage is UTC; everything shown to a person is Asia/Kolkata."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Attach UTC to naive datetimes read back from SQLite; convert aware ones."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def fmt_time(value: time) -> str:
    return datetime.combine(date(2000, 1, 1), value).strftime("%I:%M %p").lstrip("0")


def fmt_ist(value: datetime) -> str:
    local = to_ist(value)
    return f"{local.day} {local.strftime('%b %Y')}, {fmt_time(local.time())} IST"


_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?\s*$")


def parse_clock(text: str) -> time | None:
    """Parse '03:25 PM'. Returns None for anything that is not unambiguous."""
    match = _CLOCK.match(text or "")
    if not match:
        return None
    hour, minute, meridiem = int(match[1]), int(match[2]), match[3].lower()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if meridiem == "p" else 0)
    return time(hour, minute)
