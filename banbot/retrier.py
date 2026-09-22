"""Re-runs the pipeline for members in the inconclusive bucket whose backoff has elapsed."""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime

from .gateway import Gateway, MemberInfo, MemberNotFound
from .pipeline import Pipeline
from .store import InconclusiveRow, Store
from .util import Clock

log = logging.getLogger(__name__)


class InconclusiveRetrier:
    def __init__(self, *, guild_id: int, store: Store, gateway: Gateway, pipeline: Pipeline, clock: Clock):
        self._guild_id = guild_id
        self._store = store
        self._gateway = gateway
        self._pipeline = pipeline
        self._clock = clock

    @staticmethod
    def due(rows: list[InconclusiveRow], now: datetime) -> list[InconclusiveRow]:
        return [r for r in rows if r.next_retry_at is not None and r.next_retry_at <= now]

    async def retry_rows(self, rows: list[InconclusiveRow]) -> Counter[str]:
        """Re-fetch each member's live nickname and push them through the pipeline again, batched per sweep."""
        total: Counter[str] = Counter()
        by_sweep: dict[int | None, list[MemberInfo]] = defaultdict(list)
        for row in rows:
            try:
                nickname = await self._gateway.fetch_nickname(row.discord_id)
            except MemberNotFound:
                log.info("inconclusive retry: discord=%s left the server; dropping", row.discord_id)
                self._store.clear_inconclusive(self._guild_id, row.discord_id)
                if row.sweep_id is not None:
                    self._store.record_sweep_result(
                        row.sweep_id, row.discord_id, "unresolved", roblox_username=row.roblox_username,
                        roblox_id=row.roblox_id, detail="member left during retries", at=self._clock.now(),
                    )
                total["left"] += 1
                continue
            except Exception as e:
                # Discord itself failed: count it as another failed attempt, never as clean.
                log.warning("inconclusive retry: could not fetch discord=%s (%s)", row.discord_id, e)
                await self._pipeline.mark_inconclusive(
                    MemberInfo(row.discord_id, None), row.sweep_id, stage="recheck",
                    username=row.roblox_username, roblox_id=row.roblox_id, detail=f"discord fetch failed: {e}",
                )
                total["inconclusive"] += 1
                continue
            by_sweep[row.sweep_id].append(MemberInfo(row.discord_id, nickname))

        for sweep_id, members in by_sweep.items():
            log.info("retrying %d inconclusive members (sweep=%s)", len(members), sweep_id)
            total += await self._pipeline.process(members, sweep_id=sweep_id)
        return total

    async def run_due(self, *, sweep_id: int | None = None, unassigned_only: bool = False) -> Counter[str]:
        rows = self._store.active_inconclusive(self._guild_id, sweep_id=sweep_id, unassigned_only=unassigned_only)
        due = self.due(rows, self._clock.now())
        if not due:
            return Counter()
        return await self.retry_rows(due)
