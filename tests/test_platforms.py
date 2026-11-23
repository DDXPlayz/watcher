from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from app.config import BmsConfig, DistrictConfig, PvrConfig
from app.domain import ErrorKind, SeatStatus, State
from app.monitor.matcher import Matcher
from app.platforms.base import failure_for_status, looks_like_challenge
from app.platforms.bookmyshow import BookMyShowPlatform
from app.platforms.district import DistrictPlatform, parse_cinema_page
from app.platforms.pvr import PvrPlatform
from app.platforms.pvr_parser import (
    DateTab,
    ParseFailure,
    city_from_cinema,
    observations,
    parse_movie_page,
    parse_seat_page,
    seat_page_mismatches,
)
from tests.conftest import fixture_text, make_obs
from tests.fake_pvr import FakePvr, screenx_listing

# ---------------------------------------------------------------- PVR parsing, on real captured markup


def test_real_doomsday_page_has_no_target_venue(target, aliases):
    page = parse_movie_page(fixture_text("pvr_doomsday_19dec_trimmed.html"))
    assert page.title == "AVENGERS: DOOMSDAY"
    assert [d.day for d in page.dates] == [17, 18, 19, 20]
    assert page.active_date and page.active_date.matches(target.date)
    assert {c.name for c in page.cinemas} == {"INOX Nehru Place Delhi", "PVR Naraina Delhi"}
    assert observations(page, Matcher(target, aliases), "35312", "u", target.date) == []


def test_real_screenx_control_reads_format_language_and_status(target, aliases):
    page = parse_movie_page(fixture_text("pvr_screenx_control_trimmed.html"))
    jasola = next(c for c in page.cinemas if "Jasola" in c.name)
    group = next(g for g in jasola.groups if g.header == "SCREEN X")
    assert [(s.time_text, s.language, s.dimension, s.status) for s in group.slots] == [
        ("03:25 PM", "Hindi", "3D", "lapsed"), ("07:20 PM", "Hindi", "3D", "available"), ("11:15 PM", "Hindi", "3D", "available"),
    ]
    obs = [o for o, _ in observations(page, Matcher(target, aliases), "x", "u", target.date)]
    assert {o.format for o in obs} >= {"SCREEN X 3D", "BIGPIX [ATMOS] 3D", "INSIGNIA", "2D"}
    assert all(o.bookable is not True for o in obs), "a listing alone must never prove bookability"


@pytest.mark.parametrize(
    ("colour", "status"),
    [("", "available"), ("seat-color-yellow", "filling"), ("seat-color-red", "sold_out"), ("seat-color-disable", "lapsed"), ("seat-color-pink", "unknown")],
)
def test_slot_colours(colour, status):
    page = parse_movie_page(screenx_listing(colour=colour))
    group = next(g for c in page.cinemas for g in c.groups if g.header == "SCREEN X")
    assert group.slots[0].status == status


def test_real_seat_page():
    seat = parse_seat_page(fixture_text("pvr_seatlayout_trimmed.html"), 20)
    assert (seat.movie, seat.language, seat.weekday, seat.day, seat.month) == ("RAMAYANA", "Hindi", "Sun", 22, "Nov")
    assert seat.cinema == "INOX Pacific Mall, Jasola Delhi"
    assert (seat.seats.available, seat.seats.total, seat.seats.status) == (150, 171, SeatStatus.AVAILABLE)
    assert {c.name for c in seat.seats.categories} == {"CLUB", "ROYAL", "ROYAL RECLINER"}
    assert "K1" in seat.seats.available_seats and len(seat.seats.available_seats) == 150


def test_seat_page_for_another_show_is_rejected(target, aliases):
    seat = parse_seat_page(fixture_text("pvr_seatlayout_trimmed.html"), 20)
    bad = seat_page_mismatches(seat, make_obs(), Matcher(target, aliases))
    assert {b.split()[0] for b in bad} == {"movie", "language", "date", "time"}


@pytest.mark.parametrize("html", ["", "<html><body>hello</body></html>", "<div class='movie-names'><h1>X</h1></div>"])
def test_unrecognised_pages_are_parse_failures(html):
    with pytest.raises(ParseFailure):
        parse_movie_page(html)
    with pytest.raises(ParseFailure):
        parse_seat_page(html, 20)


