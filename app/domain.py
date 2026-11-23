"""Typed domain models shared by platforms, matcher, engine and API."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.time import utcnow


class State(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    SOLD_OUT = "SOLD_OUT"


class ErrorKind(StrEnum):
    ACCESS_DENIED = "ACCESS_DENIED"  # 403 or a bot challenge. Never worked around.
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    PARSE_ERROR = "PARSE_ERROR"  # the page did not have the expected structure
    RENDER_ERROR = "RENDER_ERROR"  # JavaScript content never appeared
    ACCESS_NOT_PERMITTED = "ACCESS_NOT_PERMITTED"  # terms prohibit automation; no request is made
    AWAITING_TERMS_DECISION = "AWAITING_TERMS_DECISION"  # owner has not decided; no request is made
    INTERNAL = "INTERNAL"


POLICY_KINDS = frozenset({ErrorKind.ACCESS_NOT_PERMITTED, ErrorKind.AWAITING_TERMS_DECISION})


class SeatStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    SOLD_OUT = "SOLD_OUT"
    UNKNOWN = "UNKNOWN"


class Target(BaseModel):
    model_config = ConfigDict(frozen=True)

    movie: str = "Avengers: Doomsday"
    cinema: str = "INOX Pacific Mall, Jasola"
    city: str = "New Delhi"
    country: str = "India"
    date: dt.date
    language: str = "English"
    format: str = "ScreenX"
    timezone: str = "Asia/Kolkata"
    earliest_time: dt.time
    latest_time: dt.time

    @model_validator(mode="after")
    def _check(self) -> Target:
        if self.timezone != "Asia/Kolkata":
            raise ValueError("timezone must be Asia/Kolkata: every supported platform lists Indian local time")
        if self.earliest_time > self.latest_time:
            raise ValueError("earliest_time must not be after latest_time")
        return self


class SeatCategory(BaseModel):
    name: str
    available: int | None = None
    total: int | None = None
    price_text: str | None = None


class SeatInfo(BaseModel):
    status: SeatStatus = SeatStatus.UNKNOWN
    available: int | None = None
    total: int | None = None
    categories: list[SeatCategory] = Field(default_factory=list)
    available_seats: list[str] = Field(default_factory=list)
    captured_at: dt.datetime = Field(default_factory=utcnow)


class Observation(BaseModel):
    """One showtime exactly as a platform presented it. None means 'the platform did not tell us'."""

    platform: str
    movie: str | None = None
    movie_id: str | None = None
    cinema: str | None = None
    cinema_id: str | None = None
    city: str | None = None
    date: dt.date | None = None
    language: str | None = None
    format: str | None = None
    showtime: dt.time | None = None
    timezone: str = "Asia/Kolkata"
    bookable: bool | None = None
    sold_out: bool = False
    lapsed: bool = False
    session_id: str | None = None
    booking_url: str | None = None
    seats: SeatInfo = Field(default_factory=SeatInfo)
    evidence: dict[str, Any] = Field(default_factory=dict)


class PlatformResult(BaseModel):
    """Outcome of one check of one platform."""

    platform: str
    state: State
    observations: list[Observation] = Field(default_factory=list)
    listing_complete: bool = False  # True only if absence of a session here means it is not listed
    error_kind: ErrorKind | None = None
    error_detail: str | None = None
    retry_after: float | None = None
    signals: dict[str, Any] = Field(default_factory=dict)
    checked_at: dt.datetime = Field(default_factory=utcnow)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.state is not State.PLATFORM_ERROR


class HealthStatus(BaseModel):
    platform: str
    operational: bool
    detail: str = ""
