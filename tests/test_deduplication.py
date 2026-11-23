"""Engine transitions: what becomes an event, what becomes a notification, and what must not."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.database.models import NotificationRow, PlatformRow, ScreeningRow
from app.database.repository import Repository
from app.domain import ErrorKind, PlatformResult, State
from app.monitor.deduplication import entity_key, screening_key
from app.monitor.engine import Engine
from tests.conftest import make_obs, ok_result, seats


async def notes(repo: Repository) -> list[NotificationRow]:
    return list(reversed(await repo.notifications(500)))


async def event_types(repo: Repository) -> list[str]:
    return [e.type for e in reversed(await repo.events(500))]


def failure(kind: ErrorKind = ErrorKind.ACCESS_DENIED, platform: str = "pvr") -> PlatformResult:
    return PlatformResult(platform=platform, state=State.PLATFORM_ERROR, error_kind=kind, error_detail="HTTP 403")


async def test_first_match_alerts_once_and_stores_evidence(engine, repo):
    assert await engine.process(ok_result(make_obs(seats=seats(18)))) is State.AVAILABLE
    (note,) = await notes(repo)
    assert note.kind == "ticket" and note.url == "https://www.pvrcinemas.com/seatlayout/abc=="
    assert "SCREENX TICKETS AVAILABLE" in note.title
    for text in ("INOX Pacific Mall, Jasola", "19 December 2026", "English", "ScreenX", "3:30 PM IST", "PVR INOX", "18 seats visible"):
        assert text in note.body
    (row,) = await repo.screenings()
    assert row.evidence["verdicts"]["format"] == "PASS" and row.evidence["cinema_as_listed"].endswith("Jasola Delhi")


async def test_seat_count_is_omitted_when_unknown(engine, repo):
    await engine.process(ok_result(make_obs()))
    (note,) = await notes(repo)
    assert "Seat availability" not in note.body


async def test_unchanged_session_is_not_repeated(engine, repo):
    for _ in range(50):
        await engine.process(ok_result(make_obs(seats=seats(90))))
    assert len(await notes(repo)) == 1


async def test_duplicate_rows_on_one_page_are_one_screening(engine, repo):
    await engine.process(ok_result(make_obs(), make_obs()))
    assert len(await notes(repo)) == 1 and len(await repo.screenings()) == 1


async def test_restart_does_not_repeat(engine, repo, target, aliases, tmp_path):
    await engine.process(ok_result(make_obs()))
    await repo.close()
    again = Repository(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await again.start({"pvr": ("PVR INOX", True)})
    await Engine(again, target, aliases).process(ok_result(make_obs()))
    assert len(await again.notifications()) == 1
    await again.close()


async def test_sold_out_then_reopened_alerts_each_time(engine, repo):
    await engine.process(ok_result(make_obs()))
    assert await engine.process(ok_result(make_obs(bookable=False, sold_out=True))) is State.SOLD_OUT
    await engine.process(ok_result(make_obs(bookable=False, sold_out=True)))
    assert await engine.process(ok_result(make_obs())) is State.AVAILABLE
    await engine.process(ok_result(make_obs(bookable=False, sold_out=True)))
    await engine.process(ok_result(make_obs()))
    kinds = [n.title.split("—")[1].strip() for n in await notes(repo)]
    assert kinds == [
        "SCREENX TICKETS AVAILABLE",
        "SCREENX NOW SOLD OUT", "SCREENX AVAILABLE AGAIN",
        "SCREENX NOW SOLD OUT", "SCREENX AVAILABLE AGAIN",
    ]


async def test_sold_out_on_first_sight_is_not_the_sold_out_state(engine, repo):
    assert await engine.process(ok_result(make_obs(bookable=False, sold_out=True))) is State.UNAVAILABLE
    assert await notes(repo) == [] and "SCREENING_SEEN" in await event_types(repo)
    await engine.process(ok_result(make_obs()))
    assert len(await notes(repo)) == 1


async def test_disappearing_needs_two_clean_checks_then_return_alerts(engine, repo):
    await engine.process(ok_result(make_obs()))
    await engine.process(ok_result())
    assert (await repo.screenings())[0].availability == "AVAILABLE"
    await engine.process(ok_result())
    assert (await repo.screenings())[0].availability == "UNAVAILABLE"
    await engine.process(ok_result(make_obs()))
    assert [n.kind for n in await notes(repo)] == ["ticket", "ticket"]


async def test_single_missing_check_does_not_cause_a_repeat_alert(engine, repo):
    await engine.process(ok_result(make_obs()))
    await engine.process(ok_result())
    await engine.process(ok_result(make_obs()))
    assert len(await notes(repo)) == 1


async def test_platform_error_never_changes_known_state(engine, repo):
    await engine.process(ok_result(make_obs()))
    for _ in range(10):
        assert await engine.process(failure()) is State.PLATFORM_ERROR
    (row,) = await repo.screenings()
    assert row.availability == "AVAILABLE" and row.missing_count == 0
    platform = await repo.platform("pvr")
    assert platform.state == "PLATFORM_ERROR" and platform.last_good_state == "AVAILABLE" and platform.failure_count == 10
    assert len(await notes(repo)) == 1


async def test_identical_failures_make_one_event_and_recovery_makes_one(engine, repo):
    for _ in range(5):
        await engine.process(failure())
    await engine.process(failure(ErrorKind.TIMEOUT))
    await engine.process(ok_result())
    types = await event_types(repo)
    assert types.count("PLATFORM_FAILURE") == 2 and types.count("PLATFORM_RECOVERED") == 1
    assert await notes(repo) == []


async def test_policy_states_are_not_failures(engine, repo):
    await engine.process(failure(ErrorKind.ACCESS_NOT_PERMITTED, "bookmyshow"))
    platform = await repo.platform("bookmyshow")
    assert platform.failure_count == 0 and platform.state == "PLATFORM_ERROR"
    assert "PLATFORM_FAILURE" not in await event_types(repo) and await repo.recent_checks() == []


async def test_wrong_screenings_never_alert(engine, repo):
    wrong = [
        make_obs(language="Hindi"), make_obs(format="IMAX 3D"), make_obs(showtime=dt.time(13, 59)), make_obs(showtime=dt.time(18, 1)),
        make_obs(cinema="INOX Nehru Place Delhi"), make_obs(date=dt.date(2026, 12, 18)), make_obs(movie="Dune Part Three"),
    ]
    assert await engine.process(ok_result(*wrong)) is State.UNAVAILABLE
    assert await notes(repo) == [] and await repo.screenings() == []


async def test_unverified_never_alerts_and_is_noted_once(engine, repo):
    for _ in range(4):
        assert await engine.process(ok_result(make_obs(bookable=None))) is State.UNVERIFIED
    assert await notes(repo) == []
    assert (await event_types(repo)).count("UNVERIFIED_LISTING") == 1


async def test_known_available_turning_unverifiable_is_kept_but_goes_stale(engine, repo):
    await engine.process(ok_result(make_obs()))
    before = (await repo.screenings())[0].last_verified
    await engine.process(ok_result(make_obs(bookable=None)))
    row = (await repo.screenings())[0]
    assert row.availability == "AVAILABLE" and row.last_verified == before and row.missing_count == 0
    await engine.process(ok_result(make_obs()))
    assert len(await notes(repo)) == 1


async def test_incomplete_listing_is_unverified_not_unavailable(engine, repo):
    await engine.process(ok_result(make_obs()))
    for _ in range(3):
        assert await engine.process(ok_result(listing_complete=False)) is State.UNVERIFIED
    assert (await repo.screenings())[0].availability == "AVAILABLE"


async def test_not_yet_listed_is_reported_as_such(engine):
    assert await engine.process(ok_result(state=State.NOT_YET_LISTED)) is State.NOT_YET_LISTED


async def test_same_show_on_two_platforms_keeps_both_links(engine, repo):
    await engine.process(ok_result(make_obs()))
    district = make_obs(
        platform="district", cinema="INOX Pacific Mall, Jasola, New Delhi", cinema_id="1018963", city="New Delhi",
        movie="Avengers: Doomsday", format="3D SCREEN X", session_id="70001", booking_url="https://www.district.in/x",
    )
    await engine.process(ok_result(district, platform="district"))
    rows = await repo.screenings()
    assert len(rows) == 2 and len({r.entity_key for r in rows}) == 1 and len({r.booking_url for r in rows}) == 2
    first, second = await notes(repo)
    assert "TICKETS AVAILABLE" in first.title and "ALSO ON DISTRICT" in second.title


async def test_similar_shows_are_not_merged():
    assert entity_key(make_obs()) != entity_key(make_obs(format="SCREEN X"))
    assert entity_key(make_obs()) != entity_key(make_obs(showtime=dt.time(15, 35)))
    assert screening_key(make_obs(session_id="a")) == screening_key(make_obs(session_id="b"))  # PVR token may rotate


async def test_two_matching_times_are_two_alerts(engine, repo):
    await engine.process(ok_result(make_obs(), make_obs(showtime=dt.time(17, 45))))
    assert len(await notes(repo)) == 2


async def test_seat_band_change_alerts_once_per_cooldown_and_snapshots(engine, repo):
    await engine.process(ok_result(make_obs(seats=seats(90))))
    await engine.process(ok_result(make_obs(seats=seats(89))))  # same band: recorded, not alerted
    assert len(await notes(repo)) == 1
    await engine.process(ok_result(make_obs(seats=seats(12))))
    await engine.process(ok_result(make_obs(seats=seats(60))))
    await engine.process(ok_result(make_obs(seats=seats(9))))
    assert [n.kind for n in await notes(repo)] == ["ticket", "update"]
    row = (await repo.screenings())[0]
    assert row.seats_available == 9 and (await repo.latest_snapshot(row.id)).available == 9


async def test_new_dates_and_formats_become_events_after_baseline(engine, repo):
    await engine.process(ok_result(signals={"dates": ["17 Dec"], "venue_formats": []}))
    await engine.process(ok_result(signals={"dates": ["17 Dec", "19 Dec"], "venue_formats": ["English / SCREEN X 3D"]}))
    types = await event_types(repo)
    assert types.count("NEW_DATE") == 1 and types.count("NEW_FORMAT") == 1 and await notes(repo) == []


async def test_failed_transaction_leaves_nothing_behind(engine, repo, monkeypatch):
    async def boom(*_, **__):
        raise RuntimeError("disk full")

    monkeypatch.setattr(engine, "_queue", boom)
    try:
        await engine.process(ok_result(make_obs()))
    except RuntimeError:
        pass
    monkeypatch.undo()
    assert await repo.screenings() == [] and await notes(repo) == []
    await engine.process(ok_result(make_obs()))  # the next check finds it again
    assert len(await notes(repo)) == 1


async def test_target_change_stops_old_screenings_counting(engine, repo, target):
    await engine.process(ok_result(make_obs()))
    engine.set_target(target.model_copy(update={"date": dt.date(2026, 12, 20)}))
    for _ in range(3):
        await engine.process(ok_result(state=State.NOT_YET_LISTED))
    async with repo.transaction() as s:
        row = await s.scalar(select(ScreeningRow))
        platform = await s.get(PlatformRow, "pvr")
    assert row.missing_count == 0 and platform.state == "NOT_YET_LISTED"
