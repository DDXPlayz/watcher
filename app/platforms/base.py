"""Contract every booking platform implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain import ErrorKind, HealthStatus, PlatformResult, State, Target

USER_AGENT = "DoomsdayWatcher/1.0 (personal ticket availability monitor)"

_CHALLENGE_MARKERS = (
    "access denied",
    "attention required",
    "verify you are human",
    "you have been blocked",
    "captcha",
    "unusual traffic",
)


class PlatformFailure(Exception):
    """Raised inside an adapter; always converted to a PLATFORM_ERROR result."""

    def __init__(self, kind: ErrorKind, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


def failure_for_status(status: int, retry_after_header: str | None = None) -> PlatformFailure | None:
    if status < 400:
        return None
    retry_after = float(retry_after_header) if retry_after_header and retry_after_header.isdigit() else None
    if status in (401, 403):
        return PlatformFailure(ErrorKind.ACCESS_DENIED, f"HTTP {status}", retry_after)
    if status == 429:
        return PlatformFailure(ErrorKind.RATE_LIMITED, "HTTP 429", retry_after or 300.0)
    if status >= 500:
        return PlatformFailure(ErrorKind.SERVER_ERROR, f"HTTP {status}", retry_after)
    return PlatformFailure(ErrorKind.PARSE_ERROR, f"unexpected HTTP {status}", retry_after)


def looks_like_challenge(text: str) -> bool:
    """Recognise a block page so we can stop and back off. Nothing here tries to get past one."""
    lowered = text[:4000].casefold()
    return len(text) < 6000 and any(marker in lowered for marker in _CHALLENGE_MARKERS)


class BookingPlatform(ABC):
    name: str

    @abstractmethod
    async def check_availability(self, target: Target) -> PlatformResult: ...

    @abstractmethod
    async def health_check(self) -> HealthStatus: ...

    async def reset(self) -> None:
        """Drop any state that may be wedged. Called after a timeout or crash."""

    async def close(self) -> None:
        """Release resources."""

    def error(self, kind: ErrorKind, detail: str, retry_after: float | None = None) -> PlatformResult:
        return PlatformResult(
            platform=self.name, state=State.PLATFORM_ERROR, error_kind=kind, error_detail=detail[:400], retry_after=retry_after
        )
