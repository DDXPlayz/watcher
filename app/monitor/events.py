"""Event types and notification text."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from app.domain import Observation, SeatStatus, Target
from app.monitor.matcher import classify_screenx
from app.utils.time import fmt_time, to_ist, utcnow

PLATFORM_NAMES = {"pvr": "PVR INOX", "bookmyshow": "BookMyShow", "district": "District"}


class EventType(StrEnum):
    SCREENING_AVAILABLE = "SCREENING_AVAILABLE"
    SCREENING_REOPENED = "SCREENING_REOPENED"
    SCREENING_SOLD_OUT = "SCREENING_SOLD_OUT"
    SCREENING_UNAVAILABLE = "SCREENING_UNAVAILABLE"
    SCREENING_SEEN = "SCREENING_SEEN"  # matches, but not bookable on first sight
    SEATS_CHANGED = "SEATS_CHANGED"
    UNVERIFIED_LISTING = "UNVERIFIED_LISTING"
    NEW_DATE = "NEW_DATE"
    NEW_FORMAT = "NEW_FORMAT"
    STATE_CHANGED = "STATE_CHANGED"
    PLATFORM_FAILURE = "PLATFORM_FAILURE"
    PLATFORM_RECOVERED = "PLATFORM_RECOVERED"
    NOTIFICATION_DELIVERED = "NOTIFICATION_DELIVERED"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
    MONITORING_PAUSED = "MONITORING_PAUSED"
    MONITORING_STARTED = "MONITORING_STARTED"
    TARGET_CHANGED = "TARGET_CHANGED"
    APP_STARTED = "APP_STARTED"
    APP_STOPPED = "APP_STOPPED"
    SYSTEM = "SYSTEM"


def seat_text(obs: Observation) -> str | None:
    seats = obs.seats
    if seats.available is not None:
        return f"{seats.available} seats visible"
    return {
        SeatStatus.AVAILABLE: "shown as available",
        SeatStatus.LIMITED: "shown as filling fast",
        SeatStatus.SOLD_OUT: "sold out",
    }.get(seats.status)


def screening_message(
    kind: EventType, obs: Observation, target: Target, *, also_on: bool = False
) -> tuple[str, str]:
    """Title and body for a screening alert. Pure text; providers add the link."""
    _, dimension = classify_screenx(obs.format)
    film = target.movie.upper()
    title = {
        EventType.SCREENING_AVAILABLE: f"{film} — SCREENX TICKETS AVAILABLE",
        EventType.SCREENING_REOPENED: f"{film} — SCREENX AVAILABLE AGAIN",
        EventType.SCREENING_SOLD_OUT: f"{film} — SCREENX NOW SOLD OUT",
        EventType.SEATS_CHANGED: f"{film} — SCREENX SEAT UPDATE",
    }[kind]
    if also_on:
        title = f"{film} — SCREENX ALSO ON {PLATFORM_NAMES[obs.platform].upper()}"
    assert obs.showtime is not None and obs.date is not None
    lines = [
        f"Cinema: {target.cinema}",
        f"Date: {obs.date.strftime('%A')}, {obs.date.day} {obs.date.strftime('%B %Y')}",
        f"Language: {target.language}",
        f"Format: ScreenX{f' ({dimension})' if dimension else ''}",
        f"Time: {fmt_time(obs.showtime)} IST",
        f"Platform: {PLATFORM_NAMES[obs.platform]}",
    ]
    if text := seat_text(obs):
        lines.append(f"Seat availability: {text}")
    lines += ["", f"Detected: {fmt_time(to_ist(utcnow()).time())} IST"]
    return title, "\n".join(lines)


def in_window(target: Target, date: dt.date, showtime: dt.time) -> bool:
    return date == target.date and target.earliest_time <= showtime <= target.latest_time
