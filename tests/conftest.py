"""Shared fixtures. No test contacts a booking platform or a notification service."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.config import Aliases, AppConfig
from app.database.repository import Repository
from app.domain import Observation, PlatformResult, SeatInfo, SeatStatus, State, Target
from app.monitor.engine import Engine
from app.monitor.events import PLATFORM_NAMES
from app.notifications.base import DeliveryError, Message, Provider

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.pvrcinemas.com/seatlayout/abc=="


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def target() -> Target:
    return Target(date=dt.date(2026, 12, 19), earliest_time=dt.time(14), latest_time=dt.time(18))


@pytest.fixture
def aliases() -> Aliases:
    return Aliases()


def make_obs(**changes: Any) -> Observation:
    base: dict[str, Any] = {
        "platform": "pvr",
        "movie": "AVENGERS: DOOMSDAY",
        "movie_id": "35312",
        "cinema": "INOX Pacific Mall, Jasola Delhi",
        "city": "Delhi",
        "date": dt.date(2026, 12, 19),
        "language": "English",
        "format": "SCREEN X 3D",
        "showtime": dt.time(15, 30),
        "bookable": True,
        "booking_url": URL,
        "evidence": {"source_url": "fixture"},
    }
    return Observation(**{**base, **changes})


def seats(count: int, few: int = 20) -> SeatInfo:
    status = SeatStatus.SOLD_OUT if count == 0 else SeatStatus.LIMITED if count <= few else SeatStatus.AVAILABLE
    return SeatInfo(status=status, available=count, total=171)


def ok_result(*obs: Observation, platform: str = "pvr", **changes: Any) -> PlatformResult:
    return PlatformResult(
        **{"platform": platform, "state": State.UNAVAILABLE, "observations": list(obs), "listing_complete": True, **changes}
    )


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    repository = Repository(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await repository.start({k: (v, True) for k, v in PLATFORM_NAMES.items()})
    yield repository
    await repository.close()


@pytest.fixture
def engine(repo: Repository, target: Target, aliases: Aliases) -> Engine:
    return Engine(repo, target, aliases)


@pytest.fixture
def config(tmp_path: Path, target: Target) -> AppConfig:
    return AppConfig(target=target, data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


class FakeProvider(Provider):
    def __init__(self, name: str = "fake", failures: int = 0, permanent: bool = False) -> None:
        self.name = name
        self.failures = failures
        self.permanent = permanent
        self.sent: list[Message] = []
        self.attempts = 0

    async def send(self, message: Message) -> None:
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise DeliveryError("simulated outage", permanent=self.permanent)
        self.sent.append(message)
