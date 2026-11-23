"""BookMyShow: deliberately makes no requests.

Its terms prohibit scrapers without written permission, and its Cloudflare protection answered this
project's one plain, honestly identified request with a block page (22 Nov 2026). Getting past that would
mean evading a protection measure, so the platform is always reported as not checkable, never as having
no tickets. The permitted alternative is scheduled reminders with a direct link; see
config.yaml: platforms.bookmyshow.reminder_times_ist.
"""

from __future__ import annotations

from app.config import BmsConfig
from app.domain import ErrorKind, HealthStatus, PlatformResult, Target
from app.platforms.base import BookingPlatform

NOTE = "BookMyShow prohibits automated access and blocks it. Not checked; use the direct link."


class BookMyShowPlatform(BookingPlatform):
    name = "bookmyshow"

    def __init__(self, cfg: BmsConfig) -> None:
        self.cfg = cfg

    async def check_availability(self, target: Target) -> PlatformResult:
        return self.error(ErrorKind.ACCESS_NOT_PERMITTED, NOTE)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(platform=self.name, operational=False, detail=NOTE)
