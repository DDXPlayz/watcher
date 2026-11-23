"""SQLAlchemy models. Only portable column types, so the same schema works on PostgreSQL."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.utils.time import as_utc, utcnow


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Stores naive UTC, returns aware UTC, on every backend."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        return as_utc(value).replace(tzinfo=None) if value is not None else None

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        return as_utc(value) if value is not None else None


class Base(DeclarativeBase):
    pass


class PlatformRow(Base):
    __tablename__ = "platforms"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(32), default="UNVERIFIED")
    last_good_state: Mapped[str | None] = mapped_column(String(32))
    error_kind: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[str | None] = mapped_column(Text)
    last_attempt: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_success: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)  # consecutive
    recoveries: Mapped[int] = mapped_column(Integer, default=0)
    signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScreeningRow(Base):
    __tablename__ = "screenings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), unique=True)
    entity_key: Mapped[str] = mapped_column(String(255), index=True)
    platform: Mapped[str] = mapped_column(ForeignKey("platforms.id"), index=True)
    external_session_id: Mapped[str | None] = mapped_column(String(128))
    movie: Mapped[str] = mapped_column(String(128))
    cinema: Mapped[str] = mapped_column(String(128))
    date: Mapped[dt.date] = mapped_column(Date)
    language: Mapped[str] = mapped_column(String(64))
    format: Mapped[str] = mapped_column(String(64))
    showtime: Mapped[dt.time] = mapped_column(Time)
    booking_url: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[str] = mapped_column(String(32))
    seat_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    seats_available: Mapped[int | None] = mapped_column(Integer)
    seats_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    ever_available: Mapped[bool] = mapped_column(Boolean, default=False)
    seat_alert_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_detected: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_verified: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class SeatSnapshotRow(Base):
    __tablename__ = "seat_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    screening_id: Mapped[int] = mapped_column(ForeignKey("screenings.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(32))
    available: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    seats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    platform: Mapped[str | None] = mapped_column(String(32), index=True)
    screening_id: Mapped[int | None] = mapped_column(ForeignKey("screenings.id", ondelete="SET NULL"))
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NotificationRow(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(320), unique=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32))  # ticket | update | system | reminder | test
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    detected_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)  # when the check that found it finished
    send_ms: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # per-channel delivery state

    __table_args__ = (Index("ix_notifications_due", "status", "next_attempt_at"),)


class CheckRow(Base):
    __tablename__ = "checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32))
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32))
    ok: Mapped[bool] = mapped_column(Boolean)
    error_kind: Mapped[str | None] = mapped_column(String(48))

    __table_args__ = (Index("ix_checks_platform_at", "platform", "at"),)


class SettingRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
