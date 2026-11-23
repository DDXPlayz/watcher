"""Scheduling, isolation, recovery, restart, configuration and the HTTP API."""

from __future__ import annotations

import asyncio
import datetime as dt
import gc
import json
import logging
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.config import AppConfig, MonitoringConfig, PlatformConfig, Secrets, load_config
from app.domain import ErrorKind, HealthStatus, PlatformResult, State, Target
from app.main import create_app
from app.platforms.base import BookingPlatform, PlatformFailure
from app.scheduler import CheckRefused, PlatformWorker, Supervisor
from app.service import Watcher
from app.utils.logging import JsonFormatter, RepeatFilter
from app.utils.retry import CircuitBreaker, backoff_delay
from app.utils.time import parse_clock
from tests.conftest import FakeProvider, make_obs, ok_result
from tests.fake_pvr import FakePvr


class Scripted(BookingPlatform):
    def __init__(self, name: str, action: Callable[[int], PlatformResult]) -> None:
        self.name, self.action, self.calls, self.resets = name, action, 0, 0

    async def check_availability(self, target: Target) -> PlatformResult:
        self.calls += 1
        return self.action(self.calls)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(platform=self.name, operational=True)

    async def reset(self) -> None:
        self.resets += 1


class Hanging(Scripted):
    async def check_availability(self, target: Target) -> PlatformResult:
        self.calls += 1
        await asyncio.sleep(60)
        raise AssertionError


def err(name: str, kind: ErrorKind, **kw) -> PlatformResult:
    return PlatformResult(platform=name, state=State.PLATFORM_ERROR, error_kind=kind, error_detail="x", **kw)


def worker(engine, platform, **mon) -> PlatformWorker:
    return PlatformWorker(platform, engine, PlatformConfig(interval_seconds=60), MonitoringConfig(**mon))


async def eventually(check: Callable[[], bool], seconds: float = 5) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------- backoff and breaker


def test_backoff_grows_and_is_capped():
    delays = [backoff_delay(n, 10, 300, jitter=0) for n in range(1, 10)]
    assert delays[:5] == [10, 20, 40, 80, 160] and delays[-1] == 300


def test_breaker_opens_probes_and_recovers():
    now = [0.0]
    b = CircuitBreaker(3, 100, clock=lambda: now[0])
    for _ in range(3):
        b.record_failure()
    assert b.state == "open" and not b.allows()
    now[0] = 101
    assert b.state == "half_open" and b.allows()
    b.record_failure()
    assert b.state == "open"
    now[0] = 202
    b.record_success()
    assert b.state == "closed" and b.failures == 0


# ---------------------------------------------------------------- worker


async def test_interval_is_a_true_minimum(engine):
    w = worker(engine, Scripted("pvr", lambda _: ok_result()))
    result = await w.run_once()
    assert all(60 <= w.next_delay(result) <= 60 * 1.15 + 0.01 for _ in range(200))


async def test_unverified_is_rechecked_sooner_but_never_under_30s(engine):
    w = worker(engine, Scripted("pvr", lambda _: ok_result(make_obs(bookable=None))))
    result = await w.run_once()
    assert result.state is State.UNVERIFIED
    assert all(30 <= w.next_delay(result) <= 35 for _ in range(100))


async def test_failures_back_off_and_recovery_restores_interval(engine):
    w = worker(engine, Scripted("pvr", lambda n: err("pvr", ErrorKind.NETWORK) if n <= 3 else ok_result()), breaker_threshold=10)
    delays = [w.next_delay(await w.run_once()) for _ in range(3)]
    assert delays[0] < delays[1] < delays[2]
    assert 60 <= w.next_delay(await w.run_once()) < 70 and w.breaker.failures == 0


async def test_retry_after_is_honoured_and_blocks_check_now(engine):
    w = worker(engine, Scripted("pvr", lambda _: err("pvr", ErrorKind.RATE_LIMITED, retry_after=600.0)))
    assert w.next_delay(await w.run_once()) >= 600
    with pytest.raises(CheckRefused, match="wait"):
        w.request_check_now()


