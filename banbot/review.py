"""Review queue: persisted cases a human moderator approves (ban) or denies.

Discord-specific rendering/buttons live in discord_gateway.py behind the ReviewPoster protocol,
so this module is fully testable without Discord.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Protocol

from .enforcement import DECISION_MOD_APPROVED, Banner, BanOutcome, BanRequest
from .flags import FlagResult
from .gateway import Gateway, MemberNotFound
from .store import ReviewRow, Store
from .util import Clock

log = logging.getLogger(__name__)

# Why a case was queued (stored in review_queue.reason).
REASON_STATUS = "status"  # "status:Flagged", "status:Mixed", ... provider said review
REASON_CONFIRMED_REQUIRES_REVIEW = "confirmed_requires_review"
REASON_NICKNAME_CHANGED = "nickname_changed"
REASON_MEMBER_LEFT = "member_left_before_ban"
REASON_INCONCLUSIVE_EXHAUSTED = "inconclusive_exhausted"
REASON_BAN_EVASION = "ban_evasion"  # this Roblox account was already banned here under a different Discord account


@dataclass(frozen=True)
class ReviewCase:
    discord_id: int
    roblox_id: int | None
    roblox_username: str
    nickname: str | None
    flag: FlagResult
    reason: str
    identity_source: str = "nickname"  # how we linked this member to the account: bloxlink | nickname


@dataclass(frozen=True)
class Decision:
    ok: bool
    message: str
    ban_outcome: BanOutcome | None = None


class ReviewPoster(Protocol):
    async def post(self, row: ReviewRow) -> tuple[int, int] | None:
        """Post the case to the mod channel with Approve/Deny buttons. Returns (channel_id, message_id)."""
        ...

    async def post_report(self, row: ReviewRow) -> tuple[int, int] | None:
        """Report-only mode: post a plain detection notice (no buttons). Returns (channel_id, message_id)."""
        ...

    async def update(self, row: ReviewRow, resolution: str) -> None:
        """Mark the posted message resolved (disable buttons, append outcome)."""
        ...

    async def log_detection(self, row: ReviewRow) -> None:
        """Optional archival log (e.g. a forum thread per detection). A no-op if not configured."""
        ...


class ReviewQueue:
    def __init__(
        self,
        *,
        guild_id: int,
        store: Store,
        poster: ReviewPoster,
        banner: Banner,
        gateway: Gateway,
        clock: Clock,
        mod_role_id: int,
        report_only: bool = False,
    ):
        self._guild_id = guild_id
        self._store = store
        self._poster = poster
        self._banner = banner
        self._gateway = gateway
        self._clock = clock
        self._mod_role_id = mod_role_id
        self.report_only = report_only

    # ---------------------------------------------------------------- report-only mode
    async def report(self, case: ReviewCase) -> tuple[ReviewRow, bool]:
        """Post a detection notice for mods. No buttons, no ban. Deduped per member + Roblox id + status."""
        row, created = self._store.record_report(
            self._guild_id,
            discord_id=case.discord_id,
            roblox_id=case.roblox_id,
            roblox_username=case.roblox_username,
            provider=case.flag.provider,
            outcome=case.flag.outcome.value,
            status_name=case.flag.status_name,
            reason=case.reason,
            nickname=case.nickname,
            raw_response_json=case.flag.raw_json(),
            summary=case.flag.detail,
            identity_source=case.identity_source,
            at=self._clock.now(),
        )
        if not created:
            log.info("report #%s already posted for discord=%s roblox=%s status=%s (seen %d times); not re-posting",
                     row.id, case.discord_id, case.roblox_id, case.flag.status_name, row.seen_count)
            return row, False
        log.warning("report #%s: discord=%s roblox=%s (%s) status=%s reason=%s",
                    row.id, case.discord_id, case.roblox_id, case.roblox_username, case.flag.status_name, case.reason)
        await self._post_report(row)
        await self._log(row)
        return row, True

    async def _post_report(self, row: ReviewRow) -> None:
        try:
            ids = await self._poster.post_report(row)
        except Exception:
            log.exception("failed to post report #%s to the mod channel; will retry on next startup", row.id)
            return
        if ids:
            self._store.set_review_message(row.id, ids[0], ids[1])

    def reports(self, limit: int = 50) -> list[ReviewRow]:
        return self._store.reported(self._guild_id, limit)

    # ---------------------------------------------------------------- enqueue
    async def enqueue(self, case: ReviewCase) -> tuple[ReviewRow, bool]:
        row, created = self._store.enqueue_review(
            self._guild_id,
            discord_id=case.discord_id,
            roblox_id=case.roblox_id,
            roblox_username=case.roblox_username,
            provider=case.flag.provider,
            outcome=case.flag.outcome.value,
            status_name=case.flag.status_name,
            reason=case.reason,
            nickname=case.nickname,
            raw_response_json=case.flag.raw_json(),
            summary=case.flag.detail,
            identity_source=case.identity_source,
            at=self._clock.now(),
        )
        if not created:
            log.info("review #%s already pending for discord=%s roblox=%s; not duplicating",
                     row.id, case.discord_id, case.roblox_id)
            return row, False
        log.warning("review #%s queued: discord=%s roblox=%s (%s) status=%s reason=%s",
                    row.id, case.discord_id, case.roblox_id, case.roblox_username, case.flag.status_name, case.reason)
        await self._post(row)
        await self._log(row)
        return row, True

    async def _post(self, row: ReviewRow) -> None:
        try:
            ids = await self._poster.post(row)
        except Exception:
            log.exception("failed to post review #%s to the mod channel; will retry on next startup", row.id)
            return
        if ids:
            self._store.set_review_message(row.id, ids[0], ids[1])

    async def _log(self, row: ReviewRow) -> None:
        try:
            await self._poster.log_detection(row)
        except Exception:
            log.exception("failed to log detection #%s to the forum", row.id)

    async def repost_unposted(self) -> int:
        """On startup: post any pending/reported rows that never made it to Discord."""
        n = 0
        for row in self._store.pending_reviews(self._guild_id):
            if row.message_id is None:
                await self._post(row)
                n += 1
        for row in self._store.reported(self._guild_id, limit=1000):
            if row.message_id is None:
                await self._post_report(row)
                n += 1
        return n

    def pending(self) -> list[ReviewRow]:
        return self._store.pending_reviews(self._guild_id)

    # ---------------------------------------------------------------- decisions
    def can_moderate(self, role_ids: Iterable[int]) -> bool:
        return self._mod_role_id in set(role_ids)

    async def approve(self, review_id: int, *, actor_id: int, actor_role_ids: Iterable[int]) -> Decision:
        if not self.can_moderate(actor_role_ids):
            log.warning("review #%s: approve attempt by non-mod %s rejected", review_id, actor_id)
            return Decision(False, "You don't have permission to do that.")
        if self.report_only:
            log.warning("review #%s: approve by %s refused - bot is in REPORT_ONLY mode", review_id, actor_id)
            return Decision(False, "Report-only mode is on. No ban was issued.")
        row = self._store.get_review(self._guild_id, review_id)
        if row is None:
            return Decision(False, "Case not found.")
        now = self._clock.now()
        if not self._store.resolve_review(review_id, status="approved", by=actor_id, at=now):
            return Decision(False, f"Case #{review_id} has already been resolved.")

        try:
            nickname = await self._gateway.fetch_nickname(row.discord_id)
        except MemberNotFound:
            nickname = None  # they left; a ban still works on the user id
        except Exception as e:
            log.warning("review #%s: could not fetch live nickname (%s)", review_id, e)
            nickname = None

        result = await self._banner.ban(BanRequest(
            discord_id=row.discord_id,
            roblox_id=row.roblox_id,
            roblox_username=row.roblox_username,
            nickname_at_ban=nickname,
            provider=row.provider,
            status_name=row.status_name,
            raw_response_json=row.raw_response_json,
            decision_path=DECISION_MOD_APPROVED,
            approved_by=actor_id,
        ))
        reply, outcome = {
            BanOutcome.BANNED: (f"Case #{review_id} resolved. Member banned.",
                                f"Banned · <@{actor_id}>"),
            BanOutcome.WOULD_BAN: (f"Case #{review_id} resolved. Dry run: no ban issued.",
                                   f"Approved · <@{actor_id}> · dry run, no ban issued"),
            BanOutcome.FAILED: (f"Case #{review_id} approved, but the ban failed: {result.error}",
                                f"Approved · <@{actor_id}> · ban failed: {result.error}"),
            BanOutcome.ALREADY_BANNED: (f"Case #{review_id} resolved. Member was already banned.",
                                        f"Approved · <@{actor_id}> · already banned"),
        }[result.outcome]
        note = f"approved by {actor_id}: {result.outcome.value}"
        self._store.set_review_note(review_id, note)
        log.warning("review #%s %s (mod %s)", review_id, note, actor_id)
        await self._safe_update(row, outcome)
        return Decision(True, reply, result.outcome)

    async def deny(self, review_id: int, *, actor_id: int, actor_role_ids: Iterable[int]) -> Decision:
        if not self.can_moderate(actor_role_ids):
            log.warning("review #%s: deny attempt by non-mod %s rejected", review_id, actor_id)
            return Decision(False, "You don't have permission to do that.")
        row = self._store.get_review(self._guild_id, review_id)
        if row is None:
            return Decision(False, "Case not found.")
        now = self._clock.now()
        if not self._store.resolve_review(review_id, status="denied", by=actor_id, at=now, note=f"denied by {actor_id}"):
            return Decision(False, f"Case #{review_id} has already been resolved.")
        log.warning("review #%s DENIED by mod %s at %s (discord=%s roblox=%s)",
                    review_id, actor_id, now.isoformat(), row.discord_id, row.roblox_id)
        await self._safe_update(row, f"Dismissed · <@{actor_id}>")
        return Decision(True, f"Case #{review_id} resolved. No action taken.")

    async def _safe_update(self, row: ReviewRow, resolution: str) -> None:
        try:
            await self._poster.update(row, resolution)
        except Exception:
            log.exception("failed to update review message for #%s", row.id)
