"""Process-owned Shazam policy; resolved events are the seam for the hosted ledger."""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from threading import RLock

FAILURES = frozenset({"http_429", "http_503", "http_5xx", "malformed", "timeout_post"})


def shazam_off() -> bool:
    return os.environ.get("IDEA_ENGINE_SHAZAM", "").strip().casefold() == "off"


class ShazamBlocked(RuntimeError):
    """An admission refusal, never a resolved provider attempt."""


@dataclass(frozen=True)
class BreakerConfig:
    failure_rate_e4: int = 3_000
    window_seconds: int = 300
    cooldown_seconds: int = 1_800
    minimum_sample: int = 20
    shazam_daily_budget_per_egress: int = 2_000
    latch_count: int = 3
    reenable_generation: int = 0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            minimum = 0 if name in {"failure_rate_e4", "reenable_generation"} else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"shazam_breaker.{name} must be an integer >= {minimum}")
        if self.failure_rate_e4 > 10_000:
            raise ValueError("shazam_breaker.failure_rate_e4 must be <= 10000")


class ShazamBreaker:
    def __init__(
        self, config: BreakerConfig | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.config = config or BreakerConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._samples: deque[tuple[datetime, bool]] = deque()
        self._day = self.clock().astimezone(UTC).date()
        self._daily_attempts = 0
        self._opens = 0
        self._until: datetime | None = None
        self._latched = False

    def configure(self, config: BreakerConfig) -> None:
        with self._lock:
            if config.reenable_generation > self.config.reenable_generation:
                self.reenable()
            self.config = config

    def reenable(self) -> None:
        """Explicit operator action; preserve the daily request budget and manual hard off."""
        with self._lock:
            self._latched = False
            self._opens = 0
            self._until = None
            self._samples.clear()

    def _advance(self) -> datetime:
        now = self.clock().astimezone(UTC)
        if now.date() != self._day:
            self._day = now.date()
            self._daily_attempts = 0
            self._opens = 0
        cutoff = now - timedelta(seconds=self.config.window_seconds)
        while self._samples and self._samples[0][0] <= cutoff:
            self._samples.popleft()
        return now

    def reason(self) -> str | None:
        with self._lock:
            now = self._advance()
            if self._latched:
                return "shazam_breaker:c_latch"
            if self._daily_attempts >= self.config.shazam_daily_budget_per_egress:
                return "shazam_breaker:b_daily_budget"
            if self._until is not None and now < self._until:
                return "shazam_breaker:a_failure_rate"
            return None

    def release_dispatch(self, dispatch_day: date) -> None:
        with self._lock:
            self._advance()
            if dispatch_day == self._day:
                self._daily_attempts -= 1

    def dispatch(self, *, running_free: bool) -> date:
        if shazam_off():
            raise ShazamBlocked("shazam_manual_off")
        with self._lock:
            reason = self.reason()
            if reason and not running_free:
                raise ShazamBlocked(reason)
            self._daily_attempts += 1
            return self._day

    def resolved(self, outcome: str) -> None:
        if shazam_off():
            return
        with self._lock:
            now = self._advance()
            self._samples.append((now, outcome in FAILURES))
            # Rule (a) keeps judging every resolved attempt while rule (b) blocks new work: an
            # admitted Free primary runs on past the daily budget, and its qualifying failures
            # must still trip and latch (D8) instead of expiring silently at 00:00 UTC.  Budget
            # exhaustion is never itself a rate trip — only sampled qualifying failures are.
            # An open cooldown is not re-tripped; the next period is judged after it lapses.
            if self._latched or (self._until is not None and now < self._until):
                return
            count = len(self._samples)
            failures = sum(failed for _, failed in self._samples)
            if (
                count >= self.config.minimum_sample
                and failures * 10_000 > count * self.config.failure_rate_e4
            ):
                self._opens += 1
                self._until = now + timedelta(seconds=self.config.cooldown_seconds)
                self._latched = self._opens >= self.config.latch_count
