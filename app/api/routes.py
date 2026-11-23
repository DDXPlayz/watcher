"""HTTP API. Everything except /health and the sign-in page needs a session when a password is set."""

from __future__ import annotations

import csv
import io
import json
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.schemas import LoginBody, TargetBody
from app.api.security import COOKIE, SESSION_SECONDS
from app.scheduler import CheckRefused
from app.service import Watcher
from app.utils.time import fmt_ist

router = APIRouter()


def watcher(request: Request) -> Watcher:
    return request.app.state.watcher


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    try:
        data = await watcher(request).health()
    except Exception:  # noqa: BLE001 - a health probe must answer, and must not answer "fine"
        return JSONResponse({"status": "down"}, status_code=503)
    # Public, so only the verdict.
    return JSONResponse({"status": data["status"]}, status_code=503 if data["status"] == "down" else 200)


@router.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
    client = request.client.host if request.client else "?"
    if not request.app.state.limiter.allow(f"login:{client}", 5):
        raise HTTPException(429, "Too many attempts. Wait a minute.")
    auth = request.app.state.auth
    if not auth.check_password(body.password):
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(
        COOKIE,
        auth.issue(),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.app.state.cfg.cookie_secure,
    )
    return {"ok": True}


@router.post("/api/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/status")
@router.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    return await watcher(request).status()


def _event(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.type,
        "platform": row.platform,
        "screening_id": row.screening_id,
        "at": row.at,
        "at_ist": fmt_ist(row.at),
        "message": row.message,
        "details": row.details,
    }


@router.get("/api/events")
async def events(request: Request, limit: int = 60, before_id: int | None = None) -> list[dict[str, Any]]:
    return [_event(e) for e in await watcher(request).repo.events(max(1, min(limit, 500)), before_id)]


@router.get("/api/checks")
async def checks(request: Request, limit: int = 60) -> list[dict[str, Any]]:
    rows = await watcher(request).repo.recent_checks(max(1, min(limit, 500)))
    return [
        {"platform": c.platform, "at": c.at, "duration_ms": c.duration_ms, "state": c.state, "ok": c.ok, "error_kind": c.error_kind}
        for c in rows
    ]


@router.get("/api/notifications")
async def notifications(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "id": n.id,
            "kind": n.kind,
            "title": n.title,
            "status": n.status,
            "provider": n.provider,
            "attempts": n.attempts,
            "last_error": n.last_error,
            "created_at": n.created_at,
            "delivered_at": n.delivered_at,
        }
        for n in await watcher(request).repo.notifications()
    ]


@router.get("/api/logs")
async def logs(request: Request, lines: int = 200) -> PlainTextResponse:
    path = request.app.state.cfg.log_dir / "watcher.log"
    if not path.exists():
        return PlainTextResponse("")
    with path.open(encoding="utf-8", errors="replace") as handle:
        return PlainTextResponse("".join(deque(handle, maxlen=max(1, min(lines, 1000)))))


@router.get("/api/export")
async def export(request: Request, fmt: str = "json") -> Response:
    rows = [_event(e) for e in await watcher(request).repo.events(100_000)]
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "time_ist", "time_utc", "type", "platform", "message", "details"])
        for r in rows:
            cells = [r["id"], r["at_ist"], r["at"].isoformat(), r["type"], r["platform"] or "", r["message"], json.dumps(r["details"], ensure_ascii=False)]
            # A leading = + - @ would be run as a formula by spreadsheet software.
            writer.writerow([f"'{c}" if isinstance(c, str) and c[:1] in "=+-@" and c else c for c in cells])
        return Response(
            "﻿" + out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=doomsday-events.csv"},
        )
    if fmt != "json":
        raise HTTPException(400, "fmt must be json or csv")
    return Response(
        json.dumps(rows, ensure_ascii=False, default=str, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=doomsday-events.json"},
    )


@router.post("/api/monitoring/{action}")
async def monitoring(action: str, request: Request) -> dict[str, bool]:
    if action not in ("start", "resume", "pause"):
        raise HTTPException(404)
    try:
        await watcher(request).set_running(action != "pause")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"running": watcher(request).supervisor.running.is_set()}


@router.post("/api/check")
async def check_now(request: Request) -> dict[str, Any]:
    service = watcher(request)
    if not service.supervisor.running.is_set():
        raise HTTPException(409, "Monitoring is paused.")
    started: list[str] = []
    refused: dict[str, str] = {}
    for name, worker in service.workers.items():
        try:
            worker.request_check_now()
            started.append(name)
        except CheckRefused as exc:
            refused[name] = str(exc)
    return {"started": started, "refused": refused}


@router.post("/api/test-notification")
async def test_notification(request: Request) -> dict[str, bool]:
    service = watcher(request)
    if not service.dispatcher.providers:
        raise HTTPException(409, "No notification provider is configured. See docs/NOTIFICATIONS.md.")
    if not request.app.state.limiter.allow("test-notification", 3):
        raise HTTPException(429, "Three tests a minute is the limit.")
    await service.test_notification()
    return {"queued": True}


@router.put("/api/target")
async def update_target(body: TargetBody, request: Request) -> dict[str, Any]:
    try:
        target = await watcher(request).update_target(body.model_dump(exclude={"confirm"}))
    except ValueError as exc:
        raise HTTPException(422, "That target is not valid: the start time must not be after the end time.") from exc
    return target.model_dump(mode="json")
