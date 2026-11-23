"""Structured JSON logs with secret redaction and suppression of identical repeated errors."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Any

_BOT_TOKEN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_KEYED = re.compile(r"((?:token|password|secret|user_key|authorization)[\"'=:\s]+)[^\s\"',&}]+", re.IGNORECASE)


def redact(text: str) -> str:
    return _KEYED.sub(r"\1[REDACTED]", _BOT_TOKEN.sub("bot[REDACTED]", text))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload.update({k: redact(v) if isinstance(v, str) else v for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class RepeatFilter(logging.Filter):
    """Let an identical WARNING/ERROR through once per window and count what was hidden."""

    def __init__(self, window_seconds: float = 900.0) -> None:
        super().__init__()
        self.window = window_seconds
        self.seen: dict[tuple[str, str], tuple[float, int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        key = (record.name, record.getMessage()[:300])
        now = time.monotonic()
        last, hidden = self.seen.get(key, (float("-inf"), 0))
        if now - last < self.window:
            self.seen[key] = (last, hidden + 1)
            return False
        if len(self.seen) > 500:
            self.seen.clear()
        self.seen[key] = (now, 0)
        if hidden:
            record.data = {**(getattr(record, "data", None) or {}), "suppressed_repeats": hidden}
        return True


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(log_dir / "watcher.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
    ]
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RepeatFilter())
        root.addHandler(handler)
    # httpx logs full URLs at INFO, and a Telegram URL contains the bot token.
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **data: Any) -> None:
    logger.log(level, msg, extra={"data": data})
