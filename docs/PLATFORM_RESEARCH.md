# Platform research

Carried out on 22 November 2026 from an Indian residential connection. "Verified" means observed that day. "Assumed" means not proven.

## Where things stood

| | PVR INOX | BookMyShow | District |
|---|---|---|---|
| Film on sale in Delhi-NCR | Yes | Yes | Yes |
| English formats offered | Infinity Vision 2D, 3D, 4DX, ICE | Infinity Vision 2D, 3D, 4DX, ICE | Infinity Vision 2D, 3D, 4DX, ICE |
| English ScreenX offered | No | No | No |
| Target cinema listed for the film on 19 Dec | No (25 cinemas listed) | Not checked at venue level | No |

Press reports say advance booking opened on 17 November with premium screens first and other formats to follow. So the target show had not been released when this was written.

## Decision per platform

| Platform | Decision | Why |
|---|---|---|
| PVR INOX | Automated, every 60 s | Serves an undisguised headless browser. robots.txt allows the page. Terms do not mention bots; see the caveat below. |
| District | Automated since 23 Nov, at your request | Terms list "scrapes or crawls" among prohibited uses; you chose to accept that. robots.txt is still respected. |
| BookMyShow | Not automated | Terms prohibit scrapers without written permission, and Cloudflare blocks every honest automated client tried. |

## PVR INOX

**Verified**

- The film page is a React app. Raw HTML is the same 7 KB shell for every route, so a browser is needed.
- A default headless Chromium (`HeadlessChrome` user agent, `navigator.webdriver` true, no stealth) is served normally.
- A first visit must choose a region. "Delhi-NCR" then "All" goes to the home page and stores `city` in localStorage. After that the film URL shows listings.
- Dates are tabs with day, month and weekday, and no year. **Near the date the weekday becomes "Today" or "Tomorrow".** The parser handles this with the Indian calendar; the weekday otherwise pins the year.
- Shows are grouped by cinema, then by auditorium heading: `SCREEN X`, `BIGPIX [ATMOS]`, `INSIGNIA`, `4DX`, `IMAX`, `PXL [ATMOS]`, or no heading. Each show has a note (`3D`, `MS - INFINITY VISION`) and a language line (`English -3D`).
- The venue appears as `INOX Pacific Mall, Jasola Delhi`.
- Status is a CSS class: none = available, `seat-color-yellow` = filling, `seat-color-red` = sold out, `seat-color-disable` = lapsed. These match the on-page legend.
- Choosing a show opens `/seatlayout/<token>` without signing in. It shows film, language, date, time, cinema, seats by row and number, categories and prices. **The link opens directly in a fresh browser**, so it is a true booking deep link.
- Some venues show a notice with Close and Accept before the seat map (a food-payment notice at INOX Nehru Place and INOX EF3). Jasola and PVR Chanakyapuri did not.
- robots.txt does not disallow `/moviesessions` or `/seatlayout` (it names one specific old seat URL).

**Positive control.** The same code was run on *Ramayana*, Hindi, 23 Nov, 2 PM to 6 PM. It found the 3:25 PM ScreenX show at Jasola, opened its seat page and read 146 of 171 seats free. So the detector can say yes at this venue and format.

**Terms caveat.** PVR's Terms of Use allow personal, non-commercial copies of selected content and prohibit "systematic retrieval of data" to build "a collection, compilation, database or directory". This project stores only shows that match your one target, not listings. That is a reading of the terms, not legal advice. If PVR objects, set `platforms.pvr.enabled: false`.

**Assumed**

- The ScreenX show will appear under film id 35312. Ramayana's formats all share one id, which supports this.
- The seat token stays stable. It is not relied on: screenings are keyed by verified attributes.
- ~~A data-centre server will be served like a home connection.~~ **Disproved 24 Nov:** from GitHub Actions (US data centre) PVR and District both answered HTTP 403. An Indian VPS was not tried; a home connection works.
- All dates fit in the tab row. Four were shown.

**Not done on purpose.** The site's own API is not called, no seat is ever selected, notices are never accepted, and a 403 or block page is never worked around.

**Seat holds.** Not investigated, since finding out means holding real seats. No hold duration is assumed anywhere.

## BookMyShow

**Verified**

- One plain request to `robots.txt` with curl got a Cloudflare "you have been blocked" page.
- 23 Nov: the film page in an undisguised headless Chromium (the same setup PVR serves) got HTTP 403 and the same block page. Getting past it would mean disguising the browser, which this project does not do.
- The terms say scrapers are not allowed "without our express written permission".
- In an ordinary browser the film page is `/movies/national-capital-region-ncr/avengers-doomsday/ET00439706`. Formats have their own ids (for example ET00526306 for one Infinity Vision variant). "Book tickets" opens a language and format picker; English ScreenX was not in it.

**Not verified.** Venue name for Jasola, showtime data and seat data. Going further would have meant more automated browsing of a site that prohibits it.

**Alternative provided.** A direct link in the dashboard, and optional reminders at times you choose (`reminder_times_ist`). Also press "I'm interested" on the film page while signed in; whether that sends an alert for a new format is not verified.

The watcher reports BookMyShow as not checked. It never reports it as having no tickets.

## District

**Verified**

- Pages embed structured data (`__NEXT_DATA__`). Plain HTTP with an honest user agent works.
- The Jasola page (CD1018963) carries, per show: session id, start time with offset, language, `scrnFmt` (`3D SCREEN X`), auditorium, seat counts by category and prices. It also lists every date the cinema has open.
- The film is MV176735.
- robots.txt disallows every URL with a query string. Other dates use `?fromdate=` or District's own API.
- The terms require respecting robots exclusions and include "scrapes or crawls" in a list of prohibited uses that ends "or otherwise commercialize". Whether that reaches personal monitoring is arguable, so it is left to you.

**Film-wide signal (added 23 Nov).** The film page lists the formats on offer for the film in the city as filters (`SCREEN_FORMAT`). On 23 Nov: MS Infinity Vision, 3D, 4DX-3D, ICE-3D. A new entry such as SCREEN X is logged as an event. It is a hint, not an alert: it says nothing about the target cinema or date.

**What the cinema page gives** (on since 23 Nov). The page allowed by robots.txt shows one default date plus the list of open dates. Until that default is 19 December the watcher can only tell that the date has opened, which it reports as UNVERIFIED and never alerts on. So District adds an early signal on the dashboard, not a second independent detector.

**Assumed.** How a sold-out District show looks. The adapter needs `avail > 0` and `disableClick = false` to call a show bookable; anything else is not.

## Access that was not used

Private APIs, proxies, CAPTCHA solving, header or fingerprint disguise, signed-in sessions. No official public API was found for any of the three.
