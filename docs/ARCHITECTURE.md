# Architecture

One Python process: a web server, one worker per platform, a notifier and a maintenance loop, over one SQLite database.

```
PVR worker ──────┐
BookMyShow worker ┼─> Engine (matcher, transitions, dedup) ─> database ─> notifier ─> ntfy / Pushover / Telegram
District worker ─┘                                              │
                                            dashboard + API <───┘
Supervisor restarts workers.  Maintenance checks health and sends the heartbeat.
```

## Data flow

1. A worker calls `check_availability(target)`. The adapter returns a `PlatformResult` holding `Observation`s: what the site showed, with `None` for anything it did not.
2. The engine runs the matcher on each observation. Eight fields each get PASS, FAIL or UNKNOWN. One FAIL: not our show. One UNKNOWN: UNVERIFIED. All PASS: AVAILABLE.
3. In one transaction the engine updates the screening, writes events and queues notifications. If the transaction fails nothing is kept, and the next check finds the show again.
4. The notifier wakes at once and sends to every channel in parallel. Each channel's result is stored; a failed channel is retried alone. A short lease on each row stops two processes sharing a database from sending it twice.

**Two runners.** `python -m app.main --once` checks every platform once, delivers, and exits; GitHub Actions runs it every 5 minutes with its database carried in the Actions cache. It keeps its own history, so it can alert independently of Render. Its alerts say "Sent by: github watcher".

## Modules

| Path | Job |
|---|---|
| `app/monitor/matcher.py` | Matching rules. Pure. |
| `app/monitor/engine.py` | State transitions, events, queueing |
| `app/monitor/deduplication.py` | Screening, entity and notification keys |
| `app/scheduler.py` | Worker loop, backoff, breaker, supervisor |
| `app/platforms/pvr.py`, `pvr_parser.py` | Browser steps, and pure parsing |
| `app/platforms/district.py`, `bookmyshow.py` | Gated adapter; no-request adapter |
| `app/notifications/` | Providers and the durable queue |
| `app/service.py` | Wiring, health, status |
| `app/api/`, `app/main.py` | HTTP API, security, startup |

## Decisions that matter

**Two-step proof at PVR.** A green showtime is not enough. The adapter opens the show's seat page and requires film, cinema, language, date and time to agree with the listing, and at least one free seat. Re-checked every 5 minutes or when the colour changes. If a venue notice blocks the seat page, a green show whose booking flow opens to that notice counts as bookable, with the seat count unknown.

**Absence is hard to prove.** An empty, changing or unrecognised page is PLATFORM_ERROR. A show must be missing from two clean checks before it counts as gone.

**States.** NOT_YET_LISTED: date or venue not listed. UNAVAILABLE: venue listed, no matching show. SOLD_OUT is used only for a show previously seen on sale; one already sold out at first sight is UNAVAILABLE with an event.

**Duplicates.** Each screening has a generation that increases every time it becomes available. A notification's unique key is screening, transition and generation, enforced by the database.

**Same show on two platforms.** Two rows with their own links share an `entity_key`. The second platform's alert is titled "also on".

**ScreenX 2D or 3D.** Both count as ScreenX, and the alert says which. ScreenX combined with another premium format is UNVERIFIED.

## Scheduling

- PVR 60 s, District 120 s. Jitter only lengthens a wait. 30 s is the hard minimum. An unverified PVR match is re-checked at 30 s.
- Failure: exponential backoff to 15 minutes. 403 and 429 start at 2 minutes, and `Retry-After` is honoured.
- Breaker: opens after 5 failures, probes after 5 minutes, never permanent.
- Each check has a 90 s limit, then the browser is reset. The browser is replaced every 100 checks.

## Health

`/health` is 503 when no automated platform has succeeded recently, the database fails or the maintenance loop stops. `degraded` means something is wrong but at least one platform is being checked, or no notifier is set up. Platforms that are off by policy never count as healthy.

A thread outside the event loop ends the process if the loop hangs for 5 minutes, so Docker restarts it. The heartbeat is withheld when checks or notifications are failing, so an outside service can tell you.

## Database

`platforms`, `screenings`, `seat_snapshots`, `events`, `notifications`, `checks`, `settings`, `schema_version`. UTC in storage, IST on display. Only portable column types, so PostgreSQL works by changing `DATABASE_URL` and installing a driver (not tested). Only matching screenings are stored. Checks are kept 7 days, seat snapshots 30, events 120.

## Security

Secrets come from the environment and never reach the API, logs or page. A password is required unless bound to loopback. Session cookie: HttpOnly, SameSite=Strict, signed. Origin check on writes, host check against DNS rebinding, login and API limits, strict CSP, all site text rendered as text. No docs or admin endpoints.

## Checkout assistance

Not implemented. Setting either flag to true is a startup error. The watcher stops at reading the public seat map.
