"""Delivers queued notifications. The queue is a database table, so nothing is lost across restarts.

Every alert goes to every configured channel (push, email, WhatsApp...). Each channel is tracked
separately: one that succeeded is never sent again, one that failed is retried with backoff. Availability
alerts expire after a few hours because a late 'tickets available' message would be misleading.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from typing import Any

import httpx
from sqlalchemy import select, update

from app.config import AppConfig
from app.database.models import EventRow, NotificationRow
from app.database.repository import Repository
from app.monitor.events import EventType
from app.notifications.base import DeliveryError, Message, Provider
from app.notifications.email import EmailProvider
from app.notifications.ntfy import NtfyProvider
from app.notifications.pushover import PushoverProvider
from app.notifications.telegram import TelegramProvider
from app.notifications.whatsapp import CallMeBotProvider, WhatsAppCloudProvider
from app.utils.retry import backoff_delay
from app.utils.time import utcnow

logger = logging.getLogger("watcher.notify")

EXPIRY = dt.timedelta(hours=6)
ALARM_EXPIRY = dt.timedelta(minutes=3)
BACKOFF_BASE, BACKOFF_CAP = 5.0, 300.0
CLAIM_SECONDS = 90  # another process will not pick up a row while this one is sending it
DONE = "DELIVERED"
SKIPPED = "SKIPPED"  # channel left out of this message on purpose


def build_providers(cfg: AppConfig, client: httpx.AsyncClient) -> list[Provider]:
    s, n = cfg.secrets, cfg.notifications
    made: dict[str, Provider] = {}
    if n.telegram.enabled:
        made["telegram"] = TelegramProvider(client, s.telegram_bot_token, s.telegram_chat_id)
    if n.pushover.enabled:
        made["pushover"] = PushoverProvider(client, s.pushover_app_token, s.pushover_user_key)
    if n.ntfy.enabled:
        made["ntfy"] = NtfyProvider(client, s.ntfy_server, s.ntfy_topic, s.ntfy_token)
    if n.email.enabled:
        made["email"] = EmailProvider(
            s.smtp_host, s.smtp_port, s.smtp_security, s.smtp_username, s.smtp_password, s.email_from, s.email_to
        )
    if n.whatsapp.enabled:
        made["whatsapp"] = CallMeBotProvider(client, s.callmebot_phone, s.callmebot_apikey)
    if n.whatsapp_cloud.enabled:
        made["whatsapp_cloud"] = WhatsAppCloudProvider(
            client, s.wa_cloud_token, s.wa_cloud_phone_number_id, s.wa_cloud_to, s.wa_cloud_template, s.wa_cloud_language,
            s.wa_cloud_api_version,
        )
    ordered = [made.pop(name) for name in n.order if name in made]
    return ordered + list(made.values())


class Dispatcher:
    def __init__(
        self, repo: Repository, providers: list[Provider], max_attempts: int = 60, runner: str = "main", ack_url: str = ""
    ) -> None:
        self.repo = repo
        self.ack_url = ack_url
        self.providers = providers
        self.max_attempts = max_attempts
        self.runner = runner
        self._wake = asyncio.Event()
        self.all_failed_streak = 0  # attempts in a row where no channel worked
        self.channel_streak: dict[str, int] = {}  # per channel, failures in a row
        self.last_tick = time.monotonic()

    @property
    def consecutive_failures(self) -> int:
        return self.all_failed_streak

    @property
    def failing_channels(self) -> list[str]:
        return [name for name, streak in self.channel_streak.items() if streak >= 3]

    def wake(self) -> None:
        self._wake.set()

    async def enqueue(
        self, kind: str, title: str, body: str, dedupe_key: str, url: str | None = None, only: list[str] | None = None
    ) -> bool:
        """Queue a message for every channel, or with `only`, for those channels (the rest are marked skipped)."""
        channels: dict[str, Any] = {}
        if only is not None:
            channels = {p.name: {"status": SKIPPED} for p in self.providers if p.name not in only}
            if len(channels) == len(self.providers):
                return False  # none of the chosen channels is set up
        async with self.repo.transaction() as s:
            if await s.scalar(select(NotificationRow.id).where(NotificationRow.dedupe_key == dedupe_key)):
                return False
            s.add(NotificationRow(kind=kind, title=title, body=body, url=url, dedupe_key=dedupe_key, channels=channels))
        self.wake()
        return True

    async def run(self) -> None:
        while True:
            self.last_tick = time.monotonic()
            try:
                delivered_any = await self.process_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the notifier must outlive a database hiccup
                logger.exception("notification queue error")
                delivered_any = False
                await asyncio.sleep(2)
            if delivered_any:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 2)
            self._wake.clear()

    async def pending_count(self) -> int:
        async with self.repo.transaction() as s:
            rows = await s.scalars(select(NotificationRow.id).where(NotificationRow.status == "PENDING"))
            return len(list(rows))

    async def process_due(self) -> bool:
        """Attempt everything that is due. Returns True if any channel delivered anything."""
        async with self.repo.transaction() as s:
            query = (
                select(NotificationRow.id)
                .where(NotificationRow.status == "PENDING", NotificationRow.next_attempt_at <= utcnow())
                .order_by(NotificationRow.kind != "ticket", NotificationRow.id)
                .limit(20)
            )
            due = list((await s.scalars(query)).all())
        delivered = False
        for row_id in due:
            row = await self._claim(row_id)
            if row is not None:
                delivered = await self._attempt(row) or delivered
        return delivered

    async def _claim(self, row_id: int) -> NotificationRow | None:
        """Take a short lease on a row so two processes sharing a database never send it twice."""
        now = utcnow()
        async with self.repo.transaction() as s:
            result = await s.execute(
                update(NotificationRow)
                .where(NotificationRow.id == row_id, NotificationRow.status == "PENDING", NotificationRow.next_attempt_at <= now)
                .values(next_attempt_at=now + dt.timedelta(seconds=CLAIM_SECONDS))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                return None
            return await s.get(NotificationRow, row_id)

    def _message(self, row: NotificationRow) -> Message:
        body = row.body if self.runner == "main" else f"{row.body}\nSent by: {self.runner} watcher"
        ack_url = (self.ack_url or None) if row.kind == "alarm" else None
        return Message(title=row.title, body=body, url=row.url, kind=row.kind, ack_url=ack_url)

    async def _attempt(self, row: NotificationRow) -> bool:
        channels: dict[str, Any] = dict(row.channels or {})
        # A repeat that could not go out in time is replaced by the next one rather than delivered late.
        if utcnow() - row.created_at > (ALARM_EXPIRY if row.kind == "alarm" else EXPIRY):
            await self._finish(row, channels, error=row.last_error or "not delivered in time", expired=True)
            return False
        if not self.providers:
            await self._reschedule(row, channels, "no notification provider is configured", delay=60, count=False)
            return False
        message = self._message(row)
        todo = [p for p in self.providers if (channels.get(p.name) or {}).get("status") not in (DONE, SKIPPED)]
        results = await asyncio.gather(*(self._send(p, message) for p in todo))
        delivered_now = False
        errors: list[str] = []
        permanent_only = True
        for provider, (ok, error, permanent, took_ms) in zip(todo, results, strict=True):
            if ok:
                delivered_now = True
                channels[provider.name] = {"status": DONE, "at": utcnow().isoformat(), "ms": took_ms}
                self.channel_streak[provider.name] = 0
            else:
                channels[provider.name] = {"status": "FAILED", "error": error, "at": utcnow().isoformat()}
                self.channel_streak[provider.name] = self.channel_streak.get(provider.name, 0) + 1
                errors.append(f"{provider.name}: {error}")
                permanent_only = permanent_only and permanent
        if any((c or {}).get("status") == DONE for c in channels.values()):
            self.all_failed_streak = 0
        elif todo:
            self.all_failed_streak += 1
        if not errors:
            await self._finish(row, channels)
            return delivered_now
        error = "; ".join(errors)
        logger.error("notification delivery failed: %s", error, extra={"data": {"notification_id": row.id}})
        if row.attempts + 1 >= self.max_attempts:
            await self._finish(row, channels, error=error)
        else:
            # A rejected token may be fixed in .env, so keep trying, only less often.
            delay = BACKOFF_CAP if permanent_only else backoff_delay(row.attempts + 1, BACKOFF_BASE, BACKOFF_CAP)
            await self._reschedule(row, channels, error, delay=delay)
        return delivered_now

    @staticmethod
    async def _send(provider: Provider, message: Message) -> tuple[bool, str, bool, int]:
        started = time.monotonic()
        try:
            await asyncio.wait_for(provider.send(message), 20)
        except DeliveryError as exc:
            return False, str(exc), exc.permanent, 0
        except TimeoutError:
            return False, "timed out", False, 0
        except Exception as exc:  # noqa: BLE001 - one broken channel must not stop the others
            return False, type(exc).__name__, False, 0
        return True, "", False, round((time.monotonic() - started) * 1000)

    async def _reschedule(self, row: NotificationRow, channels: dict[str, Any], error: str, delay: float, count: bool = True) -> None:
        async with self.repo.transaction() as s:
            fresh = await s.get(NotificationRow, row.id)
            if fresh is None:
                return
            fresh.attempts += 1 if count else 0
            fresh.last_error = error
            fresh.channels = channels
            self._stamp_delivery(fresh, channels)
            fresh.next_attempt_at = utcnow() + dt.timedelta(seconds=delay)

    @staticmethod
    def _stamp_delivery(row: NotificationRow, channels: dict[str, Any]) -> None:
        delivered = [(name, c) for name, c in channels.items() if (c or {}).get("status") == DONE]
        if not delivered:
            return
        row.provider = ", ".join(name for name, _ in delivered)
        if row.delivered_at is None:  # the first channel to arrive is what latency measures
            first = min(delivered, key=lambda item: item[1]["at"])
            row.delivered_at = dt.datetime.fromisoformat(first[1]["at"])
            row.send_ms = first[1].get("ms")

    async def _finish(self, row: NotificationRow, channels: dict[str, Any], error: str | None = None, expired: bool = False) -> None:
        async with self.repo.transaction() as s:
            fresh = await s.get(NotificationRow, row.id)
            if fresh is None:
                return
            fresh.channels = channels
            self._stamp_delivery(fresh, channels)
            some = fresh.provider is not None
            everything = bool(channels) and all((c or {}).get("status") in (DONE, SKIPPED) for c in channels.values())
            if everything and not error:
                status = "DELIVERED"
            elif some:
                status = "PARTIAL"
            else:
                status = "EXPIRED" if expired else "FAILED"
            fresh.status, fresh.last_error = status, error
            if not expired:
                fresh.attempts += 1
            label = {
                "DELIVERED": f"delivered via {fresh.provider}",
                "PARTIAL": f"delivered via {fresh.provider} only",
                "FAILED": "failed on every channel",
                "EXPIRED": "expired undelivered",
            }[status]
            s.add(
                EventRow(
                    type=EventType.NOTIFICATION_DELIVERED if some else EventType.NOTIFICATION_FAILED,
                    message=f"Notification {label}: {fresh.title}",
                    details={"notification_id": fresh.id, "kind": fresh.kind, "error": error, "channels": channels},
                )
            )
