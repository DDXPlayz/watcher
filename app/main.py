"""Entry point: `python -m app.main`"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.api.routes import router
from app.api.security import COOKIE, HEADERS, Auth, RateLimiter, host_allowed, load_secret, same_origin
from app.config import ROOT, AppConfig, load_config
from app.service import Watcher
from app.utils.logging import setup_logging

DASHBOARD = ROOT / "dashboard"
OPEN_PATHS = {"/health", "/login", "/api/login", "/assets/styles.css", "/assets/login.js"}


def create_app(cfg: AppConfig, service: Watcher | None = None) -> FastAPI:
    service = service or Watcher(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        yield
        await service.stop()

    app = FastAPI(title="Doomsday Watcher", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.watcher = service
    app.state.limiter = RateLimiter()
    app.state.auth = Auth(cfg.secrets.dashboard_password, load_secret(cfg.secrets.session_secret, cfg.data_dir))

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await _gate(request, call_next)
        for name, value in HEADERS.items():
            response.headers[name] = value
        return response

    async def _gate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        client = request.client.host if request.client else "?"
        if not app.state.auth.required and not host_allowed(request, loopback_only=True):
            return JSONResponse({"detail": "Unknown host."}, status_code=421)
        if path != "/health" and not app.state.limiter.allow(f"api:{client}", 240):
            return JSONResponse({"detail": "Too many requests."}, status_code=429)
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        if path not in OPEN_PATHS and not app.state.auth.valid(request.cookies.get(COOKIE)):
            if path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "Sign in first."}, status_code=401)
        return await call_next(request)

    app.include_router(router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(DASHBOARD / "index.html")

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(DASHBOARD / "login.html")

    app.mount("/assets", StaticFiles(directory=DASHBOARD), name="assets")
    return app


async def run_once(cfg: AppConfig) -> int:
    service = Watcher(cfg)
    await service.start(background=False)
    try:
        return await service.run_once()
    finally:
        await service.stop()


def start_hang_watchdog(service: Watcher, limit: float = 300.0) -> None:
    """Exit the process if the event loop stops turning, so Docker or systemd can restart it.

    Runs in its own thread because nothing inside a blocked event loop can report that it is blocked.
    """

    def watch() -> None:
        while True:
            time.sleep(15)
            if time.monotonic() - service.supervisor.last_tick > limit:
                print("event loop made no progress; exiting so it can be restarted", file=sys.stderr, flush=True)
                os._exit(70)

    threading.Thread(target=watch, name="hang-watchdog", daemon=True).start()


def main() -> None:
    once = "--once" in sys.argv[1:]
    try:
        cfg = load_config()
    except (ValidationError, ValueError, OSError) as exc:
        # Pydantic's own text echoes input values, which may be secrets.
        if isinstance(exc, ValidationError):
            problems = "\n".join(f"  {'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}" for e in exc.errors(include_input=False))
        else:
            problems = f"  {exc}"
        print(f"Configuration is not valid:\n{problems}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / ".write-test").write_text("ok", encoding="ascii")
        (cfg.data_dir / ".write-test").unlink()
    except OSError:
        print(f"Cannot write to {cfg.data_dir}. On a mounted disk, give the container user (uid 10001) write access.", file=sys.stderr)
        raise SystemExit(2) from None
    setup_logging(cfg.log_dir)
    if once:
        raise SystemExit(asyncio.run(run_once(cfg)))
    service = Watcher(cfg)
    start_hang_watchdog(service)
    uvicorn.run(create_app(cfg, service), host=cfg.host, port=cfg.port, log_config=None, access_log=False, timeout_graceful_shutdown=20)


if __name__ == "__main__":
    main()
