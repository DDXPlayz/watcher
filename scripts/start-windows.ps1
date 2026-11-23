# Runs the watcher on this PC, restarting it if it ever exits.
# Runtime, database and secrets live in %USERPROFILE%\.doomsday-watcher (outside OneDrive and git):
#   venv\       Python environment          browsers\   Playwright's Chromium
#   data\       database                    logs\       log files
#   watcher.env secrets and settings, KEY=value per line (same names as .env.example)
$ErrorActionPreference = "Stop"
$home_dir = Join-Path $env:USERPROFILE ".doomsday-watcher"
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $home_dir "venv\Scripts\python.exe"
$envFile = Join-Path $home_dir "watcher.env"

if (-not (Test-Path $python)) { throw "No Python environment at $python. See docs/DEPLOYMENT.md, 'Run on Windows'." }
if (-not (Test-Path $envFile)) { throw "No settings file at $envFile. Copy .env.example there as watcher.env and fill it in." }

foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim().Trim('"').Trim("'"), "Process")
    }
}
$env:DATA_DIR = Join-Path $home_dir "data"
$env:LOG_DIR = Join-Path $home_dir "logs"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $home_dir "browsers"

Set-Location $project
while ($true) {
    & $python -m app.main
    Start-Sleep -Seconds 15   # it exited (crash or hang watchdog): start again
}