def test_date_labels_today_and_tomorrow_use_indian_calendar():
    day = dt.date(2026, 12, 19)

    def tab(label: str, number: int = 19, month: str = "Dec") -> DateTab:
        return DateTab(index=0, day=number, month=month, weekday=label, active=False)

    assert tab("Sat").matches(day) and tab("Sat").matches(day, dt.date(2026, 11, 22))
    assert tab("Today").matches(day, dt.date(2026, 12, 19)) and not tab("Today").matches(day, dt.date(2026, 12, 18))
    assert tab("Tomorrow").matches(day, dt.date(2026, 12, 18)) and not tab("Tomorrow").matches(day, dt.date(2026, 12, 19))
    assert not tab("Sun").matches(day)  # 19 Dec is a Sunday in 2027: wrong year
    assert not tab("Sat", 20).matches(day) and not tab("Sat", month="Nov").matches(day) and not tab("").matches(day)


def test_city_comes_from_venue_name():
    assert city_from_cinema("INOX Pacific Mall, Jasola Delhi") == "Delhi"
    assert city_from_cinema("PVR Superplex Mall Of India Noida") == "Noida"
    assert city_from_cinema("PVR Gaur City Greater Noida") == "Greater Noida"
    assert city_from_cinema("PVR Somewhere") is None


# ---------------------------------------------------------------- PVR adapter in a real browser, local site only


@pytest.fixture
def site() -> Iterator[FakePvr]:
    fake = FakePvr()
    yield fake
    fake.close()


@pytest.fixture
async def pvr(site: FakePvr, aliases) -> AsyncIterator[PvrPlatform]:
    platform = PvrPlatform(PvrConfig(movie_url=site.url), aliases, timeout_ms=4000)
    yield platform
    await platform.close()


async def test_pvr_absent_venue_is_not_yet_listed(pvr, site, target):
    result = await pvr.check_availability(target)
    assert (result.state, result.observations, result.listing_complete) == (State.NOT_YET_LISTED, [], True)
    assert result.signals["dates"] == ["17 Dec", "18 Dec", "19 Dec", "20 Dec"]


async def test_pvr_missing_date_is_not_yet_listed(pvr, site, target):
    site.dates = [("17", "Thu"), ("18", "Fri")]
    assert (await pvr.check_availability(target)).state is State.NOT_YET_LISTED


async def test_pvr_match_is_verified_on_the_seat_page(pvr, site, target, aliases):
    site.mode = "listed"
    result = await pvr.check_availability(target)
    matches = [o for o in result.observations if Matcher(target, aliases).evaluate(o).qualified]
    (obs,) = matches
    assert obs.showtime == dt.time(15, 30) and obs.booking_url == f"{site.base}/seatlayout/tokenABC=="
    assert (obs.seats.available, obs.seats.total) == (150, 171)
    assert obs.evidence["seat_page"]["when"] == "Sat 19 Dec 03:30 PM"


async def test_pvr_does_not_reopen_the_seat_page_every_check(pvr, site, target):
    site.mode = "listed"
    for _ in range(3):
        result = await pvr.check_availability(target)
    assert any(o.bookable for o in result.observations)
    assert sum("seatlayout" in h for h in site.hits) == 1


async def test_pvr_seat_page_for_wrong_show_stays_unverified(pvr, site, target, aliases):
    site.mode = "listed"
    site.seat_html = site.seat_html.replace("03:30 PM", "07:20 PM")
    result = await pvr.check_availability(target)
    states = [Matcher(target, aliases).evaluate(o).state for o in result.observations]
    assert State.UNVERIFIED in states and State.AVAILABLE not in states


async def test_pvr_full_house_is_sold_out(pvr, site, target, aliases):
    site.mode = "listed"
    site.seat_html = site.seat_html.replace("seat-current-pvr", "seat-disable")
    result = await pvr.check_availability(target)
    assert State.SOLD_OUT in [Matcher(target, aliases).evaluate(o).state for o in result.observations]


async def test_pvr_listing_marked_sold_out_is_not_opened(pvr, site, target, aliases):
    site.mode, site.listing = "listed", screenx_listing(colour="seat-color-red")
    result = await pvr.check_availability(target)
    assert State.SOLD_OUT in [Matcher(target, aliases).evaluate(o).state for o in result.observations]
    assert not any("seatlayout" in h for h in site.hits)


async def test_pvr_venue_notice_is_never_accepted(pvr, site, target, aliases):
    site.mode = "notice"
    result = await pvr.check_availability(target)
    (obs,) = [o for o in result.observations if Matcher(target, aliases).evaluate(o).qualified]
    assert obs.seats.available is None and obs.booking_url == site.url and "digital payments" in obs.evidence["booking_gate"]
    assert not any("accepted" in h or "seatlayout" in h for h in site.hits)


