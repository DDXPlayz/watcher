"""Pure parsers for what PVR's public pages display. No network, no browser.

Structure verified against pages captured on 22 Nov 2026 (tests/fixtures/pvr_*.html).
"""

from __future__ import annotations

import datetime as dt
import re

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

from app.domain import Observation, SeatCategory, SeatInfo, SeatStatus
from app.monitor.matcher import Matcher, classify_screenx, normalize
from app.utils.time import parse_clock, to_ist, utcnow

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KNOWN_CITIES = {"delhi", "noida", "greater noida", "gurugram", "gurgaon", "ghaziabad", "faridabad"}


class ParseFailure(Exception):
    pass


class DateTab(BaseModel):
    index: int
    day: int
    month: str
    weekday: str
    active: bool

    def matches(self, date: dt.date, today: dt.date | None = None) -> bool:
        """PVR shows no year. The weekday has to agree with the calendar, which pins the year.

        Near the day itself PVR replaces the weekday with 'Today' or 'Tomorrow' (Indian time).
        """
        if self.day != date.day or self.month[:3].casefold() != MONTHS[date.month - 1]:
            return False
        today = today or to_ist(utcnow()).date()
        label = self.weekday.strip().casefold()
        if label in ("today", "tomorrow"):
            return date == today + dt.timedelta(days=0 if label == "today" else 1)
        return label[:3] == WEEKDAYS[date.weekday()]

    def label(self) -> str:
        return f"{self.day:02d} {self.month}"  # without the weekday, which changes to 'Today' near the date


class Slot(BaseModel):
    index: int
    time_text: str
    note: str
    language: str
    dimension: str
    status: str  # available | filling | sold_out | lapsed | unknown


class Group(BaseModel):
    index: int
    header: str
    slots: list[Slot]


class Cinema(BaseModel):
    index: int
    name: str
    groups: list[Group]


class MoviePage(BaseModel):
    title: str
    dates: list[DateTab]
    cinemas: list[Cinema] = Field(default_factory=list)

    @property
    def active_date(self) -> DateTab | None:
        active = [d for d in self.dates if d.active]
        return active[0] if len(active) == 1 else None

    @property
    def slot_count(self) -> int:
        return sum(len(g.slots) for c in self.cinemas for g in c.groups)


class SlotRef(BaseModel):
    """Where a slot sits in the page, so the browser can open exactly that one."""

    cinema: int
    group: int
    slot: int
    time_text: str


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ").split()) if node else ""


def _slot_status(classes: list[str]) -> str:
    colours = [c for c in classes if c.startswith("seat-color")]
    if not colours:
        return "available"
    return {"seat-color-yellow": "filling", "seat-color-red": "sold_out", "seat-color-disable": "lapsed"}.get(colours[0], "unknown")


_LANG_DIM = re.compile(r"^(?P<lang>.*?)\s*(?:-\s*(?P<dim>2D|3D))?$", re.IGNORECASE)


def parse_movie_page(html: str) -> MoviePage:
    soup = BeautifulSoup(html, "lxml")
    title = _text(soup.select_one(".movie-names h1"))
    if not title:
        raise ParseFailure("film title not found")
    dates: list[DateTab] = []
    for index, item in enumerate(soup.select(".calnder-current.fordesktop li")):
        box = item.find("div")
        day = _text(item.select_one("h5"))
        if not isinstance(box, Tag) or not day.isdigit():
            raise ParseFailure("date tab without a day number")
        dates.append(
            DateTab(
                index=index,
                day=int(day),
                month=_text(item.select_one(".month")),
                weekday=_text(item.select_one(".day")),
                active="dates-pvr-active" in (box.get("class") or []),
            )
        )
    if not dates:
        raise ParseFailure("no date tabs found")

    cinemas: list[Cinema] = []
    for c_index, tab in enumerate(soup.select(".p-accordion-tab")):
        name = _text(tab.select_one(".cinema-listed-locat h2"))
        if not name:
            raise ParseFailure("cinema block without a name")
        groups: list[Group] = []
        for g_index, wrapper in enumerate(tab.select(".select-slot-movies")):
            parent = wrapper.parent
            header = _text(parent.select_one(".cinemas-name h2")) if parent else ""
            slots: list[Slot] = []
            for s_index, box in enumerate(wrapper.select(".box-slot-moviesession")):
                inner = box.select_one(".time-select")
                if inner is None:
                    raise ParseFailure("showtime without a status element")
                match = _LANG_DIM.match(_text(box.select_one(".eng h6")))
                slots.append(
                    Slot(
                        index=s_index,
                        time_text=_text(box.select_one(".show-times h5")),
                        note=_text(box.select_one(".show-times p")),
                        language=(match["lang"] if match else "").strip(),
                        dimension=(match["dim"] or "").upper() if match else "",
                        status=_slot_status(list(inner.get("class") or [])),
                    )
                )
            groups.append(Group(index=g_index, header=header, slots=slots))
        cinemas.append(Cinema(index=c_index, name=name, groups=groups))
    return MoviePage(title=title, dates=dates, cinemas=cinemas)


def city_from_cinema(name: str) -> str | None:
    """PVR ends venue names with the city ('INOX Pacific Mall, Jasola Delhi')."""
    words = normalize(name).split()
    for size in (2, 1):
        if " ".join(words[-size:]) in KNOWN_CITIES:
            return " ".join(words[-size:]).title()
    return None


def format_text(group: Group, slot: Slot) -> str:
    """Everything PVR says about the auditorium and projection, in one string for the matcher."""
    parts = [group.header, slot.note, slot.dimension]
    return " ".join(dict.fromkeys(p for p in parts if p)) or "2D"


