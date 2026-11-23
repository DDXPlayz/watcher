"""Email, WhatsApp, District's film signal, one-shot mode and the database upgrade."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from pathlib import Path

import httpx
import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from app.config import DistrictConfig, Secrets, load_config
from app.database.repository import Repository
from app.domain import ErrorKind, PlatformResult, State
from app.monitor.events import PLATFORM_NAMES
from app.notifications.base import DeliveryError, Message
from app.notifications.email import EmailProvider
from app.notifications.whatsapp import CallMeBotProvider, WhatsAppCloudProvider, one_line
from app.platforms.district import DistrictPlatform, parse_film_formats
from app.service import Watcher
from tests.conftest import FakeProvider, fixture_text, make_obs, ok_result
from tests.test_reliability import Scripted

MSG = Message(
    title="AVENGERS: DOOMSDAY — SCREENX TICKETS AVAILABLE",
    body="Cinema: INOX Pacific Mall, Jasola\nTime: 3:30 PM IST\nPlatform: PVR INOX",
    url="https://www.pvrcinemas.com/seatlayout/abc==",
)


# ---------------------------------------------------------------- email, against a real local SMTP server


class Inbox:
    def __init__(self) -> None:
        self.messages: list[tuple[str, list[str], bytes]] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - aiosmtpd hook name
        self.messages.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content))
        return "250 OK"


def authenticator(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == b"me@example.test" and auth_data.password == b"app-pass"
    return AuthResult(success=ok)


@pytest.fixture
def smtp():
    inbox = Inbox()
    with socket.socket() as probe:  # aiosmtpd cannot pick its own port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    controller = Controller(
        inbox, hostname="127.0.0.1", port=port, authenticator=authenticator, auth_require_tls=False, auth_required=True
    )
    controller.start()
    yield inbox, port
    controller.stop()


async def test_email_is_delivered_with_subject_body_and_link(smtp):
    inbox, port = smtp
    provider = EmailProvider("127.0.0.1", port, "none", "me@example.test", "app-pass", "", "me@example.test, parent@example.test")
    await provider.send(MSG)
    ((sender, recipients, raw),) = inbox.messages
    text = raw.decode()
    assert sender == "me@example.test" and recipients == ["me@example.test", "parent@example.test"]
    assert "Subject: AVENGERS: DOOMSDAY" in text and "Importance: high" in text
    assert "Book now: https://www.pvrcinemas.com/seatlayout/abc==" in text and "text/html" in text


async def test_email_wrong_password_is_permanent_and_quiet(smtp):
    _, port = smtp
    provider = EmailProvider("127.0.0.1", port, "none", "me@example.test", "wrong-secret", "", "me@example.test")
    with pytest.raises(DeliveryError) as info:
        await provider.send(MSG)
    assert info.value.permanent and "wrong-secret" not in str(info.value)


async def test_email_server_down_is_retryable():
    provider = EmailProvider("127.0.0.1", 9, "none", "", "", "a@example.test", "a@example.test")
    with pytest.raises(DeliveryError) as info:
        await provider.send(MSG)
    assert not info.value.permanent


def test_email_html_escapes_site_text():
    mail = EmailProvider("h", 1, "ssl", "", "", "a@example.test", "b@example.test").build(MSG.model_copy(update={"body": "<script>x</script>"}))
    html = mail.get_body(("html",)).get_content()
    assert "<script>" not in html and "&lt;script&gt;" in html


# ---------------------------------------------------------------- WhatsApp


def recorder(status: int = 200, text: str = "Message queued", body: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if body is not None:
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=text)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_callmebot_request():
    seen, client = recorder()
    await CallMeBotProvider(client, "+919800000000", "123456").send(MSG)
    params = seen[0].url.params
    assert params["phone"] == "+919800000000" and params["apikey"] == "123456"
    assert params["text"].startswith("*AVENGERS: DOOMSDAY") and MSG.url in params["text"]


@pytest.mark.parametrize(
    ("status", "text", "permanent"),
    [(200, "APIKey is invalid. Please check it", True), (200, "<b>ERROR</b> something", False), (403, "", True), (502, "", False)],
)
async def test_callmebot_failures(status, text, permanent):
    _, client = recorder(status, text)
    with pytest.raises(DeliveryError) as info:
        await CallMeBotProvider(client, "+91", "secret-key-9").send(MSG)
    assert info.value.permanent is permanent and "secret-key-9" not in str(info.value)


async def test_callmebot_network_error_hides_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeliveryError) as info:
        await CallMeBotProvider(client, "+91", "secret-key-9").send(MSG)
    assert "secret-key-9" not in str(info.value)


async def test_whatsapp_cloud_template():
    seen, client = recorder(body={"messages": [{"id": "wamid.1"}]})
    await WhatsAppCloudProvider(client, "tok", "555", "+919800000000", "ticket_alert", "en", "v23.0").send(MSG)
    request = seen[0]
    data = json.loads(request.content)
    assert str(request.url) == "https://graph.facebook.com/v23.0/555/messages" and request.headers["authorization"] == "Bearer tok"
    assert data["to"] == "919800000000" and data["template"]["name"] == "ticket_alert"
    summary, link = (p["text"] for p in data["template"]["components"][0]["parameters"])
    assert "\n" not in summary and "Jasola" in summary and link == MSG.url


async def test_whatsapp_cloud_rejection():
    _, client = recorder(400, body={"error": {"message": "template not approved"}})
    with pytest.raises(DeliveryError) as info:
        await WhatsAppCloudProvider(client, "tok", "555", "91", "t", "en", "v23.0").send(MSG)
    assert info.value.permanent


def test_one_line():
    assert one_line("a\n\tb    c", 100) == "a b c" and len(one_line("x" * 2000, 900)) == 900


# ---------------------------------------------------------------- configuration of the new channels


def test_all_channels_can_be_enabled_together():
    env = {
        "NOTIFY_PROVIDERS": "pushover,email,whatsapp,whatsapp_cloud",
        "PUSHOVER_APP_TOKEN": "a", "PUSHOVER_USER_KEY": "b", "SMTP_HOST": "smtp.gmail.com", "EMAIL_TO": "me@example.test",
        "CALLMEBOT_PHONE": "+91", "CALLMEBOT_APIKEY": "k", "WA_CLOUD_TOKEN": "t", "WA_CLOUD_PHONE_NUMBER_ID": "1",
        "WA_CLOUD_TO": "91", "WA_CLOUD_TEMPLATE": "x", "SMTP_PORT": "587", "SMTP_SECURITY": "starttls",
    }
    cfg = load_config(Path(__file__).parent.parent / "config.yaml", env)
    from app.notifications.dispatcher import build_providers

    assert [p.name for p in build_providers(cfg, httpx.AsyncClient())] == ["pushover", "email", "whatsapp", "whatsapp_cloud"]
    assert cfg.secrets.smtp_port == 587 and cfg.platforms.district.terms_ack


@pytest.mark.parametrize(
    "env",
    [{"NOTIFY_PROVIDERS": "email"}, {"NOTIFY_PROVIDERS": "whatsapp", "CALLMEBOT_PHONE": "+91"}, {"NOTIFY_PROVIDERS": "email", "SMTP_HOST": "h", "EMAIL_TO": "x", "SMTP_SECURITY": "tls"}],
)
def test_incomplete_channel_settings_are_refused(env):
    with pytest.raises(ValueError):
        load_config(Path(__file__).parent.parent / "config.yaml", env)


# ---------------------------------------------------------------- District film signal


def test_film_formats_from_real_page():
    assert parse_film_formats(fixture_text("district_film_trimmed.html")) == ["MS INFINITY VISION", "MS INFINITY VISION 3D", "MS INFINITY VISION 4DX-3D", "MS INFINITY VISION ICE-3D"]
    assert parse_film_formats("<html></html>") is None
    assert parse_film_formats('<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>') is None


async def test_district_check_reports_film_formats_and_survives_film_page_failure(target):
    pages = {"/movies/inox-pacific-mall-jasola-new-delhi-in-new-delhi-CD1018963": fixture_text("district_jasola_trimmed.html")}
    film_status = [200]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path])
        return httpx.Response(film_status[0], text=fixture_text("district_film_trimmed.html"))

    platform = DistrictPlatform(DistrictConfig(terms_ack=True), transport=httpx.MockTransport(handler))
    result = await platform.check_availability(target)
    assert result.state is State.NOT_YET_LISTED and result.signals["film_formats"] == ["MS INFINITY VISION", "MS INFINITY VISION 3D", "MS INFINITY VISION 4DX-3D", "MS INFINITY VISION ICE-3D"]
    film_status[0] = 500
    result = await platform.check_availability(target)
    assert result.state is State.NOT_YET_LISTED and "film_formats" not in result.signals
    await platform.close()


async def test_screenx_appearing_for_the_film_is_an_event_not_an_alert(engine, repo):
    await engine.process(ok_result(platform="district", signals={"film_formats": ["MS INFINITY VISION"]}))
    await engine.process(ok_result(platform="district", signals={"film_formats": ["MS INFINITY VISION", "SCREEN X"]}))
    events = [e.message for e in await repo.events()]
    assert any("new formats for the film in the city: SCREEN X" in m for m in events)
    assert await repo.notifications() == []


# ---------------------------------------------------------------- one-shot mode


async def test_once_mode_checks_alerts_and_exits(config):
    provider = FakeProvider()
    service = Watcher(config, [Scripted("pvr", lambda _: ok_result(make_obs()))], [provider])
    await service.start(background=False)
    assert await service.run_once(drain_seconds=5) == 0
    assert len(provider.sent) == 1 and service.supervisor.tasks == {}
    await service.stop()

    again = Watcher(config, [Scripted("pvr", lambda _: ok_result(make_obs()))], [FakeProvider()])
    await again.start(background=False)
    assert await again.run_once(drain_seconds=5) == 0
    assert again.dispatcher.providers[0].sent == [], "state carried over: no repeat"
    await again.stop()


async def test_once_mode_sends_without_waiting_for_slower_platforms(config):
    provider = FakeProvider()
    seen_before_finishing: list[int] = []

    class Slow(Scripted):
        async def check_availability(self, target):
            for _ in range(100):  # up to 5 s for the fast platform's alert to go out
                if provider.sent:
                    break
                await asyncio.sleep(0.05)
            seen_before_finishing.append(len(provider.sent))
            return PlatformResult(platform="district", state=State.NOT_YET_LISTED, listing_complete=True)

    fast = Scripted("pvr", lambda _: ok_result(make_obs()))
    service = Watcher(config, [fast, Slow("district", lambda _: None)], [provider])
    await service.start(background=False)
    assert await service.run_once(drain_seconds=5) == 0
    assert seen_before_finishing == [1], "the alert waited for the slower platform"
    await service.stop()


async def test_once_mode_fails_when_every_platform_fails(config):
    failing = PlatformResult(platform="pvr", state=State.PLATFORM_ERROR, error_kind=ErrorKind.ACCESS_DENIED, error_detail="x")
    blocked = PlatformResult(platform="bookmyshow", state=State.PLATFORM_ERROR, error_kind=ErrorKind.ACCESS_NOT_PERMITTED)
    service = Watcher(config, [Scripted("pvr", lambda _: failing), Scripted("bookmyshow", lambda _: blocked)], [FakeProvider()])
    await service.start(background=False)
    assert await service.run_once(drain_seconds=1) == 1
    await service.stop()


async def test_once_mode_retries_a_failed_channel_before_exiting(config):
    flaky = FakeProvider("email", failures=1)
    service = Watcher(config, [Scripted("pvr", lambda _: ok_result(make_obs()))], [flaky])
    await service.start(background=False)
    started = asyncio.get_running_loop().time()
    await service.run_once(drain_seconds=30)
    assert len(flaky.sent) == 1 and asyncio.get_running_loop().time() - started < 30
    await service.stop()


def test_secrets_never_printed():
    assert "app-pass" not in repr(Secrets(smtp_password="app-pass", callmebot_apikey="app-pass"))


# ---------------------------------------------------------------- database upgrade from the first version


async def test_version_1_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY); INSERT INTO schema_version VALUES (1);
        CREATE TABLE notifications (id INTEGER PRIMARY KEY, dedupe_key VARCHAR(320) UNIQUE, event_id INTEGER, kind VARCHAR(32),
          title TEXT, body TEXT, url TEXT, provider VARCHAR(32), status VARCHAR(16), attempts INTEGER, last_error TEXT,
          created_at DATETIME, next_attempt_at DATETIME, delivered_at DATETIME, detected_at DATETIME, send_ms INTEGER);
        INSERT INTO notifications (dedupe_key, kind, title, body, status, attempts, created_at, next_attempt_at)
          VALUES ('old', 'ticket', 't', 'b', 'DELIVERED', 1, '2026-11-22 10:00:00', '2026-11-22 10:00:00');
        """
    )
    con.close()
    # The other tables did not exist in this hand-made file; create them the way version 1 would have.
    from app.database.models import Base

    repo = Repository(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with repo.engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[t for n, t in Base.metadata.tables.items() if n != "notifications"]))
    await repo.start({k: (v, True) for k, v in PLATFORM_NAMES.items()})
    (row,) = await repo.notifications()
    assert row.dedupe_key == "old" and row.channels is None
    con = sqlite3.connect(path)
    assert [r[0] for r in con.execute("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    con.close()
    await repo.close()
