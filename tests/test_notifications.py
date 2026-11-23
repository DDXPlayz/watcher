from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.database.models import NotificationRow
from app.notifications.base import DeliveryError, Message
from app.notifications.dispatcher import Dispatcher
from app.notifications.ntfy import NtfyProvider
from app.notifications.pushover import PushoverProvider
from app.notifications.telegram import TelegramProvider
from app.utils.logging import redact
from app.utils.time import utcnow
from tests.conftest import FakeProvider, make_obs, ok_result

MSG = Message(title="T <b>", body="Body & more", url="https://example.test/seat?a=1&b=2", kind="ticket")
TOKEN = "123456789:AAExampleExampleExampleExample12345"


def capture(status: int = 200, body: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True, "status": 1})

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_telegram_payload():
    seen, client = capture()
    await TelegramProvider(client, TOKEN, "42").send(MSG)
    data = json.loads(seen[0].content)
    assert data["chat_id"] == "42" and data["reply_markup"]["inline_keyboard"][0][0]["url"] == MSG.url
    assert "T &lt;b&gt;" in data["text"] and "Body &amp; more" in data["text"] and data["disable_notification"] is False


async def test_pushover_payload():
    seen, client = capture()
    await PushoverProvider(client, "app", "user").send(MSG)
    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert form["priority"] == "2" and form["retry"] == "30" and form["url"] == MSG.url
    await PushoverProvider(client, "app", "user").send(MSG.model_copy(update={"kind": "update"}))
    assert dict(httpx.QueryParams(seen[1].content.decode()))["priority"] == "0"


async def test_ntfy_payload():
    seen, client = capture()
    await NtfyProvider(client, "https://ntfy.test/", "topic-topic-topic", "tk").send(MSG)
    data = json.loads(seen[0].content)
    assert data["topic"] == "topic-topic-topic" and data["click"] == MSG.url and data["priority"] == 5
    assert data["actions"][0]["url"] == MSG.url and seen[0].headers["authorization"] == "Bearer tk"


@pytest.mark.parametrize(("status", "permanent"), [(401, True), (429, False), (500, False)])
async def test_provider_errors_do_not_leak_the_token(status, permanent):
    _, client = capture(status)
    with pytest.raises(DeliveryError) as info:
        await TelegramProvider(client, TOKEN, "42").send(MSG)
    assert info.value.permanent is permanent and "AAExample" not in str(info.value)


async def test_network_error_does_not_leak_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeliveryError) as info:
        await TelegramProvider(client, TOKEN, "42").send(MSG)
    assert "AAExample" not in str(info.value)


@pytest.mark.parametrize("provider", [TelegramProvider, PushoverProvider])
async def test_rejected_by_api_is_a_failure(provider):
    _, client = capture(200, {"ok": False, "status": 0})
    with pytest.raises(DeliveryError):
        await provider(client, TOKEN, "42").send(MSG)