async def test_access_denied_waits_minutes(engine):
    w = worker(engine, Scripted("pvr", lambda _: err("pvr", ErrorKind.ACCESS_DENIED)))
    assert w.next_delay(await w.run_once()) >= 90


async def test_check_now_has_a_minimum_gap(engine):
    w = worker(engine, Scripted("pvr", lambda _: ok_result()))
    w.request_check_now()
    await w.run_once()
    with pytest.raises(CheckRefused, match="minimum gap"):
        w.request_check_now()


async def test_check_now_refused_when_not_permitted(engine):
    w = worker(engine, Scripted("bookmyshow", lambda _: err("bookmyshow", ErrorKind.ACCESS_NOT_PERMITTED)))
    result = await w.run_once()
    assert w.next_delay(result) > 1800 and w.breaker.failures == 0
    with pytest.raises(CheckRefused):
        w.request_check_now()


async def test_exception_in_adapter_becomes_an_error(engine):
    def boom(_: int) -> PlatformResult:
        raise ValueError("bug")

    w = worker(engine, Scripted("pvr", boom))
    result = await w.run_once()
    assert result.error_kind is ErrorKind.INTERNAL and w.platform.resets == 1


async def test_hung_adapter_times_out(engine):
    w = worker(engine, Hanging("pvr", lambda _: ok_result()), check_timeout_seconds=5)
    w.mon = w.mon.model_copy(update={"check_timeout_seconds": 0.05})
    assert (await w.run_once()).error_kind is ErrorKind.TIMEOUT and w.platform.resets == 1


async def test_workers_are_isolated(engine, repo):
    def boom(_: int) -> PlatformResult:
        raise PlatformFailure(ErrorKind.SERVER_ERROR, "x")

    bad, good = worker(engine, Scripted("pvr", boom)), worker(engine, Scripted("district", lambda _: ok_result(platform="district")))
    hung = worker(engine, Hanging("bookmyshow", lambda _: ok_result()))
    sup = Supervisor([bad, good, hung], MonitoringConfig(), lambda *_: asyncio.sleep(0))
    sup.running.set()
    sup.start()
    await eventually(lambda: good.platform.calls == 1 and bad.platform.calls == 1 and hung.platform.calls == 1)
    await eventually(lambda: good.last_result is not None)
    assert good.last_result.ok
    await sup.stop()


async def test_crashed_worker_is_restarted(engine):
    restarts: list[str] = []

    async def note(name: str, reason: str) -> None:
        restarts.append(reason)

    w = worker(engine, Scripted("pvr", lambda _: ok_result()))
    original, crashed = w.engine.process, []

    async def flaky(result):
        if not crashed:
            crashed.append(1)
            raise OSError("database is locked")
        return await original(result)

    w.engine.process = flaky
    sup = Supervisor([w], MonitoringConfig(), note, restart_delay=0)
    sup.running.set()
    sup.start()
    await eventually(lambda: sup.tasks["pvr"].done())
    await sup.tick()
    await eventually(lambda: w.last_result is not None and w.last_result.ok)
    assert len(restarts) == 1 and "database is locked" in restarts[0]
    await sup.stop()


async def test_stalled_worker_is_restarted(engine):
    restarts: list[str] = []

    async def note(name: str, reason: str) -> None:
        restarts.append(reason)

    w = worker(engine, Hanging("pvr", lambda _: ok_result()))
    sup = Supervisor([w], w.mon, note, restart_delay=0)
    sup.running.set()
    sup.start()
    await eventually(lambda: w.platform.calls == 1)
    w.last_progress -= 1000
    await sup.tick()
    await eventually(lambda: w.platform.calls == 2)
    assert "no progress" in restarts[0]
    await sup.stop()


async def test_pause_stops_checks_and_resume_restarts_them(engine):
    w = worker(engine, Scripted("pvr", lambda _: ok_result()))
    sup = Supervisor([w], w.mon, lambda *_: asyncio.sleep(0))
    sup.start()
    await asyncio.sleep(2.3)
    assert w.platform.calls == 0
    sup.running.set()
    await eventually(lambda: w.platform.calls == 1)
    await sup.stop()


