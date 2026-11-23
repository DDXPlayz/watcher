from __future__ import annotations

import datetime as dt

import pytest

from app.domain import State, Target
from app.monitor.matcher import Matcher, Verdict, classify_screenx
from tests.conftest import make_obs


@pytest.fixture
def matcher(target, aliases) -> Matcher:
    return Matcher(target, aliases)


def test_exact_combination_qualifies(matcher):
    decision = matcher.evaluate(make_obs())
    assert decision.qualified and decision.state is State.AVAILABLE and decision.reasons == []


@pytest.mark.parametrize(
    "change",
    [
        {"movie": "Avengers: Endgame Encore"},
        {"movie": "Avengers: Secret Wars"},
        {"movie": "Doomsday"},
        {"cinema": "PVR Pacific, Subhash Nagar, New Delhi"},
        {"cinema": "PVR Pacific D21, Dwarka, New Delhi"},
        {"cinema": "INOX Nehru Place Delhi"},
        {"city": "Noida"},
        {"city": "Mumbai"},
        {"date": dt.date(2026, 12, 18)},
        {"date": dt.date(2026, 12, 20)},
        {"date": dt.date(2027, 12, 19)},
        {"language": "Hindi"},
        {"language": "Tamil"},
        {"format": "2D"},
        {"format": "3D"},
        {"format": "IMAX 3D"},
        {"format": "4DX 3D"},
        {"format": "MS INFINITY VISION 3D"},
        {"format": "BIGPIX [ATMOS] 3D"},
        {"format": "INSIGNIA"},
        {"format": "SCREEN 5"},
        {"showtime": dt.time(13, 59)},
        {"showtime": dt.time(18, 1)},
        {"showtime": dt.time(3, 30)},
    ],
)
def test_any_wrong_field_is_irrelevant(matcher, change):
    decision = matcher.evaluate(make_obs(**change))
    assert not decision.qualified and decision.state is None


@pytest.mark.parametrize("when", [dt.time(14, 0), dt.time(18, 0), dt.time(15, 30)])
def test_window_is_inclusive(matcher, when):
    assert matcher.evaluate(make_obs(showtime=when)).qualified


def test_one_second_after_window_fails(matcher):
    assert not matcher.evaluate(make_obs(showtime=dt.time(18, 0, 1))).qualified


@pytest.mark.parametrize("field", ["movie", "cinema", "city", "date", "language", "format", "showtime", "bookable", "booking_url"])
def test_any_missing_field_is_unverified(matcher, field):
    decision = matcher.evaluate(make_obs(**{field: None}))
    assert not decision.qualified and decision.state is State.UNVERIFIED


@pytest.mark.parametrize("field", ["movie", "cinema", "city", "language", "format"])
def test_blank_text_is_unverified_not_a_match(matcher, field):
    assert matcher.evaluate(make_obs(**{field: "  "})).state is State.UNVERIFIED


@pytest.mark.parametrize("cinema", ["INOX Jasola", "INOX Pacific Mall", "PVR Jasola Delhi", "INOX: Pacific Mall, Jasola (Audi 5)"])
def test_ambiguous_cinema_is_unverified(matcher, cinema):
    assert matcher.evaluate(make_obs(cinema=cinema)).state is State.UNVERIFIED


@pytest.mark.parametrize("movie", ["Avengers: Doomsday (Fan Event)", "AVENGERS: DOOMSDAY ENCORE"])
def test_film_variants_are_unverified(matcher, movie):
    assert matcher.evaluate(make_obs(movie=movie)).state is State.UNVERIFIED


def test_foreign_timezone_is_unverified(matcher):
    assert matcher.evaluate(make_obs(timezone="UTC")).state is State.UNVERIFIED


@pytest.mark.parametrize(
    ("change", "state"),
    [({"sold_out": True}, State.SOLD_OUT), ({"lapsed": True}, State.UNAVAILABLE), ({"bookable": False}, State.UNAVAILABLE)],
)
def test_not_bookable_never_qualifies(matcher, change, state):
    decision = matcher.evaluate(make_obs(**change))
    assert not decision.qualified and decision.state is state


@pytest.mark.parametrize(
    ("text", "verdict", "dimension"),
    [
        ("SCREEN X", Verdict.PASS, ""),
        ("ScreenX", Verdict.PASS, ""),
        ("SCREEN-X 2D", Verdict.PASS, "2D"),
        ("3D SCREEN X", Verdict.PASS, "3D"),
        ("SCREEN X [ATMOS] 3D", Verdict.PASS, "3D"),
        ("MS INFINITY VISION SCREEN X", Verdict.PASS, ""),
        ("4DX SCREEN X", Verdict.UNKNOWN, ""),
        ("IMAX / SCREEN X", Verdict.UNKNOWN, ""),
        ("2D 3D SCREEN X", Verdict.UNKNOWN, "3D"),
        ("SCREEN 10", Verdict.FAIL, ""),
        ("WIDESCREEN XL", Verdict.FAIL, ""),
        ("4DX", Verdict.FAIL, ""),
        ("", Verdict.UNKNOWN, ""),
        (None, Verdict.UNKNOWN, ""),
    ],
)
def test_screenx_classification(text, verdict, dimension):
    assert classify_screenx(text) == (verdict, dimension)


def test_every_single_field_is_mandatory(matcher):
    """Sweep: each identity field made wrong or missing on its own must stop the match."""
    wrong = {
        "movie": "Dune Part Three", "cinema": "PVR Priya", "city": "Mumbai", "date": dt.date(2026, 12, 17),
        "language": "Hindi", "format": "IMAX", "showtime": dt.time(21), "bookable": False,
    }
    for field, value in wrong.items():
        assert not matcher.evaluate(make_obs(**{field: value})).qualified, field
        assert not matcher.evaluate(make_obs(**{field: None})).qualified, field


def test_target_rejects_other_timezones_and_reversed_window():
    with pytest.raises(ValueError):
        Target(date=dt.date(2026, 12, 19), earliest_time=dt.time(14), latest_time=dt.time(18), timezone="UTC")
    with pytest.raises(ValueError):
        Target(date=dt.date(2026, 12, 19), earliest_time=dt.time(18), latest_time=dt.time(14))
