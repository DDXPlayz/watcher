"""Backoff and circuit breaker. Pure logic with an injectable clock so it can be tested without sleeping."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field


def backoff_delay(attempt: int, base: float, cap: float, jitter: float = 0.2) -> float:
    """Exponential backoff for attempt 1, 2, 3... with +/- jitter; never above cap * (1 + jitter)."""
    raw = min(cap, base * (2 ** min(max(attempt, 1) - 1, 16)))
    return raw * random.uniform(1 - jitter, 1 + jitter)


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive failures; lets one probe through after `cooldown`.

    It never stays open permanently: each cooldown ends in a half-open probe.
    """

    threshold: int = 5
    cooldown: float = 300.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = field(default=None)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allows(self) -> bool:
        return self.state != "open"

    def seconds_until_probe(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (self.clock() - self.opened_at))

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()
