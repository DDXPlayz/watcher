# Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| "Configuration is not valid" at startup | A setting or secret is missing or wrong | The message names the field |
| Overall: Monitoring with problems | A platform is failing, or no notifier is set up, or delivery is failing | Read the platform cards and the Notifications tab |
| Overall: Nothing can be checked | No automated platform has succeeded recently | Check PVR yourself until it recovers |
| PVR: ACCESS_DENIED | PVR refused this network | It retries slowly. If it persists, move to another network. Do not disguise it |
| PVR: RENDER_ERROR | Page did not finish loading | Usually passes. If constant, the site may have changed |
| PVR: PARSE_ERROR | Page structure or film URL changed | Open the film on pvrcinemas.com. If its address changed, update `platforms.pvr.movie_url`. Otherwise the parser needs work |
| PVR: possible match, not verified | A show looks right but the seat page could not confirm it | **Open PVR now and look.** The Timeline says which field was unconfirmed |
| "Executable doesn't exist" | Browser not installed | `playwright install chromium` |
| Notification pending | All providers failed; it keeps retrying | The Notifications tab shows the error. HTTP 401, 403 or 404 means a wrong token, chat or topic |
| Test arrives, but nothing on the lock screen | Focus mode or app notification settings | See NOTIFICATIONS.md |
| Sign-in forgotten | | Change `DASHBOARD_PASSWORD` in `.env` and restart. Everyone is signed out |
| "Unknown host" (421) | No password is set and the address is not localhost | Use 127.0.0.1 or set a password |
| "Cross-site request refused" | A proxy changed the Host header | Configure it to pass Host through |

## Prove the detector still works

Pick any film in ScreenX at Jasola and run the same code on it:

```
python -m scripts.live_control control https://www.pvrcinemas.com/moviesessions/<film>/<id> "FILM NAME AS SHOWN" Hindi 2026-11-30
```

It should print an AVAILABLE line if that film has a show between 2 PM and 6 PM that day. Run it once in a while, not in a loop.

## Logs

`logs/watcher.log`, one JSON object per line. Identical errors are shown once per 15 minutes with a count. Secrets are redacted.
