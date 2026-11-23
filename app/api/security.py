"""Session cookie, request limits and response headers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.requests import Request

COOKIE = "dw_session"
SESSION_SECONDS = 14 * 86400
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def load_secret(configured: str, data_dir: Path) -> bytes:
    if configured:
        return configured.encode()
    path = data_dir / "session.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="ascii")
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip().encode()


class Auth:
    def __init__(self, password: str, secret: bytes) -> None:
        self._password = password
        # Binding the signature to the password signs everyone out when it changes.
        self._key = hashlib.sha256(secret + password.encode()).digest()

    @property
    def required(self) -> bool:
        return bool(self._password)

    def check_password(self, candidate: str) -> bool:
        return self.required and hmac.compare_digest(candidate.encode(), self._password.encode())

    def issue(self) -> str:
        expires = str(int(time.time()) + SESSION_SECONDS)
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None) -> bool:
        if not self.required:
            return True
        expires, _, signature = (token or "").partition(".")
        if not expires.isdigit() or not hmac.compare_digest(signature.encode(), self._sign(expires).encode()):
            return False
        return int(expires) > time.time()

    def _sign(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        if len(self._hits) > 2000:
            self._hits.clear()
        hits = self._hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def same_origin(request: Request) -> bool:
    """State-changing requests must come from our own page (defence in depth beside SameSite=Strict)."""
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site") in (None, "same-origin", "none")
    return origin.split("://", 1)[-1] == request.headers.get("host")


def host_allowed(request: Request, loopback_only: bool) -> bool:
    """Without a password the dashboard answers only to loopback names, which defeats DNS rebinding."""
    if not loopback_only:
        return True
    host = request.headers.get("host", "")
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return name in LOOPBACK_HOSTS
