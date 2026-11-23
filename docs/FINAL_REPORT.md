# Final report

## Check, 24 November

Live accuracy check on films showing that day at Jasola, through the production adapters and matcher:

- District read 37 shows of 14 films. With ScreenX controls as the target (Ramayana, Hindi, 3:25 PM; The Hunger Games: Sunrise on the Reaping, English, 9:15 AM), each matched exactly its one show; all 22 other shows of those films were rejected for format or time.
- PVR found the Ramayana 3:25 PM ScreenX show at Jasola, opened its seat page and read 65 of 171 free, the same count District reported.
- Full pipeline (`--once`, ntfy): both platforms detected the show, two alerts reached ntfy.sh (the second titled "also on PVR INOX"), and a second run sent nothing.
- Fixed: in one-shot mode an alert waited for slower platforms to finish (7.5 s in the live run). It now goes out as soon as its own platform's check is stored (0.7 s). Test added. 270 tests pass.

## Update, 23 November

Added at your request: email, WhatsApp (CallMeBot and Meta's Cloud API), every alert to every channel, District switched on, a one-shot mode, a GitHub Actions backup every 5 minutes, and a Render blueprint. 269 tests pass.

Verified: email delivery against a local mail server (real SMTP); one-shot mode live against PVR and District (about 8 s a run, no repeat on the second run); District's film-format signal on the live page.

Not verified: WhatsApp and Gmail with real accounts (needs your credentials); the GitHub and Render runs (needs your accounts); whether PVR and District answer servers in the US and Singapore; whether the watcher fits Render Starter's 512 MB. The GitHub `test` workflow builds the Docker image and prints its memory use on every push.

Not possible: BookMyShow. On 23 Nov an undisguised headless browser got HTTP 403 and Cloudflare's block page. Getting past it would mean disguising the browser.

Vercel cannot run this: once-a-day schedules on its free plan and no long-running process.

## Original report, 22 November

22 November 2026. Not yet production-ready: the Docker image has never been built, and it has not run on a server.

## Where the ticket stands

Not on sale as of 22 November, 7:57 PM IST. PVR lists the film for 17 to 20 December at 25 Delhi-NCR cinemas on the 19th. INOX Pacific Mall, Jasola is not one of them, and no ScreenX show appears.

## Status

**Fully implemented and tested**

| Part | Evidence |
|---|---|
| Matching | Every required case. 15 deliberate breakages of critical rules across the app were all caught by tests |
| PVR detection | Live on the target (12 of 12 checks in 12.6 minutes) and live on a positive control: Ramayana, Hindi, ScreenX at Jasola, 23 Nov 3:25 PM |
| Seat reading | Live: 146 of 171 free, with rows, categories and prices |
| ntfy | Real deliveries, read back from the server |
| Deduplication and restart | Live: killed the process after an alert and restarted it; no second ticket alert |
| Dashboard | Every control used against the running app; phone layout checked |
| Scheduling, isolation, recovery, API security | Automated tests |

**Implemented, not live-verified**

- Telegram and Pushover: payloads tested against their documented formats; no account to send through.
- The real release: the Doomsday ScreenX show does not exist yet, so its test page is the captured Ramayana ScreenX page with film and language changed.
- PVR sold-out and failure responses: simulated, apart from the sold-out colour, which appears on captured pages.
- Venue notice before the seat map: seen live at two other INOX cinemas; handling tested locally.
- Docker files. PostgreSQL. macOS and Linux (developed on Windows with Python 3.12).

**Partly implemented**

- District: complete against a captured page, off until you opt in, and limited by robots.txt to a date signal.

**Not possible under platform rules**

- BookMyShow monitoring.

**Deliberately absent:** checkout assistance, seat selection, accepting notices, payment.

## Measured

| | |
|---|---|
| PVR check interval | 60 s configured; 60 to 74 s observed with jitter |
| PVR check duration | 4.2 s average, 7.2 s slowest (cold start) |
| Check with seat verification | 8 to 9 s |
| Detection to push accepted by ntfy | 0.8 s (one live sample) |
| Expected worst case, listing to push | About 85 s when checks are succeeding |
| Automated tests | 240 passed, 2 min 16 s |
| Soak | 12.6 minutes live, plus 2,000 simulated checks with flat memory. No multi-day run |

ntfy accepting a push is not the same as it appearing on your iPhone. That last step needs your phone.

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Matching rules pass | Met |
| 2 | Wrong screenings never alert | Met in tests |
| 3 | Failures distinguished from unavailability | Met |
| 4 | Duplicates suppressed | Met, also live |
| 5 | Database survives restarts | Met, also live |
| 6 | Workers independent | Met |
| 7 | At least one provider works | Met: ntfy |
| 8 | Dashboard reflects backend | Met |
| 9 | Docker deployment works | **Not met: Docker was not available here** |
| 10 | Documentation states limitations | Met |

## Problems live testing found, now fixed

1. PVR labels near dates "Today" and "Tomorrow". The first version would have stopped seeing the target on 18 and 19 December.
2. Some INOX venues put a notice before the seat map. The first version would have left a real release unverified, with no alert.
3. Returning to the listing after verifying added several seconds before the alert.

## Remaining risks

1. **PVR may refuse a data-centre server.** Test first (DEPLOYMENT.md, step 5). A computer at home is the fallback.
2. **One platform.** If PVR is down at release, nothing else is watching. Keep the heartbeat on and check by hand when alerted.
3. **PVR may change its pages.** That shows as an error, not as "no tickets".
4. **A different film id for ScreenX.** Not expected, but it would be missed. Look at PVR's film list now and then.
5. **A login wall on seat pages under load** would leave a match unverified, with no alert. The dashboard would show it.
6. **PVR's terms** are open to interpretation. See PLATFORM_RESEARCH.md.
