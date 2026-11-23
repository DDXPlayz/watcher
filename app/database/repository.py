"""Database access. Writers use `transaction()`; everything inside commits or rolls back together."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import delete, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.migrations import migrate
from app.database.models import (
    CheckRow,
    EventRow,
    NotificationRow,
    PlatformRow,
    ScreeningRow,
    SeatSnapshotRow,
    SettingRow,
)
from app.utils.time import utcnow


class Repository:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragmas(dbapi_conn: Any, _: Any) -> None:
                cursor = dbapi_conn.cursor()
                for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=10000", "synchronous=NORMAL"):
                    cursor.execute(f"PRAGMA {pragma}")
                cursor.close()

        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def start(self, platforms: dict[str, tuple[str, bool]]) -> None:
        await migrate(self.engine)
        async with self.transaction() as s:
            for pid, (name, enabled) in platforms.items():
                row = await s.get(PlatformRow, pid)
                if row is None:
                    s.add(PlatformRow(id=pid, name=name, enabled=enabled, signals={}))
                else:
                    row.name, row.enabled = name, enabled

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session, session.begin():
            yield session

    async def ping(self) -> None:
        async with self._sessions() as s:
            await s.execute(text("SELECT 1"))

    # -- settings ---------------------------------------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self._sessions() as s:
            row = await s.get(SettingRow, key)
            return default if row is None else row.value

    async def set_setting(self, key: str, value: Any) -> None:
        async with self.transaction() as s:
            await s.merge(SettingRow(key=key, value=value))

    # -- events -----------------------------------------------------------------------------------

    async def add_event(self, type_: str, message: str, platform: str | None = None, **details: Any) -> int:
        async with self.transaction() as s:
            row = EventRow(type=type_, message=message, platform=platform, details=details)
            s.add(row)
            await s.flush()
            return row.id

    async def events(self, limit: int = 100, before_id: int | None = None) -> list[EventRow]:
        query = select(EventRow).order_by(EventRow.id.desc()).limit(limit)
        if before_id:
            query = query.where(EventRow.id < before_id)
        async with self._sessions() as s:
            return list((await s.scalars(query)).all())

    async def recent_checks(self, limit: int = 60) -> list[CheckRow]:
        async with self._sessions() as s:
            return list((await s.scalars(select(CheckRow).order_by(CheckRow.id.desc()).limit(limit))).all())

    # -- reads for the dashboard ------------------------------------------------------------------

    async def platforms(self) -> list[PlatformRow]:
        async with self._sessions() as s:
            return list((await s.scalars(select(PlatformRow))).all())

    async def platform(self, pid: str) -> PlatformRow | None:
        async with self._sessions() as s:
            return await s.get(PlatformRow, pid)

    async def screenings(self) -> list[ScreeningRow]:
        async with self._sessions() as s:
            return list((await s.scalars(select(ScreeningRow).order_by(ScreeningRow.date, ScreeningRow.showtime))).all())

    async def latest_snapshot(self, screening_id: int) -> SeatSnapshotRow | None:
        async with self._sessions() as s:
            query = (
                select(SeatSnapshotRow)
                .where(SeatSnapshotRow.screening_id == screening_id)
                .order_by(SeatSnapshotRow.id.desc())
                .limit(1)
            )
            return (await s.scalars(query)).first()

    async def notifications(self, limit: int = 50) -> list[NotificationRow]:
        async with self._sessions() as s:
            return list((await s.scalars(select(NotificationRow).order_by(NotificationRow.id.desc()).limit(limit))).all())

    async def metrics(self) -> dict[str, Any]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(
                        CheckRow.platform,
                        func.count(),
                        func.sum(func.cast(CheckRow.ok, CheckRow.duration_ms.type)),
                        func.avg(CheckRow.duration_ms),
                        func.max(CheckRow.duration_ms),
                    ).group_by(CheckRow.platform)
                )
            ).all()
            day_ago = utcnow() - dt.timedelta(days=1)
            day = (
                await s.execute(
                    select(CheckRow.platform, func.count(), func.sum(func.cast(CheckRow.ok, CheckRow.duration_ms.type)))
                    .where(CheckRow.at >= day_ago)
                    .group_by(CheckRow.platform)
                )
            ).all()
            delivered = list(
                (
                    await s.scalars(
                        select(NotificationRow).where(NotificationRow.delivered_at.is_not(None)).order_by(NotificationRow.id.desc()).limit(200)
                    )
                ).all()
            )
            delivered_count = await s.scalar(
                select(func.count()).select_from(NotificationRow).where(NotificationRow.status.in_(["DELIVERED", "PARTIAL"]))
            )
            failed_count = await s.scalar(select(func.count()).select_from(NotificationRow).where(NotificationRow.status == "FAILED"))
            pending = await s.scalar(select(func.count()).select_from(NotificationRow).where(NotificationRow.status == "PENDING"))
        day_map = {p: (n, int(ok or 0)) for p, n, ok in day}
        per = {
            p: {
                "checks": n,
                "ok": int(ok or 0),
                "failed": n - int(ok or 0),
                "success_rate": round(int(ok or 0) / n, 4) if n else None,
                "avg_ms": round(avg or 0),
                "max_ms": mx or 0,
                "uptime_24h": round(day_map[p][1] / day_map[p][0], 4) if p in day_map and day_map[p][0] else None,
            }
            for p, n, ok, avg, mx in rows
        }
        latencies = [
            (n.delivered_at - n.detected_at).total_seconds() * 1000 for n in delivered if n.delivered_at and n.detected_at
        ]
        sends = [n.send_ms for n in delivered if n.send_ms is not None]
        return {
            "platforms": per,
            "total_checks": sum(v["checks"] for v in per.values()),
            "failed_checks": sum(v["failed"] for v in per.values()),
            "notifications_delivered": delivered_count or 0,
            "notifications_failed": failed_count or 0,
            "notifications_pending": pending or 0,
            "detection_to_delivery_ms_avg": round(sum(latencies) / len(latencies)) if latencies else None,
            "detection_to_delivery_ms_max": round(max(latencies)) if latencies else None,
            "send_ms_avg": round(sum(sends) / len(sends)) if sends else None,
        }

    async def prune(self, checks_days: int = 7, events_days: int = 120, snapshots_days: int = 30) -> None:
        now = utcnow()
        async with self.transaction() as s:
            await s.execute(delete(CheckRow).where(CheckRow.at < now - dt.timedelta(days=checks_days)))
            await s.execute(delete(SeatSnapshotRow).where(SeatSnapshotRow.captured_at < now - dt.timedelta(days=snapshots_days)))
            await s.execute(delete(EventRow).where(EventRow.at < now - dt.timedelta(days=events_days)))