async def test_long_run_does_not_grow(engine, repo):
    """2,000 checks with one unchanged match: one alert, bounded tables, flat memory."""
    result = ok_result(make_obs())
    for _ in range(300):
        await engine.process(result)
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for _ in range(1700):
        await engine.process(result)
    gc.collect()
    growth = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(before, "filename"))
    tracemalloc.stop()
    assert growth < 2_000_000, f"{growth} bytes"
    assert len(await repo.notifications()) == 1 and len(await repo.screenings()) == 1
    assert len(await repo.events(1000)) <= 3


# ---------------------------------------------------------------- whole application


async def run_watcher(cfg: AppConfig, platforms, providers) -> Watcher:
    service = Watcher(cfg, platforms, providers)
    await service.start()
    return service


async def test_restart_keeps_state_and_reports_unclean_exit(config):
    provider = FakeProvider()
    first = await run_watcher(config, [Scripted("pvr", lambda _: ok_result(make_obs()))], [provider])
    await eventually(lambda: len(provider.sent) == 1, 8)
    # simulate a crash: stop the tasks without the clean shutdown marker
    for task in first._tasks:
        task.cancel()
    await asyncio.gather(*first._tasks, return_exceptions=True)
    await first.supervisor.stop()
    await first.repo.close()

    again = FakeProvider()
    second = await run_watcher(config, [Scripted("pvr", lambda _: ok_result(make_obs()))], [again])
    await eventually(lambda: second.workers["pvr"].last_result is not None, 8)
    await eventually(lambda: any(m.kind == "system" for m in again.sent), 8)
    await asyncio.sleep(0.3)
    assert [m.kind for m in again.sent] == ["system"], "the ticket alert must not repeat"
    assert len(await second.repo.screenings()) == 1
    await second.stop()

    third = await run_watcher(config, [Scripted("pvr", lambda _: ok_result())], [FakeProvider()])
    assert (await third.repo.get_setting("clean_shutdown")) is False
    assert sum("clean shutdown" in e.message for e in await third.repo.events()) == 1
    await third.stop()


async def test_pause_survives_restart(config):
    first = await run_watcher(config, [Scripted("pvr", lambda _: ok_result())], [FakeProvider()])
    await first.set_running(False)
    await first.stop()
    second = await run_watcher(config, [Scripted("pvr", lambda _: ok_result())], [FakeProvider()])
    assert (await second.health())["status"] == "paused"
    await second.stop()


async def test_not_healthy_when_everything_fails(config):
    service = await run_watcher(config, [Scripted("pvr", lambda _: err("pvr", ErrorKind.SERVER_ERROR))], [FakeProvider()])
    await eventually(lambda: service.workers["pvr"].last_result is not None)
    service._started_mono -= 1000
    assert (await service.health())["status"] == "down"
    service._all_failing_since = time.monotonic() - 1000
    await service._alert_if_all_failing("down")
    await service._alert_if_all_failing("down")
    await eventually(lambda: len(service.dispatcher.providers[0].sent) == 1)
    assert service.dispatcher.providers[0].sent[0].kind == "system"
    await service.stop()


async def test_policy_blocked_platforms_alone_are_not_healthy(config):
    service = await run_watcher(config, [Scripted("bookmyshow", lambda _: err("bookmyshow", ErrorKind.ACCESS_NOT_PERMITTED))], [FakeProvider()])
    await eventually(lambda: service.workers["bookmyshow"].last_result is not None)
    service._started_mono -= 1000
    assert (await service.health())["status"] == "down"
    await service.stop()


async def test_healthy_needs_a_provider(config):
    for providers, expected in (([FakeProvider()], "healthy"), ([], "degraded")):
        service = await run_watcher(config, [Scripted("pvr", lambda _: ok_result())], providers)
        await eventually(lambda s=service: s.workers["pvr"].last_result is not None)
        await asyncio.sleep(0.1)
        assert (await service.health())["status"] == expected
        await service.stop()