def test_redaction():
    assert TOKEN.split(":")[1] not in redact(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    assert "hunter2" not in redact("password=hunter2") and "abc" not in redact('{"token": "abc"}')


async def due_now(repo):
    async with repo.transaction() as s:
        for row in (await repo.notifications()):
            fresh = await s.get(NotificationRow, row.id)
            fresh.next_attempt_at = utcnow() - dt.timedelta(seconds=1)


async def test_match_is_delivered_with_link_and_latency(engine, repo):
    provider = FakeProvider()
    await engine.process(ok_result(make_obs()))
    assert await Dispatcher(repo, [provider]).process_due()
    (message,) = provider.sent
    assert message.url == make_obs().booking_url and message.urgent
    (row,) = await repo.notifications()
    assert row.status == "DELIVERED" and row.delivered_at and row.provider == "fake"
    metrics = await repo.metrics()
    assert metrics["notifications_delivered"] == 1 and metrics["detection_to_delivery_ms_avg"] is not None


async def test_delivered_once_even_if_queue_runs_again(engine, repo):
    provider = FakeProvider()
    await engine.process(ok_result(make_obs()))
    dispatcher = Dispatcher(repo, [provider])
    for _ in range(5):
        await dispatcher.process_due()
        await engine.process(ok_result(make_obs()))
    assert len(provider.sent) == 1


async def test_failure_is_retried_until_it_works(engine, repo):
    provider = FakeProvider(failures=3)
    await engine.process(ok_result(make_obs()))
    dispatcher = Dispatcher(repo, [provider])
    for attempt in range(3):
        assert not await dispatcher.process_due()
        (row,) = await repo.notifications()
        assert row.status == "PENDING" and row.attempts == attempt + 1 and row.next_attempt_at > utcnow()
        await due_now(repo)
    assert await dispatcher.process_due() and len(provider.sent) == 1 and dispatcher.consecutive_failures == 0


async def test_every_channel_gets_every_alert(engine, repo):
    push, mail, whatsapp = FakeProvider("ntfy"), FakeProvider("email"), FakeProvider("whatsapp")
    await engine.process(ok_result(make_obs()))
    assert await Dispatcher(repo, [push, mail, whatsapp]).process_due()
    assert len(push.sent) == len(mail.sent) == len(whatsapp.sent) == 1
    (row,) = await repo.notifications()
    assert row.status == "DELIVERED" and set(row.channels) == {"ntfy", "email", "whatsapp"}


async def test_a_failing_channel_is_retried_alone(engine, repo):
    push, mail = FakeProvider("ntfy"), FakeProvider("email", failures=2)
    await engine.process(ok_result(make_obs()))
    dispatcher = Dispatcher(repo, [push, mail])
    await dispatcher.process_due()
    (row,) = await repo.notifications()
    assert row.status == "PENDING" and row.delivered_at is not None and row.provider == "ntfy"
    for _ in range(2):
        await due_now(repo)
        await dispatcher.process_due()
    (row,) = await repo.notifications()
    assert row.status == "DELIVERED" and len(push.sent) == 1 and len(mail.sent) == 1 and mail.attempts == 3
    assert dispatcher.failing_channels == [] and dispatcher.all_failed_streak == 0


async def test_channel_that_never_works_ends_partial_and_is_reported(engine, repo):
    push, broken = FakeProvider("ntfy"), FakeProvider("whatsapp", failures=99)
    await engine.process(ok_result(make_obs()))
    dispatcher = Dispatcher(repo, [push, broken], max_attempts=3)
    for _ in range(3):
        await dispatcher.process_due()
        await due_now(repo)
    (row,) = await repo.notifications()
    assert row.status == "PARTIAL" and row.channels["whatsapp"]["status"] == "FAILED" and len(push.sent) == 1
    assert dispatcher.failing_channels == ["whatsapp"] and dispatcher.all_failed_streak == 0


async def test_two_processes_sharing_a_database_send_once(engine, repo):
    first, second = FakeProvider("a"), FakeProvider("a")
    await engine.process(ok_result(make_obs()))
    a, b = Dispatcher(repo, [first]), Dispatcher(repo, [second])
    rows = await repo.notifications()
    claimed = [await a._claim(rows[0].id), await b._claim(rows[0].id)]
    assert claimed[0] is not None and claimed[1] is None


async def test_backup_runner_says_so(engine, repo):
    provider = FakeProvider()
    await engine.process(ok_result(make_obs()))
    await Dispatcher(repo, [provider], runner="github").process_due()
    assert provider.sent[0].body.endswith("Sent by: github watcher")


async def test_a_crashing_provider_does_not_stop_the_fallback(engine, repo):
    class Crash(FakeProvider):
        async def send(self, message):
            raise ZeroDivisionError

    backup = FakeProvider("backup")
    await engine.process(ok_result(make_obs()))
    assert await Dispatcher(repo, [Crash("crash"), backup]).process_due()
    assert len(backup.sent) == 1


async def test_gives_up_after_max_attempts_and_logs_it(engine, repo):
    await engine.process(ok_result(make_obs()))
    dispatcher = Dispatcher(repo, [FakeProvider(failures=99)], max_attempts=3)
    for _ in range(3):
        await dispatcher.process_due()
        await due_now(repo)
    (row,) = await repo.notifications()
    assert row.status == "FAILED" and row.attempts == 3
    assert "NOTIFICATION_FAILED" in [e.type for e in await repo.events()]


async def test_queue_survives_restart(engine, repo):
    await engine.process(ok_result(make_obs()))
    await Dispatcher(repo, [FakeProvider(failures=1)]).process_due()
    await due_now(repo)
    provider = FakeProvider()
    assert await Dispatcher(repo, [provider]).process_due() and len(provider.sent) == 1


async def test_no_provider_keeps_the_alert_pending(engine, repo):
    await engine.process(ok_result(make_obs()))
    await Dispatcher(repo, []).process_due()
    (row,) = await repo.notifications()
    assert row.status == "PENDING" and row.attempts == 0 and "no notification provider" in row.last_error


async def test_old_alerts_expire_instead_of_arriving_late(engine, repo):
    await engine.process(ok_result(make_obs()))
    async with repo.transaction() as s:
        (await s.get(NotificationRow, 1)).created_at = utcnow() - dt.timedelta(hours=7)
    provider = FakeProvider()
    await Dispatcher(repo, [provider]).process_due()
    assert provider.sent == [] and (await repo.notifications())[0].status == "EXPIRED"


async def test_enqueue_is_idempotent(repo):
    dispatcher = Dispatcher(repo, [FakeProvider()])
    assert await dispatcher.enqueue("system", "t", "b", "k") and not await dispatcher.enqueue("system", "t", "b", "k")


async def test_tickets_go_before_other_messages(engine, repo):
    provider = FakeProvider()
    dispatcher = Dispatcher(repo, [provider])
    await dispatcher.enqueue("system", "t", "b", "k")
    await engine.process(ok_result(make_obs()))
    await dispatcher.process_due()
    assert [m.kind for m in provider.sent] == ["ticket", "system"]
