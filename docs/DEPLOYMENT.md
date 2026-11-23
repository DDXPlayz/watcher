# Deployment

> **Update, 24 November: run it at home.** From GitHub's servers (a US data centre) both PVR and District answered HTTP 403, so the GitHub backup cannot check them and its schedule is switched off. Render is also a data centre and would very likely be refused the same way; it was not tried. The watcher works from an Indian home connection. Use **Run on Windows, free** below. Nothing in this project tries to get past a block.
>
> The sections on GitHub and Render are kept for reference.

Original recommended setup: **Render** runs the watcher all the time, with the dashboard. **GitHub Actions** runs a backup check every 5 minutes. Each sends alerts on its own, so either can fail without you missing the release.

| | Render (main) | GitHub Actions (backup) |
|---|---|---|
| Checks | Every 60 s (PVR), 120 s (District) | Every 5 min when GitHub keeps to schedule; late or skipped under load |
| Cost | About $7.25 a month (Starter plan + 1 GB disk) | Free in a public repository |
| Dashboard | Yes | No; results in the Actions log |
| Location | Singapore | United States |

**Vercel cannot run this.** Its free plan runs scheduled jobs at most once a day, and its functions are short-lived and cannot keep a browser. **Render's free plan cannot either**: it sleeps after 15 minutes without visitors and loses its files on every restart, so checks stop and alerts repeat.

**Not yet proven:** that PVR and District answer servers in Singapore and the United States (tested only from India), and that the Docker image works (Docker was not available here; the GitHub test workflow builds and runs it on every push). Check both on the first day.

## 1. Put the code on GitHub

1. Create a repository on github.com. Public gives unlimited Actions minutes; secrets stay secret either way. Private gives 2,000 free minutes a month, enough only for a check about every 30 minutes (change the cron in `.github/workflows/watch.yml`).
2. From the project folder:

```
git init
git add .
git commit -m "Doomsday watcher"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

3. Open the **Actions** tab. The `test` workflow should pass, including the `docker` job; its last step prints memory use.

## 2. GitHub backup checks

1. Repository → Settings → Secrets and variables → Actions → New repository secret. Add the same channel secrets as in `.env.example` (`NOTIFY_PROVIDERS`, `PUSHOVER_...`, `SMTP_...`, `CALLMEBOT_...` and so on). Optional: `GITHUB_HEARTBEAT_URL`.
2. Actions → `watch` → **Run workflow** once by hand. Open the run: "Check once" logs a line per platform. `ACCESS_DENIED` for PVR means PVR refuses GitHub's network; the backup is then District-only.
3. After that it runs by itself. A run where every platform fails is marked failed, and GitHub emails you about failed scheduled runs.

GitHub turns off scheduled workflows in public repositories after 60 days without commits. That is after 19 December, but push something if you keep it longer.

## 3. Render main watcher

1. Sign in to render.com with GitHub. **New → Blueprint**, choose the repository. It reads `render.yaml`: Docker, Singapore, Starter plan, 1 GB disk at `/srv/data`.
2. Fill in the variables it asks for: `DASHBOARD_PASSWORD` (12+ characters), `NOTIFY_PROVIDERS` and the channel secrets. More can be added later under Environment.
3. Deploy. Within about 3 minutes the service shows healthy. Open its `onrender.com` address, sign in, press **Test notification**.
4. Check the PVR card: it should show "Healthy" with dates listed. `ACCESS_DENIED` means PVR refuses Render's network.
5. Check memory under Metrics. If it nears 512 MB, change the plan to Standard (2 GB).

Every push to `main` redeploys. History survives on the disk.

## Alternatives

- **A VPS in India** (for example an AWS Lightsail or DigitalOcean server in Mumbai or Bangalore, about $5 to $6 a month): closest to what was tested. Use `docker compose up -d --build`; see the compose section below.
- **A computer at home** that does not sleep: `python -m app.main`.

## Docker Compose on a VPS

```
cp .env.example .env      # fill it in, chmod 600 .env
docker compose up -d --build
```

The dashboard listens on the server only. Reach it with `ssh -N -L 8080:127.0.0.1:8080 user@server`, or put Caddy in front with HTTPS and set `COOKIE_SECURE=true`.

Backup:

```
docker compose exec watcher python -c "import sqlite3; sqlite3.connect('data/watcher.db').backup(sqlite3.connect('data/backup.db'))"
docker compose cp watcher:/srv/data/backup.db ./backup.db
```

## Run on Windows, free (no card)

The main watcher on your own PC, with GitHub as the backup. Everything it needs lives in `%USERPROFILE%\.doomsday-watcher`, outside OneDrive and git: `venv\`, `browsers\`, `data\`, `logs\` and `watcher.env` (your secrets, same names as `.env.example`).

Start it (it restarts itself if it exits):

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-windows.ps1
```

Start it automatically at sign-in (run once):

```
schtasks /Create /TN "Doomsday Watcher" /SC ONLOGON /RL LIMITED /TR "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%CD%\scripts\start-windows.ps1\""
```

Keep the PC from sleeping while plugged in: Settings → System → Power → Screen and sleep → "When plugged in, put my device to sleep after" → Never.

From your phone: install Tailscale on the PC and the phone (free), set `HOST=0.0.0.0` in `watcher.env`, restart, and open `http://<pc-name>:8080` on the phone. The dashboard password is in `watcher.env`.

## Run locally

Windows:

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
copy .env.example .env
.venv\Scripts\python -m app.main
```

macOS or Linux:

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env
.venv/bin/python -m app.main
```

Then open http://127.0.0.1:8080. `python -m app.main --once` runs a single check, as GitHub does.
