"""The running watcher: owns the database, workers, notifier and maintenance loop."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from typing import Any

import httpx

from app.config import AppConfig
from app.database.models import PlatformRow
from app.database.repository import Repository
from app.domain import POLICY_KINDS, ErrorKind, PlatformResult, SeatStatus, State, Target
from app.monitor.engine import Engine
from app.monitor.events import PLATFORM_NAMES, EventType
from app.notifications.base import Provider
from app.notifications.dispatcher import Dispatcher, build_providers
from app.platforms.base import BookingPlatform
from app.platforms.bookmyshow import BookMyShowPlatform
from app.platforms.district import DistrictPlatform
from app.platforms.pvr import PvrPlatform
from app.scheduler import PlatformWorker, Supervisor
from app.utils.time import IST, fmt_time, to_ist, utcnow

logger = logging.getLogger("watcher.service")

STARTUP_GRACE = 150.0
ALL_FAILING_AFTER = 600.0


def build_platforms(cfg: AppConfig) -> list[BookingPlatform]:
    platforms: list[BookingPlatform] = []
    if cfg.platforms.pvr.enabled:
        platforms.append(PvrPlatform(cfg.platforms.pvr, cfg.aliases))
    if cfg.platforms.bookmyshow.enabled:
        platforms.append(BookMyShowPlatform(cfg.platforms.bookmyshow))
    if cfg.platforms.district.enabled:
        platforms.append(DistrictPlatform(cfg.platforms.district))
    return platforms


class Watcher:
    def __init__(
        self, cfg: AppConfig, platforms: list[BookingPlatform] | None = None, providers: list[Provider] | None = None
    ) -> None:
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.repo = Repository(cfg.db_url)
        self._http = httpx.AsyncClient(timeout=12)
        chosen = build_providers(cfg, self._http) if providers is None else providers
        self.dispatcher = Dispatcher(self.repo, chosen, cfg.notifications.max_attempts, cfg.runner)
        self.engine = Engine(self.repo, cfg.target, cfg.aliases, self.dispatcher.wake)
        self.platforms = build_platforms(cfg) if platforms is None else platforms
        self.workers = {
            p.name: PlatformWorker(p, self.engine, getattr(cfg.platforms, p.name), cfg.monitoring) for p in self.platforms
        }
        self.supervisor = Supervisor(list(self.workers.values()), cfg.monitoring, self._worker_restarted)
        self.started_at = utcnow()
        self._started_mono = time.monotonic()
        self._tasks: list[asyncio.Task[None]] = []
        self._all_failing_since: float | None = None
        self._last_heartbeat = 0.0
        self._last_prune = 0.0
        self.db_ok = True
        self.finished = False

    # -- lifecycle --------------------------------------------------------------------------------

    async def start(self, background: bool = True) -> None:
        """Open the database and, with background=True, start the workers and the notifier."""
        await self.repo.start(
            {pid: (name, pid in self.workers) for pid, name in PLATFORM_NAMES.items()}
        )
        if saved := await self.repo.get_setting("target_override"):
            self.engine.set_target(Target.model_validate({**self.cfg.target.model_dump(), **saved}))
        unclean = await self.repo.get_setting("clean_shutdown") is False
        await self.repo.set_setting("clean_shutdown", False)
        await self.repo.add_event(EventType.APP_STARTED, "Watcher started")
        if unclean:
            await self.repo.add_event(EventType.SYSTEM, "The previous run ended without a clean shutdown")
        if unclean and background:
            await self._system_alert("restart", "Watcher restarted", "It stopped unexpectedly and is running again.")
        if self.cfg.monitoring.enabled and not await self.repo.get_setting("paused", False):
            self.supervisor.running.set()
        if not background:
            return
        self.supervisor.start()
        self._tasks = [
            asyncio.create_task(self._maintenance(), name="maintenance"),
            asyncio.create_task(self.dispatcher.run(), name="dispatcher"),
        ]

    async def run_once(self, drain_seconds: float = 75.0) -> int:
        """One check of every platform, then deliver what it produced. For scheduled runners.

        Returns 1 when every automated platform failed, so the scheduler marks the run as failed
        (GitHub emails the repository owner about failed scheduled runs).
        """
        if not self.supervisor.running.is_set() or self._target_passed():
            return 0
        async def check_then_send(worker: PlatformWorker) -> PlatformResult:
            # Send as soon as this platform's check is stored, without waiting for slower platforms.
            # The outbox claim keeps concurrent sends from delivering a row twice.
            result = await worker.run_once()
            await self.dispatcher.process_due()
            return result

        results = await asyncio.gather(*(check_then_send(w) for w in self.workers.values()))
        deadline = time.monotonic() + drain_seconds
        while time.monotonic() < deadline:
            await self.dispatcher.process_due()
            if not await self.dispatcher.pending_count():
                break
            await asyncio.sleep(3)
        automated = [r for r in results if r.error_kind not in POLICY_KINDS]
        all_failed = not automated or not any(r.ok for r in automated)
        if not all_failed and self.dispatcher.all_failed_streak < 3:
            await self._heartbeat("healthy")
        await self.repo.prune()
        return 1 if all_failed else 0

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.supervisor.stop()
        with contextlib.suppress(Exception):
            await self.repo.add_event(EventType.APP_STOPPED, "Watcher stopped")
            await self.repo.set_setting("clean_shutdown", True)
        await self._http.aclose()
        await self.repo.close()

    # -- control ----------------------------------------------------------------------------------

    async def set_running(self, running: bool) -> None:
        if running == self.supervisor.running.is_set():
            return
        if running:
            if self._target_passed():
                raise ValueError("the target show window has passed")
            self.supervisor.running.set()
        else:
            self.supervisor.running.clear()
        await self.repo.set_setting("paused", not running)
        await self.repo.add_event(
            EventType.MONITORING_STARTED if running else EventType.MONITORING_PAUSED,
            "Monitoring started" if running else "Monitoring paused",
        )

    async def update_target(self, changes: dict[str, Any]) -> Target:
        """Only fields that do not depend on verified venue and film identifiers can change at runtime."""
        allowed = {"date", "language", "earliest_time", "latest_time"}
        if extra := set(changes) - allowed:
            raise ValueError(f"not editable here: {', '.join(sorted(extra))}")
        target = Target.model_validate({**self.engine.target.model_dump(), **changes})
        old = self.engine.target
        self.engine.set_target(target)
        self.finished = False
        saved = {k: v for k, v in target.model_dump(mode="json").items() if k in allowed}
        await self.repo.set_setting("target_override", saved)
        await self.repo.add_event(
            EventType.TARGET_CHANGED,
            "Target changed",
            before=old.model_dump(mode="json"),
            after=target.model_dump(mode="json"),
        )
        return target

    async def test_notification(self) -> None:
        stamp = utcnow()
        await self.dispatcher.enqueue(
            "test",
            "Doomsday Watcher test",
            f"Notifications work. Sent {fmt_time(to_ist(stamp).time())} IST. This is not a ticket alert.",
            f"test-{stamp.timestamp()}",
            self.cfg.platforms.pvr.movie_url,
        )

    # -- background -------------------------------------------------------------------------------

    async def _worker_restarted(self, name: str, reason: str) -> None:
        await self.repo.add_event(EventType.SYSTEM, f"{PLATFORM_NAMES[name]} worker restarted: {reason}", platform=name)

    async def _system_alert(self, kind: str, title: str, body: str) -> None:
        """Health alerts are separate from ticket alerts and limited to one per kind per six hours."""
        if not self.cfg.notifications.system_alerts:
            return
        bucket = int(utcnow().timestamp() // 21600)
        await self.dispatcher.enqueue("system", f"WATCHER HEALTH: {title}", body, f"system-{kind}-{bucket}")

    def _target_passed(self) -> bool:
        target = self.engine.target
        return dt.datetime.now(IST) > dt.datetime.combine(target.date, target.latest_time, IST)

    async def _maintenance(self) -> None:
        while True:
            try:
                await self._maintenance_step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - this loop is what keeps everything else alive
                logger.exception("maintenance error")
            await asyncio.sleep(5)

    async def _maintenance_step(self) -> None:
        await self.supervisor.tick()
        if len(self._tasks) > 1 and self._tasks[1].done():
            logger.error("notification loop stopped; restarting")
            self._tasks[1] = asyncio.create_task(self.dispatcher.run(), name="dispatcher")
        try:
            await self.repo.ping()
            if not self.db_ok:
                logger.warning("database recovered")
            self.db_ok = True
        except Exception:
            self.db_ok = False
            raise
        if not self.finished and self._target_passed() and self.supervisor.running.is_set():
            self.finished = True
            self.supervisor.running.clear()
            await self.repo.add_event(EventType.MONITORING_PAUSED, "The target show window has passed; monitoring stopped")
        overall = (await self.health())["status"]
        await self._alert_if_all_failing(overall)
        await self._heartbeat(overall)
        await self._reminders()
        if time.monotonic() - self._last_prune > 21600 or not self._last_prune:
            self._last_prune = time.monotonic()
            await self.repo.prune()

    async def _alert_if_all_failing(self, overall: str) -> None:
        if overall != "down":
            self._all_failing_since = None
            return
        now = time.monotonic()
        self._all_failing_since = self._all_failing_since or now
        if now - self._all_failing_since >= ALL_FAILING_AFTER:
            await self._system_alert(
                "all-failing",
                "no platform can be checked",
                "Every automated check has been failing for 10+ minutes. Check the booking sites yourself until it recovers.",
            )

    async def _heartbeat(self, overall: str) -> None:
        url = self.cfg.secrets.heartbeat_url
        if not url or time.monotonic() - self._last_heartbeat < 60:
            return
        # Withheld when anything essential is broken, so the external monitor raises the alarm instead of us.
        if overall == "down" or self.dispatcher.consecutive_failures >= 3:
            return
        self._last_heartbeat = time.monotonic()
        with contextlib.suppress(httpx.HTTPError):
            await self._http.get(url)

    async def _reminders(self) -> None:
        bms = self.cfg.platforms.bookmyshow
        now = to_ist(utcnow())
        if not bms.enabled or self.finished or f"{now:%H:%M}" not in bms.reminder_times_ist:
            return
        await self.dispatcher.enqueue(
            "reminder",
            "Check BookMyShow yourself",
            "BookMyShow cannot be checked automatically. This is a scheduled reminder, not a ticket alert.",
            f"reminder-bms-{now:%Y-%m-%d-%H:%M}",
            bms.movie_url,
        )

    # -- status -----------------------------------------------------------------------------------

    def _fresh(self, row: PlatformRow, worker: PlatformWorker) -> bool:
        limit = max(self.cfg.monitoring.stale_after_seconds, worker.interval * 3)
        return row.last_success is not None and (utcnow() - row.last_success).total_seconds() <= limit

    @staticmethod
    def _is_policy(row: PlatformRow) -> bool:
        return row.error_kind in {k.value for k in POLICY_KINDS}

    def _platform_health(self, row: PlatformRow, worker: PlatformWorker | None) -> str:
        if worker is None:
            return "disabled"
        if self._is_policy(row):
            return "not_permitted" if row.error_kind == ErrorKind.ACCESS_NOT_PERMITTED else "awaiting_decision"
        if row.error_kind == ErrorKind.RATE_LIMITED:
            return "rate_limited"
        if row.error_kind:
            return "unavailable"
        if row.last_success is None:
            return "starting"
        if not self._fresh(row, worker):
            return "stale"
        return {
            State.AVAILABLE.value: "match_available",
            State.UNVERIFIED.value: "unverified",
        }.get(row.state, "no_match")

    async def health(self) -> dict[str, Any]:
        rows = {r.id: r for r in await self.repo.platforms()}
        automated = [n for n in self.workers if n in rows and not self._is_policy(rows[n])]
        fresh = [n for n in automated if self._fresh(rows[n], self.workers[n])]
        uptime = time.monotonic() - self._started_mono
        running = self.supervisor.running.is_set()
        if not running:
            status = "finished" if self.finished else "paused"
        elif (
            fresh
            and len(fresh) == len(automated)
            and self.dispatcher.providers
            and not self.dispatcher.failing_channels
        ):
            status = "healthy"
        elif fresh:
            status = "degraded"
        elif uptime < STARTUP_GRACE:
            status = "starting"
        else:
            status = "down"
        if not self.db_ok or time.monotonic() - self.supervisor.last_tick > 60 + self.cfg.monitoring.check_timeout_seconds:
            status = "down"
        return {
            "status": status,
            "checking": fresh,
            "failing": [n for n in automated if n not in fresh],
            "not_automated": [n for n in rows if n not in automated],
            "notifications_configured": bool(self.dispatcher.providers),
            "failing_channels": self.dispatcher.failing_channels,
            "database_ok": self.db_ok,
            "uptime_seconds": round(uptime),
        }

    async def status(self) -> dict[str, Any]:
        rows = {r.id: r for r in await self.repo.platforms()}
        metrics = await self.repo.metrics()
        now = utcnow()
        stale_after = self.cfg.monitoring.stale_after_seconds
        urls = {
            "pvr": self.cfg.platforms.pvr.movie_url,
            "bookmyshow": self.cfg.platforms.bookmyshow.movie_url,
            "district": self.cfg.platforms.district.movie_url,
        }
        platforms = []
        for pid, name in PLATFORM_NAMES.items():
            row, worker = rows.get(pid), self.workers.get(pid)
            if row is None:
                continue
            platforms.append(
                {
                    "id": pid,
                    "name": name,
                    "health": self._platform_health(row, worker),
                    "state": row.state,
                    "last_good_state": row.last_good_state,
                    "error_kind": row.error_kind,
                    "error_detail": row.error_detail,
                    "last_attempt": row.last_attempt,
                    "last_success": row.last_success,
                    "failure_count": row.failure_count,
                    "recoveries": row.recoveries,
                    "signals": row.signals,
                    "url": urls[pid],
                    "metrics": metrics["platforms"].get(pid),
                    **(worker.snapshot() if worker else {}),
                }
            )
        target = self.engine.target
        screenings = []
        for s in await self.repo.screenings():
            current = s.date == target.date and target.earliest_time <= s.showtime <= target.latest_time
            age = (now - s.last_verified).total_seconds()
            snap = await self.repo.latest_snapshot(s.id)
            seats_age = (now - s.seats_at).total_seconds() if s.seats_at else None
            screenings.append(
                {
                    "id": s.id,
                    "entity": s.entity_key,
                    "platform": s.platform,
                    "platform_name": PLATFORM_NAMES[s.platform],
                    "cinema": s.cinema,
                    "date": s.date,
                    "showtime": fmt_time(s.showtime),
                    "language": s.language,
                    "format": s.format,
                    "availability": s.availability,
                    "stale": age > stale_after,
                    "currently_available": s.availability == State.AVAILABLE.value and age <= stale_after and current,
                    "in_target": current,
                    "seat_status": s.seat_status if s.seat_status != SeatStatus.UNKNOWN.value else None,
                    "seats_available": s.seats_available,
                    "seats_at": s.seats_at,
                    "seats_stale": seats_age is not None and seats_age > stale_after + self.cfg.platforms.pvr.seat_reverify_seconds,
                    "seat_categories": (snap.seats or {}).get("categories", []) if snap else [],
                    "booking_url": s.booking_url,
                    "first_detected": s.first_detected,
                    "last_verified": s.last_verified,
                    "evidence": s.evidence,
                }
            )
        successes = [r.last_success for r in rows.values() if r.last_success]
        health = await self.health()
        return {
            "now": now,
            "overall": health,
            "running": self.supervisor.running.is_set(),
            "started_at": self.started_at,
            "last_success": max(successes) if successes else None,
            "target": target.model_dump(mode="json"),
            "platforms": platforms,
            "screenings": screenings,
            "matches": sum(1 for s in screenings if s["currently_available"]),
            "matches_ever": sum(1 for s in screenings if s["in_target"]),
            "providers": [p.name for p in self.dispatcher.providers],
            "auth_required": bool(self.cfg.secrets.dashboard_password),
            "metrics": metrics,
            "stale_after_seconds": stale_after,
        }