@pytest.mark.parametrize(
    ("notice", "buttons"),
    [("This show is sold out.", None), ("Session expired, please try again", None), ("Something went wrong", None), ("Enjoy the movie", ["OK", "Cancel"])],
)
async def test_pvr_refusal_dialogs_do_not_count_as_bookable(pvr, site, target, aliases, notice, buttons):
    site.mode, site.notice, site.notice_buttons = "notice", notice, buttons
    result = await pvr.check_availability(target)
    assert State.AVAILABLE not in [Matcher(target, aliases).evaluate(o).state for o in result.observations]


@pytest.mark.parametrize("change", [{"language": "Hindi"}, {"header": "BIGPIX"}, {"time_text": "01:55 PM"}, {"time_text": "06:05 PM"}])
async def test_pvr_near_matches_are_not_opened(pvr, site, target, change):
    site.mode, site.listing = "listed", screenx_listing(**change)
    result = await pvr.check_availability(target)
    assert result.state is State.UNAVAILABLE and result.observations
    assert not any("seatlayout" in h or "/click" in h for h in site.hits)


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("403", ErrorKind.ACCESS_DENIED), ("429", ErrorKind.RATE_LIMITED), ("500", ErrorKind.SERVER_ERROR), ("challenge", ErrorKind.ACCESS_DENIED),
        ("blank", ErrorKind.RENDER_ERROR), ("wrongfilm", ErrorKind.PARSE_ERROR), ("empty", ErrorKind.RENDER_ERROR), ("hang", ErrorKind.TIMEOUT),
    ],
)
async def test_pvr_failures_are_errors_never_unavailable(pvr, site, target, mode, kind):
    site.mode = mode
    result = await pvr.check_availability(target)
    assert (result.state, result.error_kind) == (State.PLATFORM_ERROR, kind)
    if mode == "429":
        assert result.retry_after == 77


async def test_pvr_listing_call_failure_is_server_error(pvr, site, target):
    await pvr.check_availability(target)
    site.mode = "api500"
    result = await pvr.check_availability(target)
    assert result.state is State.PLATFORM_ERROR and result.error_kind is ErrorKind.SERVER_ERROR


async def test_pvr_unreachable_then_recovers(pvr, site, target):
    good = pvr.cfg
    pvr.cfg = good.model_copy(update={"movie_url": "http://127.0.0.1:9/moviesessions/x"})
    assert (await pvr.check_availability(target)).error_kind is ErrorKind.NETWORK
    pvr.cfg = good
    assert (await pvr.check_availability(target)).state is State.NOT_YET_LISTED


async def test_pvr_survives_browser_crash(pvr, site, target):
    await pvr.check_availability(target)
    await pvr._browser.close()
    assert (await pvr.check_availability(target)).state is State.NOT_YET_LISTED


async def test_pvr_recycles_its_browser(pvr, site, target):
    pvr.cfg = pvr.cfg.model_copy(update={"recycle_browser_after_checks": 5})
    first = None
    for _ in range(6):
        assert (await pvr.check_availability(target)).ok
        first = first or pvr._browser
    assert pvr._browser is not first


# ---------------------------------------------------------------- District


def district_html(**session: object) -> str:
    """The captured page with one SYNTHETIC Doomsday session added."""
    html = fixture_text("district_jasola_trimmed.html")
    data = json.loads(re.search(r"json\">(.*?)</script>", html, re.DOTALL)[1])
    state = data["props"]["pageProps"]["data"]["serverState"]["1018963"]
    base = {
        "sid": "70001", "cid": 1018963, "fid": "fmt", "showTimeISO": "2026-12-19T15:30:00+05:30", "showDate": "2026-12-19", "avail": 40,
        "total": 153, "areas": [{"label": "CLUB", "sAvail": 40, "sTotal": 70, "price": 510}], "scrnFmt": "3D SCREEN X", "lang": "English",
        "statusColor": "G", "disableClick": False, "audi": "SCREEN 5",
    }
    state["arrangedSessions"].append({"entityCode": 176735, "entityName": "Avengers: Doomsday", "cinemaId": 1018963, "sessions": [{**base, **session}]})
    state["data"]["sessionDates"].append("2026-12-19")
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


CFG = DistrictConfig(terms_ack=True)


def test_district_real_page_is_not_yet_listed(target):
    result = parse_cinema_page(fixture_text("district_jasola_trimmed.html"), CFG, target)
    assert result.state is State.NOT_YET_LISTED and "2026-12-16" in result.signals["dates"]


