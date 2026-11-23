"""Independent platform workers, plus a supervisor that restarts any worker that dies or stalls."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import MonitoringConfig, PlatformConfig
from app.domain import POLICY_KINDS, ErrorKind, PlatformResult, State
from app.monitor.engine import Engine
from app.platforms.base import BookingPlatform
from app.utils.logging import log
from app.utils.retry import CircuitBreaker, backoff_delay
from app.utils.time import utcnow

logger = logging.getLogger("watcher.scheduler")

POLICY_RECHECK_SECONDS = 3600.0
# First retry delay after a failure. A refusal is treated with much more patience than a blip.
BACKOFF_BASE = {ErrorKind.ACCESS_DENIED: 120.0, ErrorKind.RATE_LIMITED: 120.0, ErrorKind.SERVER_ERROR: 30.0}


class CheckRefused(Exception):
    pass


class PlatformWorker:
    def __init__(self, platform: BookingPlatform, engine: Engine, cfg: PlatformConfig, mon: MonitoringConfig) -> None:
        self.platform = platform
        self.engine = engine
        self.mon = mon
        self.interval = float(cfg.interval_seconds or mon.default_interval_seconds)
        self.breaker = CircuitBreaker(mon.breaker_threshold, mon.breaker_cooldown_seconds)
        self.last_started: float | None = None
        self.last_progress = time.monotonic()
        self.next_check_at: dt.datetime | None = None
        self.blocked_until = 0.0  # monotonic; set from Retry-After and access denials
        self.last_result: PlatformResult | None = None
        self.checking = False
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.platform.name

    async def run_once(self) -> PlatformResult:
        """One check. Never raises for a platform problem: every failure becomes a PLATFORM_ERROR result."""
        async with self._lock:
            self.checking = True
            self.last_started = started = time.monotonic()
            checked_at = utcnow()
            try:
                result = await asyncio.wait_for(
                    self.platform.check_availability(self.engine.target), self.mon.check_timeout_seconds
                )
            except TimeoutError:
                result = self.platform.error(ErrorKind.TIMEOUT, f"check exceeded {self.mon.check_timeout_seconds}s")
                await self._safe_reset()
            except asyncio.CancelledError:
                self.checking = False
                raise
            except Exception as exc:  # noqa: BLE001 - isolation boundary between platforms
                logger.exception("unhandled error in %s", self.name)
                result = self.platform.error(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}"[:300])
                await self._safe_reset()
            result.duration_ms = round((time.monotonic() - started) * 1000)
            # For detection latency the moment of knowledge is the end of the check, not its start.
            result.checked_at = utcnow() if result.ok else checked_at
            self.last_progress = time.monotonic()
            self.checking = False

        if result.ok:
            self.breaker.record_success()
            self.blocked_until = 0.0
        elif result.error_kind not in POLICY_KINDS:
            self.breaker.record_failure()
        result.state = await self.engine.process(result)
        self.last_result = result
        log(
            logger,
            logging.INFO if result.ok or result.error_kind in POLICY_KINDS else logging.WARNING,
            f"check {self.name}: {result.state.value}" + (f" ({result.error_kind})" if result.error_kind else ""),
            duration_ms=result.duration_ms,
            detail=result.error_detail,
        )
        return result

    async def _safe_reset(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.platform.reset(), 20)

    def next_delay(self, result: PlatformResult) -> float:
        jitter = random.uniform(1 - self.mon.jitter_ratio, 1 + self.mon.jitter_ratio)
        if result.error_kind in POLICY_KINDS:
            return POLICY_RECHECK_SECONDS * jitter
        if result.ok:
            interval = self.interval
            if self.mon.adaptive_polling and result.state is State.UNVERIFIED and self.interval < 120:
                interval = max(30.0, self.interval / 2)  # something is there but unproven: look again sooner
            # Jitter only ever lengthens the wait, so the configured interval is a true minimum.
            return interval * max(1.0, jitter)
        base = BACKOFF_BASE.get(result.error_kind or ErrorKind.INTERNAL, 10.0)
        delay = backoff_delay(max(1, self.breaker.failures), base, self.mon.max_backoff_seconds)
        if result.retry_after:
            delay = max(delay, result.retry_after)
        if result.error_kind in (ErrorKind.RATE_LIMITED, ErrorKind.ACCESS_DENIED):
            self.blocked_until = time.monotonic() + delay
        return max(delay, self.breaker.seconds_until_probe())

    def request_check_now(self) -> None:
        now = time.monotonic()
        if self.checking:
            raise CheckRefused("a check is already running")
        if self.last_result and self.last_result.error_kind in POLICY_KINDS:
            raise CheckRefused(self.last_result.error_detail or "automated checks are not permitted on this platform")
        if now < self.blocked_until:
            raise CheckRefused(f"the platform asked us to wait {round(self.blocked_until - now)}s")
        if not self.breaker.allows():
            raise CheckRefused(f"circuit breaker open for {round(self.breaker.seconds_until_probe())}s")
        gap = self.mon.check_now_min_gap_seconds
        if self.last_started is not None and now - self.last_started < gap:
            raise CheckRefused(f"last check was {round(now - self.last_started)}s ago; minimum gap is {gap}s")
        self._wake.set()

    async def loop(self, running: asyncio.Event) -> None:
        # Spread the first checks so three workers do not start in the same instant.
        await asyncio.sleep(random.uniform(0, 2))
        while True:
            self.last_progress = time.monotonic()
            if not running.is_set():
                self.next_check_at = None
                await running.wait()
            result = await self.run_once()
            delay = self.next_delay(result)
            self.next_check_at = utcnow() + dt.timedelta(seconds=delay)
            deadline = time.monotonic() + delay
            while (left := deadline - time.monotonic()) > 0 and running.is_set():
                self.last_progress = time.monotonic()  # sleeping on purpose counts as progress
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), min(left, 5))
                if self._wake.is_set():
                    break
            self._wake.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval,
            "next_check_at": self.next_check_at,
            "breaker": self.breaker.state,
            "checking": self.checking,
        }


class Supervisor:
    """Keeps every worker alive. A crash or stall in one worker never touches the others."""

    def __init__(
        self,
        workers: list[PlatformWorker],
        mon: MonitoringConfig,
        on_restart: Callable[[str, str], Awaitable[None]],
        restart_delay: float = 5.0,
    ) -> None:
        self.workers = workers
        self.mon = mon
        self.on_restart = on_restart
        self.restart_delay = restart_delay
        self.running = asyncio.Event()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.last_tick = time.monotonic()

    def start(self) -> None:
        for worker in self.workers:
            self._spawn(worker)

    def _spawn(self, worker: PlatformWorker) -> None:
        worker.last_progress = time.monotonic()
        self.tasks[worker.name] = asyncio.create_task(worker.loop(self.running), name=f"worker-{worker.name}")

    async def tick(self) -> None:
        """Called every few seconds by the application."""
        self.last_tick = time.monotonic()
        stall = self.mon.check_timeout_seconds + 60
        for worker in self.workers:
            task = self.tasks.get(worker.name)
            reason = None
            if task is None or task.done():
                exc = task.exception() if task and not task.cancelled() else None
                reason = f"worker crashed: {exc!r}" if exc else "worker stopped"
            elif self.running.is_set() and time.monotonic() - worker.last_progress > stall:
                reason = f"worker made no progress for {stall}s"
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(task), 10)
            if reason:
                logger.error("%s: %s; restarting", worker.name, reason)
                await self.on_restart(worker.name, reason)
                await asyncio.sleep(self.restart_delay)
                self._spawn(worker)

    async def stop(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for worker in self.workers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(worker.platform.close(), 15)
