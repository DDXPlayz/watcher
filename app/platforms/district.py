"""District: reads the session data embedded in the public cinema page. Off until the owner opts in.

district.in/robots.txt disallows every URL with a query string, and other dates are only reachable that
way (or through District's own API). So this adapter sees the page's default date plus the list of dates
the cinema has open. It reports a full match only when the page itself carries the target date's
sessions, and UNVERIFIED when all it can tell is that the date has opened.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from typing import Any

import httpx

from app.config import DistrictConfig
from app.domain import (
    ErrorKind,
    HealthStatus,
    Observation,
    PlatformResult,
    SeatCategory,
    SeatInfo,
    SeatStatus,
    State,
    Target,
)
from app.platforms.base import USER_AGENT, BookingPlatform, PlatformFailure, failure_for_status, looks_like_challenge
from app.utils.time import IST

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
TERMS_NOTE = "District's terms list scraping among prohibited uses. Idle until terms_ack is true."


def parse_cinema_page(html: str, cfg: DistrictConfig, target: Target) -> PlatformResult:
    match = _NEXT_DATA.search(html)
    if not match:
        raise PlatformFailure(ErrorKind.PARSE_ERROR, "embedded page data not found")
    try:
        data = json.loads(match[1])["props"]["pageProps"]["data"]
        state = data["serverState"][cfg.cinema_id]
        cinema = state["meta"]["cinema"]
        dates = [str(d) for d in state["data"]["sessionDates"]]
        films = state["arrangedSessions"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PlatformFailure(ErrorKind.PARSE_ERROR, f"embedded page data changed shape: {exc!r}") from exc
    if str(cinema.get("id")) != cfg.cinema_id:
        raise PlatformFailure(ErrorKind.PARSE_ERROR, f"page is for cinema {cinema.get('id')}, expected {cfg.cinema_id}")

    result: list[Observation] = []
    covered: set[str] = set()
    for film in films:
        for session in film.get("sessions") or []:
            covered.add(str(session.get("showDate")))
            if str(film.get("entityCode")) == cfg.movie_id:
                result.append(_observation(session, film, cinema, cfg, target))

    wanted = target.date.isoformat()
    signals: dict[str, Any] = {"dates": dates}
    if wanted in covered:
        return PlatformResult(
            platform="district", state=State.UNAVAILABLE, observations=result, listing_complete=True, signals=signals
        )
    if wanted in dates:
        return PlatformResult(
            platform="district",
            state=State.UNVERIFIED,
            observations=result,
            signals=signals,
            error_detail="the cinema has opened the target date, but its shows are not on the page robots.txt allows",
        )
    return PlatformResult(
        platform="district",
        state=State.NOT_YET_LISTED,
        observations=result,
        listing_complete=True,
        signals=signals,
        error_detail="the cinema has not opened the target date",
    )


def _observation(session: dict[str, Any], film: dict[str, Any], cinema: dict[str, Any], cfg: DistrictConfig, target: Target) -> Observation:
    starts = None
    try:
        parsed = dt.datetime.fromisoformat(str(session.get("showTimeISO")))
        starts = parsed.astimezone(IST) if parsed.tzinfo else None
    except ValueError:
        pass
    available = session.get("avail")
    blocked = session.get("disableClick")
    sold_out = available == 0
    bookable: bool | None = None
    if sold_out or blocked is True:
        bookable = False
    elif isinstance(available, int) and available > 0 and blocked is False:
        bookable = True
    colour = str(session.get("statusColor") or "")
    status = {"G": SeatStatus.AVAILABLE, "Y": SeatStatus.LIMITED}.get(colour, SeatStatus.UNKNOWN)
    address = str(cinema.get("address") or "")
    return Observation(
        platform="district",
        movie=film.get("entityName"),
        movie_id=str(film.get("entityCode")),
        cinema=cinema.get("name"),
        cinema_id=str(session.get("cid")),
        city="New Delhi" if "new delhi" in address.casefold() else None,
        date=starts.date() if starts else None,
        language=session.get("lang") or None,
        format=session.get("scrnFmt") or None,
        showtime=starts.time() if starts else None,
        bookable=bookable,
        sold_out=sold_out,
        session_id=str(session["sid"]) if session.get("sid") else None,
        booking_url=f"{cfg.movie_url}?frmtid={session.get('fid', '')}&fromdate={target.date.isoformat()}",
        seats=SeatInfo(
            status=SeatStatus.SOLD_OUT if sold_out else status,
            available=available if isinstance(available, int) else None,
            total=session.get("total") if isinstance(session.get("total"), int) else None,
            categories=[
                SeatCategory(
                    name=str(a.get("label")),
                    available=a.get("sAvail"),
                    total=a.get("sTotal"),
                    price_text=f"₹{a['price']}" if a.get("price") else None,
                )
                for a in session.get("areas") or []
            ],
        ),
        evidence={"source_url": cfg.cinema_url, "audi": session.get("audi"), "show_time_iso": session.get("showTimeISO")},
    )


def parse_film_formats(html: str) -> list[str] | None:
    """Screen formats District offers for the film in this city, from the public film page.

    A 'SCREEN X' entry is a city-wide early sign. It is not proof of anything at the target cinema.
    Returns None if the page does not carry the list, so a change on District cannot break the check.
    """
    match = _NEXT_DATA.search(html)
    if not match:
        return None
    try:
        state = json.loads(match[1])["props"]["pageProps"]["data"]["serverState"]["mdpV2MovieData"]
        buckets = next(iter(state.values()))["filterBuckets"]
    except (ValueError, KeyError, TypeError, StopIteration):
        return None
    formats: list[str] = []
    for bucket in buckets:
        for item in bucket.get("buckets") or []:
            try:
                key = str(item.get("key") or "")
                kind = json.loads(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))).get("type")
            except (ValueError, TypeError, AttributeError):
                continue
            if kind == "SCREEN_FORMAT" and item.get("title"):
                formats.append(str(item["title"]))
    return sorted(set(formats))


class DistrictPlatform(BookingPlatform):
    name = "district"

    def __init__(self, cfg: DistrictConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN"}, timeout=30, follow_redirects=True, transport=transport
        )

    async def check_availability(self, target: Target) -> PlatformResult:
        if not self.cfg.terms_ack:
            return self.error(ErrorKind.AWAITING_TERMS_DECISION, TERMS_NOTE)
        try:
            response = await self._client.get(self.cfg.cinema_url)
            if failure := failure_for_status(response.status_code, response.headers.get("retry-after")):
                raise failure
            if looks_like_challenge(response.text):
                raise PlatformFailure(ErrorKind.ACCESS_DENIED, "the site showed a block page")
            result = parse_cinema_page(response.text, self.cfg, target)
            if (formats := await self._film_formats()) is not None:
                result.signals["film_formats"] = formats
            return result
        except PlatformFailure as failure:
            return self.error(failure.kind, failure.detail, failure.retry_after)
        except httpx.TimeoutException:
            return self.error(ErrorKind.TIMEOUT, "request timed out")
        except httpx.HTTPError as exc:
            return self.error(ErrorKind.NETWORK, type(exc).__name__)

    async def _film_formats(self) -> list[str] | None:
        """Best effort: a failure here never fails the check."""
        try:
            response = await self._client.get(self.cfg.movie_url)
        except httpx.HTTPError:
            return None
        return parse_film_formats(response.text) if response.status_code == 200 else None

    async def health_check(self) -> HealthStatus:
        return HealthStatus(platform=self.name, operational=self.cfg.terms_ack, detail="" if self.cfg.terms_ack else TERMS_NOTE)

    async def close(self) -> None:
        await self._client.aclose()