def test_district_exact_match(target, aliases):
    result = parse_cinema_page(district_html(), CFG, target)
    (obs,) = result.observations
    assert Matcher(target, aliases).evaluate(obs).qualified and obs.seats.available == 40 and result.listing_complete


@pytest.mark.parametrize(
    ("change", "state"),
    [
        ({"lang": "Hindi"}, None), ({"scrnFmt": "2D"}, None), ({"showTimeISO": "2026-12-19T13:59:00+05:30"}, None),
        ({"lang": None}, State.UNVERIFIED), ({"scrnFmt": ""}, State.UNVERIFIED), ({"showTimeISO": "2026-12-19T15:30:00"}, State.UNVERIFIED),
        ({"disableClick": None}, State.UNVERIFIED), ({"avail": None}, State.UNVERIFIED), ({"avail": 0}, State.SOLD_OUT),
        ({"disableClick": True}, State.UNAVAILABLE),
        ({"showTimeISO": "2026-12-19T10:00:00+00:00"}, State.AVAILABLE),  # 3:30 PM IST written in UTC
        ({"showTimeISO": "2026-12-19T15:30:00+00:00"}, None),  # 9 PM IST
    ],
)
def test_district_variations(target, aliases, change, state):
    (obs,) = parse_cinema_page(district_html(**change), CFG, target).observations
    assert Matcher(target, aliases).evaluate(obs).state is state


def test_district_date_open_but_sessions_hidden_is_unverified(target):
    html = fixture_text("district_jasola_trimmed.html").replace('"2026-12-16"]', '"2026-12-19"]')
    result = parse_cinema_page(html, CFG, target)
    assert result.state is State.UNVERIFIED and not result.listing_complete


async def district_with(handler, ack: bool = True) -> DistrictPlatform:
    return DistrictPlatform(DistrictConfig(terms_ack=ack), transport=httpx.MockTransport(handler))


async def test_district_sends_nothing_until_acknowledged(target):
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("a request was made")

    platform = await district_with(handler, ack=False)
    assert (await platform.check_availability(target)).error_kind is ErrorKind.AWAITING_TERMS_DECISION
    await platform.close()


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(403), ErrorKind.ACCESS_DENIED), (httpx.Response(429, headers={"Retry-After": "90"}), ErrorKind.RATE_LIMITED),
        (httpx.Response(500), ErrorKind.SERVER_ERROR), (httpx.Response(200, text=""), ErrorKind.PARSE_ERROR),
        (httpx.Response(200, text="<script id=\"__NEXT_DATA__\">{bad</script>"), ErrorKind.PARSE_ERROR),
        (httpx.Response(200, text="<script id=\"__NEXT_DATA__\">{}</script>"), ErrorKind.PARSE_ERROR),
        (httpx.Response(200, text="Attention Required! You have been blocked"), ErrorKind.ACCESS_DENIED),
        (httpx.ReadTimeout("t"), ErrorKind.TIMEOUT), (httpx.ConnectError("c"), ErrorKind.NETWORK),
    ],
)
async def test_district_failures(target, response, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.query == b"", "robots.txt disallows query strings"
        assert "DoomsdayWatcher" in request.headers["user-agent"]
        if isinstance(response, Exception):
            raise response
        return response

    platform = await district_with(handler)
    result = await platform.check_availability(target)
    assert (result.state, result.error_kind) == (State.PLATFORM_ERROR, kind)
    await platform.close()


def test_district_config_refuses_query_urls():
    with pytest.raises(ValueError):
        DistrictConfig(cinema_url="https://www.district.in/movies/x?fromdate=2026-12-19")


# ---------------------------------------------------------------- BookMyShow and shared helpers


async def test_bookmyshow_is_never_reported_as_unavailable(target):
    result = await BookMyShowPlatform(BmsConfig()).check_availability(target)
    assert (result.state, result.error_kind) == (State.PLATFORM_ERROR, ErrorKind.ACCESS_NOT_PERMITTED)


def test_status_mapping():
    assert failure_for_status(200) is None
    assert failure_for_status(429).retry_after == 300 and failure_for_status(429, "12").retry_after == 12
    assert failure_for_status(503).kind is ErrorKind.SERVER_ERROR and failure_for_status(404).kind is ErrorKind.PARSE_ERROR


def test_challenge_detection_ignores_long_normal_pages():
    assert looks_like_challenge("Access Denied") and not looks_like_challenge("captcha " + "x" * 7000)