async def test_stale_match_is_not_shown_as_available(config):
    service = await run_watcher(config, [Scripted("pvr", lambda n: ok_result(make_obs()))], [FakeProvider()])
    await eventually(lambda: service.workers["pvr"].last_result is not None)
    await asyncio.sleep(0.1)
    assert (await service.status())["matches"] == 1
    from app.database.models import ScreeningRow

    async with service.repo.transaction() as s:
        (await s.get(ScreeningRow, 1)).last_verified -= dt.timedelta(seconds=301)
    status = await service.status()
    assert status["matches"] == 0 and status["screenings"][0]["stale"] and not status["screenings"][0]["currently_available"]
    await service.stop()


async def test_monitoring_stops_after_the_show_window(config, target):
    past = config.model_copy(update={"target": target.model_copy(update={"date": dt.date(2020, 1, 1)})})
    service = await run_watcher(past, [Scripted("pvr", lambda _: ok_result())], [FakeProvider()])
    await eventually(lambda: service.finished)
    assert (await service.health())["status"] == "finished"
    await service.stop()


async def test_reminders_fire_once(config, monkeypatch):
    service = Watcher(config, [], [FakeProvider()])
    await service.repo.start({})
    service.cfg.platforms.bookmyshow.reminder_times_ist.append("13:00")
    monkeypatch.setattr("app.service.utcnow", lambda: dt.datetime(2026, 12, 1, 7, 30, tzinfo=dt.UTC))
    await service._reminders()
    await service._reminders()
    (row,) = await service.repo.notifications()
    assert row.kind == "reminder" and "not a ticket alert" in row.body
    await service.repo.close()


async def test_end_to_end_from_page_to_push(config):
    """Real Chromium, the real adapter, engine, database and ntfy provider; only the two servers are local."""
    from app.notifications.ntfy import NtfyProvider
    from app.platforms.pvr import PvrPlatform

    site = FakePvr()
    pushes: list[dict] = []

    def ntfy(request: httpx.Request) -> httpx.Response:
        pushes.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(ntfy))
    pvr = PvrPlatform(config.platforms.pvr.model_copy(update={"movie_url": site.url}), config.aliases, timeout_ms=6000)
    service = Watcher(config, [pvr], [NtfyProvider(client, "https://ntfy.test", "topic-topic-topic")])
    service.workers["pvr"].mon = service.workers["pvr"].mon.model_copy(update={"check_now_min_gap_seconds": 0})
    await service.start()
    try:
        await eventually(lambda: service.workers["pvr"].last_result is not None, 20)
        assert service.workers["pvr"].last_result.state is State.NOT_YET_LISTED and pushes == []

        site.mode = "403"
        service.workers["pvr"].request_check_now()
        await eventually(lambda: service.workers["pvr"].last_result.state is State.PLATFORM_ERROR, 20)
        assert pushes == []

        site.mode = "listed"  # the release
        service.workers["pvr"].blocked_until = 0
        service.workers["pvr"].request_check_now()
        await eventually(lambda: len(pushes) == 1, 25)
        assert pushes[0]["click"] == f"{site.base}/seatlayout/tokenABC==" and "SCREENX TICKETS AVAILABLE" in pushes[0]["title"]
        assert "150 seats visible" in pushes[0]["message"] and "3:30 PM IST" in pushes[0]["message"]

        service.workers["pvr"].request_check_now()
        await asyncio.sleep(4)
        assert len(pushes) == 1
        latency = (await service.repo.metrics())["detection_to_delivery_ms_max"]
        assert latency is not None and latency < 3000
        assert not any("accepted" in h for h in site.hits)
    finally:
        await service.stop()
        site.close()


# ---------------------------------------------------------------- HTTP API

PASSWORD = "correct horse battery"


