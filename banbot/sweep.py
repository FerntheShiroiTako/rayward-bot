"""Monthly sweep: chunked, rate-limited, resumable, single-flight.

Progress model
- Members are processed in ascending Discord-ID order; `sweeps.cursor_member_id` is advanced after each chunk.
- Every member gets a row in `sweep_results`; members with a terminal row are skipped on resume, so a crash
  mid-chunk cannot double-process anyone (and the ban/queue layers are idempotent anyway).
- Phase 'running' = main pass. Phase 'retrying' = waiting for this sweep's inconclusive members to resolve
  or exhaust their retries. Then the summary is posted and the sweep is 'finished'.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter

from .config import Config
from .gateway import Gateway
from .pipeline import Bucket, Pipeline
from .retrier import InconclusiveRetrier
from .store import Store, SweepAlreadyRunning, SweepRow
from .util import Clock, chunked

log = logging.getLogger(__name__)


class SweepRunner:
    def __init__(
        self,
        *,
        cfg: Config,
        store: Store,
        gateway: Gateway,
        pipeline: Pipeline,
        retrier: InconclusiveRetrier,
        clock: Clock,
    ):
        self._cfg = cfg
        self._store = store
        self._gateway = gateway
        self._pipeline = pipeline
        self._retrier = retrier
        self._clock = clock
        self._lock = asyncio.Lock()  # in-process single-flight; the DB index is the cross-restart guard

    # ------------------------------------------------------------------ public API
    def active(self) -> SweepRow | None:
        return self._store.get_active_sweep(self._cfg.guild_id)

    def start(self, *, started_by: int | None) -> SweepRow:
        """Create the sweep record. Raises SweepAlreadyRunning."""
        if self._lock.locked():
            raise SweepAlreadyRunning("a sweep is running in this process")
        return self._store.create_sweep(
            self._cfg.guild_id, started_at=self._clock.now(), started_by=started_by, dry_run=self._cfg.dry_run)

    async def run(self, sweep: SweepRow) -> SweepRow:
        """Run (or resume) a sweep to completion and post the summary. Safe to call again after a crash."""
        if self._lock.locked():
            raise SweepAlreadyRunning("a sweep is running in this process")
        async with self._lock:
            try:
                if sweep.status == "running":
                    await self._main_phase(sweep)
                    self._store.set_sweep_status(sweep.id, "retrying")
                await self._retry_phase(sweep.id)
                counts = self._store.sweep_counts(sweep.id)
                self._store.finish_sweep(sweep.id, finished_at=self._clock.now(), status="finished", counts=dict(counts))
                final = self._store.get_sweep(self._cfg.guild_id, sweep.id)
                await self._post_summary(final)
                return final
            except asyncio.CancelledError:
                log.warning("sweep #%s cancelled; it stays active and will resume on restart", sweep.id)
                raise
            except Exception as e:
                # Leave the sweep active so it can be resumed; tell the mods.
                log.exception("sweep #%s crashed; it stays active and can be resumed", sweep.id)
                self._store.set_sweep_status(
                    sweep.id, self._store.get_sweep(self._cfg.guild_id, sweep.id).status, error=f"{type(e).__name__}: {e}")
                await self._safe_send(f"Sweep #{sweep.id} stopped: `{type(e).__name__}: {e}`. "
                                      f"It will resume on restart or with /sweep resume.")
                raise

    async def resume_if_active(self) -> SweepRow | None:
        sweep = self._store.get_active_sweep(self._cfg.guild_id)
        if sweep is None:
            return None
        log.warning("resuming sweep #%s (status=%s, cursor=%s)", sweep.id, sweep.status, sweep.cursor_member_id)
        return await self.run(sweep)

    def abort(self, sweep_id: int, *, by: int | None) -> None:
        self._store.set_sweep_status(sweep_id, "failed", error=f"aborted by {by}")

    # ------------------------------------------------------------------ phases
    async def _main_phase(self, sweep: SweepRow) -> None:
        members = sorted(await self._gateway.list_members(), key=lambda m: m.id)
        self._store.set_sweep_total(sweep.id, len(members))
        done = self._store.terminal_member_ids(sweep.id)
        todo = [m for m in members if m.id > sweep.cursor_member_id and m.id not in done]
        log.warning("sweep #%s main phase: %d members total, %d to process (cursor=%s)",
                    sweep.id, len(members), len(todo), sweep.cursor_member_id)
        await self._announce_budget_plan(sweep, len(todo))

        for chunk in chunked(todo, self._cfg.sweep_batch_size):
            await self._wait_for_bloxlink_budget(sweep, len(chunk))
            counts = await self._pipeline.process(chunk, sweep_id=sweep.id)
            self._store.set_sweep_cursor(sweep.id, chunk[-1].id)
            log.info("sweep #%s chunk done (up to %s): %s", sweep.id, chunk[-1].id, dict(counts))
            if self._cfg.rate_limit.sweep_chunk_delay_s > 0:
                await self._clock.sleep(self._cfg.rate_limit.sweep_chunk_delay_s)

    # ------------------------------------------------------------------ Bloxlink daily budget
    async def _announce_budget_plan(self, sweep: SweepRow, todo: int) -> None:
        budget = self._pipeline.bloxlink_budget
        if budget is None or todo == 0:
            return
        spendable_today = max(budget.remaining() - budget.reserve, 0)
        if todo <= spendable_today:
            return
        per_day = max(budget.limit - budget.reserve, 1)
        days = 1 + -(-(todo - spendable_today) // per_day)  # ceil
        msg = (f"Sweep #{sweep.id}: {todo} members to check, {spendable_today} Bloxlink lookups available today. "
               f"Expected to finish in about {days} day{'s' if days != 1 else ''}.")
        log.warning(msg)
        await self._safe_send(msg)

    async def _wait_for_bloxlink_budget(self, sweep: SweepRow, needed: int) -> None:
        """Sleep through the UTC reset if the next chunk cannot be paid for. Cheaper than failing every
        member into the retry bucket, and it keeps the sweep's counts honest."""
        budget = self._pipeline.bloxlink_budget
        if budget is None:
            return
        announced = False
        while not budget.can_spend(needed, use_reserve=False):
            wait = budget.seconds_until_reset()
            if not announced:
                msg = f"Sweep #{sweep.id} paused: Bloxlink daily limit reached. Resumes at {budget.resets_at():%H:%M} UTC."
                log.warning(msg)
                await self._safe_send(msg)
                announced = True
            self._store.set_sweep_status(sweep.id, "running", error=f"paused for Bloxlink quota until {budget.resets_at():%Y-%m-%d %H:%M} UTC")
            await self._clock.sleep(wait)
        if announced:
            log.warning("sweep #%s resuming: Bloxlink budget %s", sweep.id, budget.describe())

    async def _retry_phase(self, sweep_id: int) -> None:
        while True:
            rows = self._store.active_inconclusive(self._cfg.guild_id, sweep_id=sweep_id)
            if not rows:
                return
            now = self._clock.now()
            due = self._retrier.due(rows, now)
            if due:
                await self._retrier.retry_rows(due)
                continue
            next_at = min(r.next_retry_at for r in rows if r.next_retry_at is not None)
            wait = max((next_at - now).total_seconds(), 0.0)
            log.info("sweep #%s: %d inconclusive members pending; next retry in %.0fs", sweep_id, len(rows), wait)
            await self._clock.sleep(wait)

    # ------------------------------------------------------------------ summary
    def format_summary(self, sweep: SweepRow) -> str:
        c = Counter(sweep.counts)
        dur = ""
        if sweep.finished_at:
            secs = int((sweep.finished_at - sweep.started_at).total_seconds())
            dur = f", {secs // 60}m{secs % 60:02d}s"
        lines = [
            f"**Sweep #{sweep.id} complete** · {sweep.total_members or 0} members{dur}",
            f"Clear: **{c[Bucket.CLEAR.value]}**",
            f"No linked account: **{c[Bucket.UNRESOLVED.value]}**",
            f"Past offender: **{c[Bucket.PAST_OFFENDER.value]}**",
        ]
        if self._cfg.report_only:
            lines.append(f"Reported: **{c[Bucket.REPORTED.value]}**")
        else:
            lines.append(f"Sent to review: **{c[Bucket.REVIEW.value]}**")
            lines.append(
                f"Would ban (dry run): **{c[Bucket.WOULD_BAN.value]}**"
                if sweep.dry_run
                else f"Banned: **{c[Bucket.BANNED.value]}**"
            )
        lines.append(f"Unverified: **{c[Bucket.INCONCLUSIVE.value]}**")
        if c[Bucket.BAN_FAILED.value]:
            lines.append(f"Ban failed: **{c[Bucket.BAN_FAILED.value]}**")
        if c[Bucket.ALREADY_BANNED.value]:
            lines.append(f"Already banned: **{c[Bucket.ALREADY_BANNED.value]}**")
        lines.append("-# Rotector via Rayward")
        return "\n".join(lines)

    async def _post_summary(self, sweep: SweepRow) -> None:
        text = self.format_summary(sweep)
        log.warning("%s", text.replace("**", ""))
        await self._safe_send(text)
        if self._cfg.notify_starter_on_sweep_complete and sweep.started_by:
            try:
                await self._gateway.send_dm(sweep.started_by, text)
            except Exception:
                log.info("could not DM sweep #%s summary to starter %s", sweep.id, sweep.started_by)

    async def _safe_send(self, text: str) -> None:
        try:
            await self._gateway.send_text(self._cfg.effective_summary_channel_id, text)
        except Exception:
            log.exception("could not post to the summary channel")