def observations(
    page: MoviePage, matcher: Matcher, movie_id: str, source_url: str, date: dt.date
) -> list[tuple[Observation, SlotRef]]:
    """Every showtime at the target cinema (or an ambiguously named Jasola one) on the active date."""
    out: list[tuple[Observation, SlotRef]] = []
    for cinema in page.cinemas:
        if not matcher.is_target_cinema(cinema.name) and "jasola" not in normalize(cinema.name):
            continue
        for group in cinema.groups:
            for slot in group.slots:
                listed = {"filling": SeatStatus.LIMITED, "available": SeatStatus.AVAILABLE, "sold_out": SeatStatus.SOLD_OUT}
                obs = Observation(
                    platform="pvr",
                    movie=page.title,
                    movie_id=movie_id,
                    cinema=cinema.name,
                    city=city_from_cinema(cinema.name),
                    date=date,
                    language=slot.language or None,
                    format=format_text(group, slot),
                    showtime=parse_clock(slot.time_text),
                    # A coloured slot alone is not proof. Only the seat page sets bookable to True.
                    bookable=False if slot.status in ("sold_out", "lapsed") else None,
                    sold_out=slot.status == "sold_out",
                    lapsed=slot.status == "lapsed",
                    seats=SeatInfo(status=listed.get(slot.status, SeatStatus.UNKNOWN)),
                    evidence={"source_url": source_url, "listing_status": slot.status, "group": group.header, "note": slot.note},
                )
                out.append((obs, SlotRef(cinema=cinema.index, group=group.index, slot=slot.index, time_text=slot.time_text)))
    return out


def venue_formats(obs: list[Observation]) -> list[str]:
    return sorted({f"{o.language or '?'} / {o.format or '?'}" for o in obs})


class SeatPage(BaseModel):
    movie: str
    language: str
    weekday: str
    day: int
    month: str
    time_text: str
    cinema: str
    seats: SeatInfo


_SUMMARY = re.compile(
    r"^(?P<weekday>[A-Za-z]{3})[a-z]*,\s*(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})[a-z]*,\s*(?P<time>\d{1,2}:\d{2}\s*[AaPp][Mm])"
)


def parse_seat_page(html: str, few_threshold: int) -> SeatPage:
    soup = BeautifulSoup(html, "lxml")
    summary = soup.select_one(".book-summary .summary-movies-content")
    if summary is None:
        raise ParseFailure("booking summary not found")
    paragraphs = [_text(p) for p in summary.select("p")]
    match = next((m for p in paragraphs if (m := _SUMMARY.match(p))), None)
    cinema = next((p for p in reversed(paragraphs) if not _SUMMARY.match(p)), "")
    items = [_text(li) for li in summary.select("li")]
    if match is None or not cinema or not items:
        raise ParseFailure("booking summary is incomplete")

    if not soup.select(".seat-layout .seats-row"):
        raise ParseFailure("seat map not found")
    free = soup.select(".seat-layout .seat-current-pvr")
    taken = soup.select(".seat-layout .seat-disable")
    if not free and not taken:
        raise ParseFailure("seat map has no seats")
    by_category: dict[str, int] = {}
    ids: list[str] = []
    for seat in free:
        # id looks like 'CL.CLUB|K:1' -> category CLUB, row K, seat 1
        area, _, place = str(seat.get("id") or "").partition("|")
        category = area.partition(".")[2] or area or "UNKNOWN"
        by_category[category] = by_category.get(category, 0) + 1
        if place:
            ids.append(place.replace(":", ""))
    prices = {
        _text(h.select_one(".seat-category-name")): _text(h.select_one(".seat-category-price")).strip("()")
        for h in soup.select(".seat-category-range")
    }
    count = len(free)
    status = SeatStatus.SOLD_OUT if count == 0 else SeatStatus.LIMITED if count <= few_threshold else SeatStatus.AVAILABLE
    names = list(dict.fromkeys([*prices, *by_category]))
    seats = SeatInfo(
        status=status,
        available=count,
        total=count + len(taken),
        categories=[SeatCategory(name=n, available=by_category.get(n, 0), price_text=prices.get(n)) for n in names if n],
        available_seats=ids,
    )
    return SeatPage(
        movie=_text(summary.select_one("h5")),
        language=items[-1],
        weekday=match["weekday"],
        day=int(match["day"]),
        month=match["month"],
        time_text=match["time"],
        cinema=cinema,
        seats=seats,
    )


def seat_page_mismatches(seat: SeatPage, obs: Observation, matcher: Matcher) -> list[str]:
    """Compare the seat page with the listing. An empty list means they describe the same show."""
    bad: list[str] = []
    if normalize(seat.movie) != normalize(obs.movie):
        bad.append(f"movie {seat.movie!r}")
    if not matcher.is_target_cinema(seat.cinema):
        bad.append(f"cinema {seat.cinema!r}")
    if normalize(seat.language) != normalize(obs.language):
        bad.append(f"language {seat.language!r}")
    date = obs.date
    if date is None or not DateTab(index=0, day=seat.day, month=seat.month, weekday=seat.weekday, active=True).matches(date):
        bad.append(f"date {seat.weekday} {seat.day} {seat.month}")
    if parse_clock(seat.time_text) != obs.showtime:
        bad.append(f"time {seat.time_text!r}")
    return bad


def is_screenx(obs: Observation) -> bool:
    return classify_screenx(obs.format)[0].value == "PASS"