async def client_for(cfg: AppConfig, host: str = "testserver"):
    service = Watcher(cfg, [Scripted("pvr", lambda _: ok_result(make_obs()))], [FakeProvider()])
    app = create_app(cfg, service)
    await service.start()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=f"http://{host}")
    await eventually(lambda: service.workers["pvr"].last_result is not None)
    await asyncio.sleep(0.1)
    return service, http


@pytest.fixture
def locked(config) -> AppConfig:
    return config.model_copy(update={"secrets": Secrets(dashboard_password=PASSWORD)})


async def test_api_requires_sign_in(locked):
    service, http = await client_for(locked)
    assert (await http.get("/health")).json() == {"status": "healthy"}
    for path in ("/status", "/api/status", "/api/events", "/api/logs", "/api/export", "/api/notifications", "/assets/app.js"):
        assert (await http.get(path)).status_code == 401, path
    assert (await http.post("/api/monitoring/pause")).status_code == 401
    assert (await http.get("/")).headers["location"] == "/login"
    assert (await http.post("/api/login", json={"password": "nope"})).status_code == 401
    response = await http.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"] and "samesite=strict" in response.headers["set-cookie"].lower()
    assert (await http.get("/api/status")).status_code == 200
    http.cookies.set("dw_session", "9999999999.deadbeef")
    assert (await httpx.AsyncClient(transport=http._transport, base_url="http://testserver", cookies={"dw_session": "9999999999.dead"}).get("/status")).status_code == 401
    await service.stop()


async def test_login_is_rate_limited(locked):
    service, http = await client_for(locked)
    codes = [(await http.post("/api/login", json={"password": "x"})).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    await service.stop()


async def test_cross_site_and_rebinding_are_refused(config):
    service, http = await client_for(config)
    assert (await http.post("/api/monitoring/pause", headers={"Origin": "https://evil.test"})).status_code == 403
    assert (await http.get("/api/status", headers={"Host": "evil.test"})).status_code == 421
    assert (await http.post("/api/monitoring/pause", headers={"Origin": "http://testserver"})).status_code == 200
    await service.stop()


async def test_status_reflects_backend_and_controls_work(config):
    service, http = await client_for(config)
    status = (await http.get("/api/status")).json()
    assert status["matches"] == 1 and status["running"] and status["platforms"][0]["health"] == "match_available"
    (screening,) = status["screenings"]
    assert screening["showtime"] == "3:30 PM" and screening["currently_available"] and screening["booking_url"]

    await http.post("/api/monitoring/pause")
    assert not service.supervisor.running.is_set() and (await http.get("/health")).json()["status"] == "paused"
    assert (await http.post("/api/check")).status_code == 409
    await http.post("/api/monitoring/resume")
    assert service.supervisor.running.is_set()
    assert "minimum gap" in (await http.post("/api/check")).json()["refused"]["pvr"]

    assert (await http.post("/api/test-notification")).json() == {"queued": True}
    await eventually(lambda: any(m.kind == "test" for m in service.dispatcher.providers[0].sent))
    assert any("Monitoring paused" in r["message"] for r in (await http.get("/api/events")).json())
    csv_text = (await http.get("/api/export?fmt=csv")).text
    assert csv_text.startswith("﻿id,time_ist") and "IST" in csv_text
    assert (await http.get("/api/export?fmt=exe")).status_code == 400
    assert (await http.get("/")).status_code == 200 and "script-src 'self'" in (await http.get("/")).headers["content-security-policy"]
    await service.stop()


async def test_target_changes_need_confirmation_and_persist(config):
    service, http = await client_for(config)
    body = {"date": "2026-12-20", "language": "English", "earliest_time": "14:00", "latest_time": "18:00"}
    assert (await http.put("/api/target", json=body)).status_code == 422
    assert (await http.put("/api/target", json={**body, "confirm": "change target"})).status_code == 422
    assert (await http.put("/api/target", json={**body, "confirm": "CHANGE TARGET", "cinema": "X"})).status_code == 422
    assert (await http.put("/api/target", json={**body, "confirm": "CHANGE TARGET", "latest_time": "13:00"})).status_code == 422
    assert service.engine.target.date == dt.date(2026, 12, 19)
    assert (await http.put("/api/target", json={**body, "confirm": "CHANGE TARGET"})).status_code == 200
    assert service.engine.target.date == dt.date(2026, 12, 20)
    await service.stop()
    again = await run_watcher(config, [Scripted("pvr", lambda _: ok_result())], [FakeProvider()])
    assert again.engine.target.date == dt.date(2026, 12, 20)
    await again.stop()


async def test_no_secret_reaches_the_api(config):
    secret = "tok-super-secret-value"
    cfg = config.model_copy(update={"secrets": Secrets(telegram_bot_token=secret, ntfy_topic=secret)})
    service, http = await client_for(cfg)
    for path in ("/api/status", "/api/events", "/api/notifications", "/api/logs", "/"):
        assert secret not in (await http.get(path)).text, path
    assert secret not in repr(cfg) and secret not in cfg.model_dump_json()
    await service.stop()


# ---------------------------------------------------------------- configuration, time, logging

BASE = Path(__file__).parent.parent / "config.yaml"


def test_shipped_config_is_the_exact_target():
    target = load_config(BASE, {}).target
    assert (target.movie, target.cinema, target.city, target.language, target.format) == (
        "Avengers: Doomsday", "INOX Pacific Mall, Jasola", "New Delhi", "English", "ScreenX",
    )
    assert (target.date, target.earliest_time, target.latest_time) == (dt.date(2026, 12, 19), dt.time(14), dt.time(18))
    assert target.date.strftime("%A") == "Saturday"


@pytest.mark.parametrize(
    "env",
    [
        {"HOST": "0.0.0.0"}, {"HOST": "0.0.0.0", "DASHBOARD_PASSWORD": "short"}, {"NOTIFY_PROVIDERS": "telegram"},
        {"NOTIFY_PROVIDERS": "ntfy", "NTFY_TOPIC": "short"}, {"NOTIFY_PROVIDERS": "carrier-pigeon"}, {"PORT": "abc"},
    ],
)
def test_bad_environment_is_refused(env):
    with pytest.raises(ValueError):
        load_config(BASE, env)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("payment_automation: false", "payment_automation: true"), ("  enabled: false\n  payment", "  enabled: true\n  payment"),
        ("default_interval_seconds: 60", "default_interval_seconds: 5"), ("respect_rate_limits: true", "respect_rate_limits: false"),
        ('timezone: "Asia/Kolkata"', 'timezone: "UTC"'), ("adaptive_polling", "adaptive_poling"), ('date: "2026-12-19"', 'date: "soon"'),
    ],
)
def test_bad_yaml_is_refused(tmp_path, old, new):
    text = BASE.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "c.yaml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path, {})


