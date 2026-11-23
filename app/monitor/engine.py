"""Turns a platform result into state, events and queued notifications, in one database transaction."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Aliases
from app.database.models import (
    CheckRow,
    EventRow,
    NotificationRow,
    PlatformRow,
    ScreeningRow,
    SeatSnapshotRow,
)
from app.database.repository import Repository
from app.domain import POLICY_KINDS, Observation, PlatformResult, SeatStatus, State, Target
from app.monitor.deduplication import entity_key, notification_key, screening_key
from app.monitor.events import PLATFORM_NAMES, EventType, in_window, screening_message
from app.monitor.matcher import Decision, Matcher
from app.utils.logging import log
from app.utils.time import fmt_time, utcnow

logger = logging.getLogger("watcher.engine")

MISSES_BEFORE_UNAVAILABLE = 2
SEAT_ALERT_COOLDOWN = dt.timedelta(minutes=30)


class Engine:
    def __init__(self, repo: Repository, target: Target, aliases: Aliases, on_notification: Callable[[], None] = lambda: None) -> None:
        self.repo = repo
        self.aliases = aliases
        self.on_notification = on_notification
        self.matcher = Matcher(target, aliases)

    @property
    def target(self) -> Target:
        return self.matcher.target

    def set_target(self, target: Target) -> None:
        self.matcher = Matcher(target, self.aliases)

    async def process(self, result: PlatformResult) -> State:
        """Record one check. Returns the platform's resulting state."""
        queued = False
        # One matcher for the whole transaction, so a target change cannot land half way through.
        matcher = self.matcher
        async with self.repo.transaction() as s:
            platform = await s.get(PlatformRow, result.platform)
            if platform is None:
                raise LookupError(f"unknown platform {result.platform}")
            platform.last_attempt = result.checked_at
            if result.ok:
                final, queued = await self._process_success(s, platform, result, matcher)
            else:
                final = State.PLATFORM_ERROR
                await self._process_failure(s, platform, result)
            if result.error_kind not in POLICY_KINDS:
                s.add(
                    CheckRow(
                        platform=result.platform,
                        at=result.checked_at,
                        duration_ms=result.duration_ms,
                        state=final.value,
                        ok=result.ok,
                        error_kind=result.error_kind.value if result.error_kind else None,
                    )
                )
            if platform.state != final.value:
                previous, platform.state = platform.state, final.value
                if result.ok:
                    s.add(
                        EventRow(
                            type=EventType.STATE_CHANGED,
                            platform=result.platform,
                            message=f"{PLATFORM_NAMES[result.platform]}: {previous} → {final.value}",
                            details={"from": previous, "to": final.value, "note": result.error_detail},
                        )
                    )
                log(logger, logging.INFO, "platform state changed", platform=result.platform, old=previous, new=final.value)
        if queued:
            self.on_notification()
        return final

    # -- failures ---------------------------------------------------------------------------------

    async def _process_failure(self, s: AsyncSession, platform: PlatformRow, result: PlatformResult) -> None:
        kind = result.error_kind.value if result.error_kind else "INTERNAL"
        policy = result.error_kind in POLICY_KINDS
        if platform.error_kind != kind:  # only the first of a run of identical errors becomes an event
            s.add(
                EventRow(
                    type=EventType.STATE_CHANGED if policy else EventType.PLATFORM_FAILURE,
                    platform=platform.id,
                    message=f"{PLATFORM_NAMES[platform.id]}: {result.error_detail or kind}",
                    details={"error_kind": kind},
                )
            )
        platform.error_kind = kind
        platform.error_detail = result.error_detail
        if not policy:
            platform.failure_count += 1

    # -- successes --------------------------------------------------------------------------------

    async def _process_success(
        self, s: AsyncSession, platform: PlatformRow, result: PlatformResult, matcher: Matcher
    ) -> tuple[State, bool]:
        if platform.error_kind:
            if platform.failure_count:
                platform.recoveries += 1
                s.add(
                    EventRow(
                        type=EventType.PLATFORM_RECOVERED,
                        platform=platform.id,
                        message=f"{PLATFORM_NAMES[platform.id]} recovered after {platform.failure_count} failed checks",
                        details={"previous_error": platform.error_kind},
                    )
                )
            platform.error_kind = platform.error_detail = None
        platform.failure_count = 0
        platform.last_success = result.checked_at
        self._record_signals(s, platform, result)

        queued = False
        seen: set[str] = set()
        states: list[State] = []
        unverified: list[str] = []
        for obs in result.observations:
            decision = matcher.evaluate(obs)
            if decision.state is None:
                continue
            key = screening_key(obs)
            if key in seen:
                continue
            seen.add(key)
            if not decision.identity_match:
                states.append(State.UNVERIFIED)
                unverified.append(key)
                self._note_unverified(s, platform, key, obs, decision)
                continue
            row = await s.scalar(select(ScreeningRow).where(ScreeningRow.key == key))
            if decision.state is State.UNVERIFIED:
                # Listed, but this check could not prove bookability. Known state is kept and goes stale.
                states.append(State.UNVERIFIED)
                unverified.append(key)
                if row is not None:
                    row.missing_count = 0
                self._note_unverified(s, platform, key, obs, decision)
                continue
            state, did_queue = await self._apply(s, row, key, obs, decision, result, matcher.target)
            states.append(state)
            queued = queued or did_queue

        signals = dict(platform.signals or {})
        signals["unverified"] = unverified
        platform.signals = signals

        if result.listing_complete:
            await self._apply_missing(s, platform, seen, matcher.target)

        if State.AVAILABLE in states:
            final = State.AVAILABLE
        elif State.UNVERIFIED in states:
            final = State.UNVERIFIED
        elif State.SOLD_OUT in states:
            final = State.SOLD_OUT
        elif State.UNAVAILABLE in states:
            final = State.UNAVAILABLE
        elif result.state in (State.NOT_YET_LISTED, State.UNVERIFIED):
            final = result.state
        else:
            final = State.UNAVAILABLE if result.listing_complete else State.UNVERIFIED
        platform.last_good_state = final.value
        return final, queued

    def _record_signals(self, s: AsyncSession, platform: PlatformRow, result: PlatformResult) -> None:
        old = dict(platform.signals or {})
        for name, type_, label in (
            ("dates", EventType.NEW_DATE, "new dates listed"),
            ("venue_formats", EventType.NEW_FORMAT, "new formats at the target cinema"),
            ("film_formats", EventType.NEW_FORMAT, "new formats for the film in the city"),
        ):
            if name not in result.signals:
                continue
            fresh = sorted(set(result.signals[name]) - set(old.get(name) or []))
            if fresh and name in old:  # the first ever check is a baseline, not news
                s.add(
                    EventRow(
                        type=type_,
                        platform=platform.id,
                        message=f"{PLATFORM_NAMES[platform.id]}: {label}: {', '.join(fresh)}",
                        details={"added": fresh},
                    )
                )
        platform.signals = {**old, **result.signals}

    def _note_unverified(
        self, s: AsyncSession, platform: PlatformRow, key: str, obs: Observation, decision: Decision
    ) -> None:
        if key in ((platform.signals or {}).get("unverified") or []):
            return
        when = fmt_time(obs.showtime) if obs.showtime else "unknown time"
        s.add(
            EventRow(
                type=EventType.UNVERIFIED_LISTING,
                platform=platform.id,
                message=f"{PLATFORM_NAMES[platform.id]}: possible match at {when} not verified ({', '.join(decision.reasons)})",
                details={"fields": {k: v.value for k, v in decision.fields.items()}, "evidence": obs.evidence},
            )
        )

    async def _apply(
        self,
        s: AsyncSession,
        row: ScreeningRow | None,
        key: str,
        obs: Observation,
        decision: Decision,
        result: PlatformResult,
        target: Target,
    ) -> tuple[State, bool]:
        assert obs.date and obs.showtime and decision.state
        state = decision.state
        previous = row.availability if row else None
        # Sold out before we ever saw it on sale is not the SOLD_OUT transition.
        if state is State.SOLD_OUT and not (row and row.ever_available):
            state = State.UNAVAILABLE
        if row is None:
            row = ScreeningRow(
                key=key,
                entity_key=entity_key(obs),
                platform=obs.platform,
                movie=target.movie,
                cinema=target.cinema,
                date=obs.date,
                language=obs.language or "",
                format=obs.format or "",
                showtime=obs.showtime,
                availability=state.value,
                generation=0,
                missing_count=0,
                ever_available=False,
                seat_status=SeatStatus.UNKNOWN.value,
                first_detected=result.checked_at,
                last_verified=result.checked_at,
            )
            s.add(row)
        row.external_session_id = obs.session_id or row.external_session_id
        row.booking_url = obs.booking_url or row.booking_url
        row.last_verified = result.checked_at
        row.missing_count = 0
        row.evidence = {
            **obs.evidence,
            "movie_as_listed": obs.movie,
            "movie_id": obs.movie_id,
            "cinema_as_listed": obs.cinema,
            "cinema_id": obs.cinema_id,
            "language_as_listed": obs.language,
            "format_as_listed": obs.format,
            "session_id": obs.session_id,
            "verdicts": {k: v.value for k, v in decision.fields.items()},
            "verified_at": result.checked_at.isoformat(),
        }
        previous_band = row.seat_status
        row.availability = state.value
        await s.flush()
        await self._record_seats(s, row, obs)

        kind: EventType | None = None
        notify = False
        if state is State.AVAILABLE and previous != State.AVAILABLE.value:
            row.generation += 1
            kind = EventType.SCREENING_REOPENED if row.ever_available else EventType.SCREENING_AVAILABLE
            row.ever_available = True
            notify = True
        elif state is State.SOLD_OUT and previous == State.AVAILABLE.value:
            kind, notify = EventType.SCREENING_SOLD_OUT, True
        elif state is State.UNAVAILABLE and previous == State.AVAILABLE.value:
            kind = EventType.SCREENING_UNAVAILABLE
        elif previous is None:
            kind = EventType.SCREENING_SEEN
        elif state is State.AVAILABLE and self._band_changed(previous_band, row.seat_status):
            kind = EventType.SEATS_CHANGED
            now = utcnow()
            if row.seat_alert_at is None or now - row.seat_alert_at >= SEAT_ALERT_COOLDOWN:
                row.seat_alert_at, notify = now, True
        if kind is None:
            return state, False

        event = EventRow(
            type=kind,
            platform=obs.platform,
            screening_id=row.id,
            message=f"{PLATFORM_NAMES[obs.platform]}: {fmt_time(obs.showtime)} ScreenX — {kind.value.replace('_', ' ').lower()}",
            details={
                "from": previous,
                "to": state.value,
                "seats": row.seats_available,
                "band_from": previous_band,
                "band_to": row.seat_status,
            },
        )
        s.add(event)
        await s.flush()
        log(logger, logging.INFO, "screening event", type=kind.value, key=key, previous=previous, state=state.value)
        if not notify:
            return state, False
        return state, await self._queue(s, event, row, obs, kind, result.checked_at, target)

    @staticmethod
    def _band_changed(old: str, new: str) -> bool:
        return SeatStatus.UNKNOWN.value not in (old, new) and old != new

    async def _record_seats(self, s: AsyncSession, row: ScreeningRow, obs: Observation) -> None:
        seats = obs.seats
        if seats.status is SeatStatus.UNKNOWN and seats.available is None:
            return
        if row.seats_at is not None and seats.captured_at <= row.seats_at:
            return  # the adapter repeated an earlier snapshot; nothing new was observed
        changed = row.seats_available != seats.available or row.seat_status != seats.status.value
        row.seat_status, row.seats_available, row.seats_at = seats.status.value, seats.available, seats.captured_at
        if changed:
            s.add(
                SeatSnapshotRow(
                    screening_id=row.id,
                    captured_at=seats.captured_at,
                    status=seats.status.value,
                    available=seats.available,
                    total=seats.total,
                    seats={"categories": [c.model_dump() for c in seats.categories], "available_seats": seats.available_seats},
                )
            )

    async def _queue(
        self,
        s: AsyncSession,
        event: EventRow,
        row: ScreeningRow,
        obs: Observation,
        kind: EventType,
        detected_at: dt.datetime,
        target: Target,
    ) -> bool:
        transition = kind.value
        if kind is EventType.SEATS_CHANGED:
            transition += f"@{int(utcnow().timestamp() // SEAT_ALERT_COOLDOWN.total_seconds())}"
        dedupe = notification_key(row.key, transition, row.generation)
        if await s.scalar(select(NotificationRow.id).where(NotificationRow.dedupe_key == dedupe)):
            return False
        also_on = False
        if kind is EventType.SCREENING_AVAILABLE:
            also_on = bool(
                await s.scalar(
                    select(ScreeningRow.id)
                    .where(
                        ScreeningRow.entity_key == row.entity_key,
                        ScreeningRow.id != row.id,
                        ScreeningRow.availability == State.AVAILABLE.value,
                    )
                    .limit(1)
                )
            )
        title, body = screening_message(kind, obs, target, also_on=also_on)
        urgent = kind in (EventType.SCREENING_AVAILABLE, EventType.SCREENING_REOPENED)
        s.add(
            NotificationRow(
                dedupe_key=dedupe,
                event_id=event.id,
                kind="ticket" if urgent else "update",
                title=title,
                body=body,
                url=row.booking_url,
                detected_at=detected_at,
            )
        )
        await s.flush()
        return True

    async def _apply_missing(self, s: AsyncSession, platform: PlatformRow, seen: set[str], target: Target) -> None:
        query = select(ScreeningRow).where(
            ScreeningRow.platform == platform.id,
            ScreeningRow.availability.in_([State.AVAILABLE.value, State.SOLD_OUT.value]),
        )
        for row in (await s.scalars(query)).all():
            if row.key in seen or not in_window(target, row.date, row.showtime):
                continue
            row.missing_count += 1
            if row.missing_count < MISSES_BEFORE_UNAVAILABLE:
                continue
            row.availability = State.UNAVAILABLE.value
            s.add(
                EventRow(
                    type=EventType.SCREENING_UNAVAILABLE,
                    platform=platform.id,
                    screening_id=row.id,
                    message=f"{PLATFORM_NAMES[platform.id]}: {fmt_time(row.showtime)} ScreenX is no longer listed",
                    details={"consecutive_misses": row.missing_count},
                )
            )
