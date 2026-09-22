"""Per-UTC-day request budget for APIs with a daily quota (Bloxlink: 2,000 requests/day).

The count lives in SQLite so a restart cannot reset it. Every attempted request is counted before it is
sent, on the assumption that the provider counts failures too.

Two callers, two rules:
- the sweep may spend down to the reserve and no further, so a big sweep cannot starve join checks;
- join checks and pre-ban re-checks may spend the reserve as well.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .store import Store
from .util import Clock

log = logging.getLogger(__name__)


def utc_day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def next_utc_midnight(now: datetime) -> datetime:
    d = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


class DailyBudget:
    def __init__(self, *, guild_id: int, store: Store, clock: Clock, api: str, limit: int, reserve: int = 0):
        if limit < 0 or reserve < 0 or reserve > limit:
            raise ValueError("budget limit/reserve out of range")
        self._guild_id = guild_id
        self._store = store
        self._clock = clock
        self.api = api
        self.limit = limit
        self.reserve = reserve

    # ---------------------------------------------------------------- reads
    def used(self) -> int:
        return self._store.usage_count(self._guild_id, self.api, utc_day(self._clock.now()))

    def remaining(self) -> int:
        return max(self.limit - self.used(), 0)

    def can_spend(self, n: int = 1, *, use_reserve: bool) -> bool:
        floor = 0 if use_reserve else self.reserve
        return self.remaining() - n >= floor

    def resets_at(self) -> datetime:
        return next_utc_midnight(self._clock.now())

    def seconds_until_reset(self) -> float:
        # Small buffer so we do not wake a hair before the provider's own clock rolls over.
        return max((self.resets_at() - self._clock.now()).total_seconds(), 0.0) + 60.0

    # ---------------------------------------------------------------- writes
    def spend(self, n: int = 1) -> int:
        """Record n requests against today. Returns the new total."""
        total = self._store.usage_increment(self._guild_id, self.api, utc_day(self._clock.now()), n)
        if total == self.limit or (total > self.limit - self.reserve and (total - n) <= self.limit - self.reserve):
            log.warning("%s daily budget: %d/%d used (reserve %d)", self.api, total, self.limit, self.reserve)
        return total

    def describe(self) -> str:
        return f"{self.used()}/{self.limit} today, resets {self.resets_at():%H:%M} UTC"