def test_provider_order_from_environment():
    cfg = load_config(BASE, {"NOTIFY_PROVIDERS": "ntfy,pushover", "NTFY_TOPIC": "x" * 20, "PUSHOVER_APP_TOKEN": "a", "PUSHOVER_USER_KEY": "b"})
    from app.notifications.dispatcher import build_providers

    assert [p.name for p in build_providers(cfg, httpx.AsyncClient())] == ["ntfy", "pushover"]


@pytest.mark.parametrize(
    ("text", "value"),
    [("02:00 PM", dt.time(14)), ("6:00 pm", dt.time(18)), ("12:00 AM", dt.time(0)), ("12:15 PM", dt.time(12, 15)), ("14:00", None), ("25:00 PM", None), ("", None), ("13:00 PM", None)],
)
def test_clock_parsing(text, value):
    assert parse_clock(text) == value


def test_repeated_errors_do_not_flood_logs():
    f = RepeatFilter(window_seconds=1000)
    record = logging.LogRecord("x", logging.ERROR, "", 0, "same error", None, None)
    assert [f.filter(record) for _ in range(5)] == [True, False, False, False, False]
    info = logging.LogRecord("x", logging.INFO, "", 0, "fine", None, None)
    assert f.filter(info) and f.filter(info)
    assert json.loads(JsonFormatter().format(record))["level"] == "ERROR"
