"""PVR INOX: renders the public film page in an ordinary headless Chromium and reads what it displays.

The browser is not disguised: default headless user agent, webdriver flag untouched, no proxies. The
adapter never calls PVR's API itself, never signs in and never selects a seat. If PVR refuses the
browser, the check fails as ACCESS_DENIED and backs off.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.config import Aliases, PvrConfig
from app.domain import ErrorKind, HealthStatus, Observation, PlatformResult, SeatInfo, State, Target
from app.monitor.deduplication import screening_key
from app.monitor.matcher import Matcher, normalize
from app.platforms.base import BookingPlatform, PlatformFailure, failure_for_status, looks_like_challenge
from app.platforms.pvr_parser import (
    MoviePage,
    ParseFailure,
    SlotRef,
    observations,
    parse_movie_page,
    parse_seat_page,
    seat_page_mismatches,
    venue_formats,
)

TABS = ".calnder-current.fordesktop li"
LISTING_CALL = "/booking/content/msessions"
REFUSAL = re.compile(
    r"sold ?out|not available|unavailable|no longer|expired|lapsed|closed for|housefull|house full|error|went wrong|try again|no record",
    re.IGNORECASE,
)


class _Verified:
    def __init__(self, url: str, seats: SeatInfo, listing_status: str) -> None:
        self.url = url
        self.seats = seats
        self.listing_status = listing_status
        self.at = time.monotonic()


class PvrPlatform(BookingPlatform):
    name = "pvr"

    def __init__(self, cfg: PvrConfig, aliases: Aliases, *, timeout_ms: int = 25_000) -> None:
        self.cfg = cfg
        self.aliases = aliases
        self.timeout_ms = timeout_ms
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._checks = 0
        self._listing_status: int | None = None
        self._verified: dict[str, _Verified] = {}

    # -- browser lifecycle ------------------------------------------------------------------------

    async def _ensure_page(self) -> Page:
        if self._checks and self._checks % self.cfg.recycle_browser_after_checks == 0:
            await self.reset()  # bounded memory over weeks of running
        if self._browser is not None and not self._browser.is_connected():
            await self.reset()
        if self._page is None or self._page.is_closed():
            if self._pw is None:
                self._pw = await async_playwright().start()
            if self._browser is None:
                self._browser = await self._pw.chromium.launch(headless=True)
            if self._context is None:
                self._context = await self._browser.new_context(
                    locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1400, "height": 1000}
                )
                await self._context.route("**/*", self._lighten)
            self._page = await self._context.new_page()
            self._page.on("response", self._note_response)
        return self._page

    @staticmethod
    async def _lighten(route: Route) -> None:
        # Pictures and fonts are not needed to read text; skipping them keeps our load on the site small.
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    def _note_response(self, response: Response) -> None:
        if LISTING_CALL in response.url:
            self._listing_status = response.status

    async def reset(self) -> None:
        for item in (self._context, self._browser):
            if item is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(item.close(), 10)
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._pw.stop(), 10)
        self._pw = self._browser = self._context = self._page = None

    async def close(self) -> None:
        await self.reset()

    async def health_check(self) -> HealthStatus:
        connected = self._browser is not None and self._browser.is_connected()
        return HealthStatus(platform=self.name, operational=connected, detail="browser connected" if connected else "browser not running")

    # -- check ------------------------------------------------------------------------------------

    async def check_availability(self, target: Target) -> PlatformResult:
        try:
            page = await self._ensure_page()
            self._checks += 1
            return await self._check(page, target)
        except PlatformFailure as failure:
            return self.error(failure.kind, failure.detail, failure.retry_after)
        except ParseFailure as failure:
            return self.error(ErrorKind.PARSE_ERROR, f"page structure not recognised: {failure}")
        except PlaywrightTimeout as exc:
            return self.error(ErrorKind.TIMEOUT, str(exc).splitlines()[0])
        except PlaywrightError as exc:
            await self.reset()
            return self.error(ErrorKind.NETWORK, str(exc).splitlines()[0])

    async def _check(self, page: Page, target: Target) -> PlatformResult:
        matcher = Matcher(target, self.aliases)
        await self._open_listing(page)
        listing = parse_movie_page(await page.content())
        if normalize(listing.title) not in {normalize(m) for m in [target.movie, *self.aliases.movie]}:
            raise PlatformFailure(ErrorKind.PARSE_ERROR, f"page shows {listing.title!r}; the film's URL may have changed")
        signals: dict[str, Any] = {"dates": [d.label() for d in listing.dates]}

        tab = next((d for d in listing.dates if d.matches(target.date)), None)
        if tab is None:
            return PlatformResult(
                platform=self.name,
                state=State.NOT_YET_LISTED,
                listing_complete=True,
                signals=signals,
                error_detail="the target date is not offered yet",
            )
        if not tab.active:
            async with page.expect_response(lambda r: LISTING_CALL in r.url, timeout=self.timeout_ms) as info:
                await page.locator(TABS).nth(tab.index).locator("div").first.click(timeout=self.timeout_ms)
            response = await info.value
            if failure := failure_for_status(response.status, response.headers.get("retry-after")):
                raise failure
        listing = await self._settled(page, target)

        found = observations(listing, matcher, self.cfg.movie_id, page.url, target.date)
        signals["venue_formats"] = venue_formats([o for o, _ in found])
        signals["cinemas_listed"] = len(listing.cinemas)
        candidates = [(o, r) for o, r in found if o.bookable is None and matcher.evaluate(o).identity_match]
        for position, (obs, ref) in enumerate(candidates, start=1):
            await self._verify(page, obs, ref, matcher, target, last=position == len(candidates))
        results = [o for o, _ in found]
        keys = {screening_key(o) for o in results}
        self._verified = {k: v for k, v in self._verified.items() if k in keys}
        return PlatformResult(
            platform=self.name,
            state=State.UNAVAILABLE if results else State.NOT_YET_LISTED,
            observations=results,
            listing_complete=True,
            signals=signals,
            error_detail=None if results else "the target cinema has no shows listed on the target date",
        )

    async def _open_listing(self, page: Page) -> None:
        self._listing_status = None
        await self._goto(page, self.cfg.movie_url)
        if not await self._region_is_set(page):
            await self._choose_region(page)
            self._listing_status = None
            await self._goto(page, self.cfg.movie_url)
        try:
            await page.locator(TABS).first.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeout as exc:
            text = await page.evaluate("document.body.innerText")
            if looks_like_challenge(text):
                raise PlatformFailure(ErrorKind.ACCESS_DENIED, "the site showed a block page") from exc
            if self._listing_status and (failure := failure_for_status(self._listing_status)):
                raise failure from exc
            raise PlatformFailure(ErrorKind.RENDER_ERROR, "the showtime page did not finish loading") from exc

    async def _goto(self, page: Page, url: str) -> None:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if response is None:
            raise PlatformFailure(ErrorKind.NETWORK, "no response")
        if failure := failure_for_status(response.status, response.headers.get("retry-after")):
            raise failure
        if looks_like_challenge(await page.title()):
            raise PlatformFailure(ErrorKind.ACCESS_DENIED, "the site showed a block page")

    async def _region_is_set(self, page: Page) -> bool:
        return await page.evaluate("() => localStorage.getItem('city')") == self.cfg.region

    async def _choose_region(self, page: Page) -> None:
        """Pick the region through the site's own dialog, as any first-time visitor must."""
        try:
            await page.get_by_text(self.cfg.region, exact=True).first.click(timeout=self.timeout_ms)
            await page.get_by_text(self.cfg.sub_region, exact=True).first.click(timeout=self.timeout_ms)
            await page.wait_for_function(
                "region => localStorage.getItem('city') === region", arg=self.cfg.region, timeout=self.timeout_ms
            )
        except PlaywrightTimeout as exc:
            if await self._region_is_set(page):
                return  # the site set it on its own while the dialog was loading
            if looks_like_challenge(await page.evaluate("document.body.innerText")):
                raise PlatformFailure(ErrorKind.ACCESS_DENIED, "the site showed a block page") from exc
            raise PlatformFailure(ErrorKind.RENDER_ERROR, "the region dialog did not respond") from exc

    async def _settled(self, page: Page, target: Target) -> MoviePage:
        """Wait until the listing for the target date stops changing. An empty listing is never trusted."""
        deadline = time.monotonic() + self.timeout_ms / 1000
        previous: MoviePage | None = None
        while time.monotonic() < deadline:
            await page.wait_for_timeout(400)
            current = parse_movie_page(await page.content())
            active = current.active_date
            if active and active.matches(target.date) and current.slot_count and current == previous:
                return current
            previous = current
        raise PlatformFailure(ErrorKind.RENDER_ERROR, "the listing for the target date never became stable")

    # -- bookability ------------------------------------------------------------------------------

    async def _verify(
        self, page: Page, obs: Observation, ref: SlotRef, matcher: Matcher, target: Target, *, last: bool
    ) -> None:
        """Open the show's public seat map and read it. Sets bookable only on positive proof."""
        key = screening_key(obs)
        status = str(obs.evidence.get("listing_status"))
        cached = self._verified.get(key)
        if cached and cached.listing_status == status and time.monotonic() - cached.at < self.cfg.seat_reverify_seconds:
            obs.bookable, obs.booking_url, obs.seats = True, cached.url, cached.seats
            obs.session_id = cached.url.rsplit("/", 1)[-1] if "/seatlayout/" in cached.url else None
            return
        for attempt in (1, 2):
            try:
                await self._verify_once(page, obs, ref, matcher, status, key)
                obs.evidence.pop("verification_error", None)
                break
            except (PlatformFailure, ParseFailure, PlaywrightError) as exc:
                obs.evidence["verification_error"] = f"attempt {attempt}: {str(exc).splitlines()[0]}"[:200]
                if looks_like_challenge(await page.title()):
                    return
                if attempt == 1:
                    await self._return_to_listing(page, target)
        if not last:  # going back costs seconds; after the last show the alert matters more
            await self._return_to_listing(page, target)

    async def _verify_once(
        self, page: Page, obs: Observation, ref: SlotRef, matcher: Matcher, status: str, key: str
    ) -> None:
        slot = (
            page.locator(".p-accordion-tab").nth(ref.cinema).locator(".select-slot-movies").nth(ref.group)
            .locator(".box-slot-moviesession").nth(ref.slot)
        )
        if (await slot.locator(".show-times h5").inner_text(timeout=5000)).strip() != ref.time_text:
            raise ParseFailure("the listing moved before the show could be opened")
        await slot.locator(".time-select").click(timeout=10_000)
        if notice := await self._wait_for_seat_page(page):
            self._accept_gate_as_proof(page, obs, notice, status, key)
            return
        await page.locator(".seat-layout .seats-row").first.wait_for(state="attached", timeout=12_000)
        await page.locator(".book-summary .summary-movies-content p").first.wait_for(state="attached", timeout=10_000)
        seat = parse_seat_page(await page.content(), self.cfg.few_seats_threshold)
        if bad := seat_page_mismatches(seat, obs, matcher):
            raise ParseFailure("seat page describes a different show: " + ", ".join(bad))
        obs.booking_url = page.url
        obs.session_id = page.url.rsplit("/", 1)[-1]
        obs.seats = seat.seats
        obs.evidence["seat_page"] = {
            "url": page.url,
            "cinema": seat.cinema,
            "when": f"{seat.weekday} {seat.day} {seat.month} {seat.time_text}",
            "language": seat.language,
        }
        if seat.seats.available:
            obs.bookable = True
            self._verified[key] = _Verified(page.url, seat.seats, status)
        else:
            obs.bookable, obs.sold_out = False, True
            self._verified.pop(key, None)

    async def _wait_for_seat_page(self, page: Page) -> str | None:
        """Wait for the seat map. Returns the text of a notice if the site puts one in front of it."""
        deadline = time.monotonic() + 12
        notice = page.locator(".p-dialog.popup_rate")
        while time.monotonic() < deadline:
            if "/seatlayout/" in page.url:
                return None
            if await notice.count() and await notice.first.is_visible():
                buttons = [t.strip().casefold() for t in await notice.first.locator("button").all_inner_texts()]
                text = " ".join((await notice.first.inner_text()).split())
                if "accept" in buttons and not REFUSAL.search(text):
                    return text
                raise ParseFailure(f"the site answered the show with: {text[:150]}")
            await page.wait_for_timeout(150)
        raise ParseFailure("neither a seat map nor a notice appeared")

    def _accept_gate_as_proof(self, page: Page, obs: Observation, notice: str, status: str, key: str) -> None:
        """Some venues require a notice to be accepted before the seat map. Accepting is the owner's decision,

        so the watcher stops here. The site marked this exact show as on sale and began its booking flow, which
        is enough to call it bookable. Seats stay uncounted and the link goes to the showtime page.
        """
        if status not in ("available", "filling"):
            raise ParseFailure(f"a notice appeared for a show listed as {status}")
        obs.bookable = True
        obs.booking_url = page.url
        obs.evidence["booking_gate"] = notice[:200]
        self._verified[key] = _Verified(page.url, obs.seats, status)

    async def _return_to_listing(self, page: Page, target: Target) -> None:
        """Needed to open another show. Failure here is not fatal: the next show just stays unverified."""
        with contextlib.suppress(PlatformFailure, ParseFailure, PlaywrightError):
            await self._open_listing(page)
            listing = parse_movie_page(await page.content())
            tab = next((d for d in listing.dates if d.matches(target.date)), None)
            if tab and not tab.active:
                await page.locator(TABS).nth(tab.index).locator("div").first.click(timeout=5000)
            await self._settled(page, target)
