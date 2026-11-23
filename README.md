# Doomsday Watcher

Watches for one screening and alerts your phone when it can be booked:

> Avengers: Doomsday, English, ScreenX, INOX Pacific Mall, Jasola, Saturday 19 December 2026, 2 PM to 6 PM IST

It checks PVR INOX about once a minute and District every two minutes, confirms a PVR show on its seat page, and alerts every channel you set up (push, email, WhatsApp, Telegram) with the booking link. You choose seats and pay. It never selects seats, accepts notices or touches payment.

**Coverage:** PVR INOX and District are automated. District can only fully confirm a show on the day itself (robots.txt); before that it gives early signals. BookMyShow blocks automated browsers and prohibits them, so you get a link and optional reminders. Details in [docs/PLATFORM_RESEARCH.md](docs/PLATFORM_RESEARCH.md).

**Hosting:** run it on a PC on a home connection (`scripts/start-windows.ps1`). PVR and District refuse data-centre networks: from GitHub's servers both answered HTTP 403, so cloud hosting is not used. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Start

1. Set up alerts: [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md)
2. Put it on GitHub and Render: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
3. Press **Test notification** on the dashboard

Behaviour is in `config.yaml`, secrets in `.env`.

## Test

```
pip install -r requirements-dev.txt
playwright install chromium
python -m pytest
```

About three minutes. Tests use saved pages and local servers, never the live sites.

## More

[Architecture](docs/ARCHITECTURE.md), [Troubleshooting](docs/TROUBLESHOOTING.md), [Final report](docs/FINAL_REPORT.md)
