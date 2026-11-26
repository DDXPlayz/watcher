"""The repeating alarm: after a ticket alert, remind every minute until "I've seen it" is pressed."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx

from app.config import AlarmConfig, AppConfig, NotificationsConfig, Secrets
from app.main import create_app
from app.notifications.base import Message
from app.notifications.dispatcher import Dispatcher
from app.notifications.ntfy import NtfyProvider
from app.notifications.pushover import PushoverProvider
from app.service import Watcher
from app.utils.time import utcnow
from tests.conftest import FakeProvider, make_obs, ok_result, seats
from tests.test_reliability import Scripted


class Show:
    """What the fake PVR shows on each check; tests change it between checks."""

    def __init__(self) -> None:
        self.on_sale = True

    def result(self, _: int) -> Any:
        if self.on_sale:
            return ok_result(make_obs(seats=seats(120)))
        return ok_result(make_obs(bookable=False, sold_out=True, seats=seats(0)))


async def ringing_watcher(config: AppConfig, *providers: FakeProvider, **alarm: Any) -> tuple[Watcher, Show]:
    cfg = config.model_copy(
        update={
            "notifications": NotificationsConfig(alarm=AlarmConfig(**alarm)),
            "dashboard_url": "https://pc.example.ts.net",
        }
    )
    show = Show()
    service = Watcher(cfg, [Scripted("pvr", show.result)], list(providers))
    await service.start(background=False)
    await service.workers["pvr"].run_once()
    await service.dispatcher.process_due()  # the ticket alert itself
    return service, show


async def tick(service: Watcher, after_seconds: float = 61) -> None:
    """One alarm step, as if `after_seconds` had passed since the last reminder."""
    state = await service.repo.get_setting("alarm")
    if state:
        last = utcnow() - dt.timedelta(seconds=after_seconds)
        await service.repo.set_setting("alarm", {**state, "last": last.isoformat()})
    await service._alarm()
    await service.dispatcher.process_due()


def alarms(provider: FakeProvider) -> list[Message]:
    return [m for m in provider.sent if m.kind == "alarm"]


async def test_repeats_every_interval_until_acknowledged(config):
    ntfy = FakeProvider("ntfy")
    service, _ = await ringing_watcher(config, ntfy)
    assert [m.kind for m in ntfy.sent] == ["ticket"]

    await service._alarm()  # first step after the alert only starts the clock
    await service.dispatcher.process_due()
    assert alarms(ntfy) == []

    await tick(service)
    await tick(service, after_seconds=10)  # not yet a minute: nothing
    await tick(service)
    assert len(alarms(ntfy)) == 2
    first = alarms(ntfy)[0]
    assert first.title.startswith("STILL ON SALE") and "Reminder 1" in first.body and "I've seen it" in first.body
    assert first.ack_url == "https://pc.example.ts.net" and first.url == make_obs().booking_url

    status = await service.alarm_status()
    assert status["ringing"] and status["repeats"] == 2

    assert await service.acknowledge_alarm() == 1
    await tick(service)
    await tick(service)
    assert len(alarms(ntfy)) == 2, "acknowledged: no more reminders"
    assert not (await service.alarm_status())["ringing"]
    await service.stop()


async def test_stops_when_sold_out_and_rings_again_when_back(config):
    ntfy = FakeProvider("ntfy")
    service, show = await ringing_watcher(config, ntfy)
    await tick(service)
    await tick(service)
    assert len(alarms(ntfy)) == 1
    assert await service.acknowledge_alarm() == 1

    show.on_sale = False
    await service.workers["pvr"].run_once()
    await tick(service)
    assert await service.repo.get_setting("alarm") is None

    show.on_sale = True  # seats come back: a new generation, so it rings again without a new acknowledgement
    await service.workers["pvr"].run_once()
    await service.dispatcher.process_due()
    await tick(service)
    await tick(service)
    assert len(alarms(ntfy)) == 2 and (await service.alarm_status())["ringing"]
    await service.stop()


async def test_reminders_go_to_push_channels_only(config):
    ntfy, email = FakeProvider("ntfy"), FakeProvider("email")
    service, _ = await ringing_watcher(config, ntfy, email)
    assert [m.kind for m in email.sent] == ["ticket"], "the ticket alert itself goes everywhere"
    await tick(service)
    await tick(service)
    assert len(alarms(ntfy)) == 1 and alarms(email) == []
    (row,) = [r for r in await service.repo.notifications() if r.kind == "alarm"]
    assert row.status == "DELIVERED" and row.channels["email"]["status"] == "SKIPPED"
    await service.stop()


async def test_off_when_disabled(config):
    ntfy = FakeProvider("ntfy")
    service, _ = await ringing_watcher(config, ntfy, enabled=False)
    for _ in range(3):
        await tick(service)
    assert alarms(ntfy) == [] and not (await service.alarm_status())["ringing"]
    await service.stop()


async def test_a_reminder_that_cannot_go_out_expires_instead_of_piling_up(config):
    ntfy = FakeProvider("ntfy")
    service, _ = await ringing_watcher(config, ntfy)
    ntfy.failures = 10
    await tick(service)
    await tick(service)
    (row,) = [r for r in await service.repo.notifications() if r.kind == "alarm"]
    async with service.repo.transaction() as s:
        fresh = await s.get(type(row), row.id)
        fresh.created_at = utcnow() - dt.timedelta(minutes=4)
        fresh.next_attempt_at = utcnow()
    ntfy.failures = 0
    await service.dispatcher.process_due()
    (row,) = [r for r in await service.repo.notifications() if r.kind == "alarm"]
    assert row.status == "EXPIRED" and alarms(ntfy) == []
    await service.stop()


async def test_channel_formats():
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        sent.append(json.loads(body) if body.startswith("{") else dict(httpx.QueryParams(body)))
        return httpx.Response(200, json={"status": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    alarm = Message(title="t", body="b", url="https://book", kind="alarm", ack_url="https://dash")
    await NtfyProvider(client, "https://ntfy.sh", "topic").send(alarm)
    labels = [a["label"] for a in sent[-1]["actions"]]
    assert labels == ["Book now", "Stop alerts"] and sent[-1]["priority"] == 5
    await PushoverProvider(client, "a" * 30, "u" * 30).send(alarm)
    assert sent[-1]["priority"] == "1" and "retry" not in sent[-1], "no Pushover emergency repeats on top of ours"
    await PushoverProvider(client, "a" * 30, "u" * 30).send(alarm.model_copy(update={"kind": "ticket"}))
    assert sent[-1]["priority"] == "2"
    await client.aclose()


async def test_enqueue_with_no_chosen_channel_set_up_is_skipped(repo):
    dispatcher = Dispatcher(repo, [FakeProvider("email")])
    assert await dispatcher.enqueue("alarm", "t", "b", "k", only=["ntfy"]) is False


async def test_acknowledge_over_the_api(config):
    cfg = config.model_copy(update={"secrets": Secrets(dashboard_password="correct horse battery")})
    service, _ = await ringing_watcher(cfg, FakeProvider("ntfy"))
    app = create_app(service.cfg, service)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver")
    assert (await http.post("/api/alarm/ack")).status_code == 401
    await http.post("/api/login", json={"password": "correct horse battery"})
    assert (await http.get("/api/status")).json()["alarm"]["ringing"] is True
    assert (await http.post("/api/alarm/ack")).json() == {"acknowledged": 1}
    assert (await http.get("/api/status")).json()["alarm"]["ringing"] is False
    await http.aclose()
    await service.stop()
