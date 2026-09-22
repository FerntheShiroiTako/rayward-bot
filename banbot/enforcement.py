"""The ONLY place that calls the Discord ban API.

Guarantees:
- every ban attempt (real or dry-run) writes an audit row first;
- dry-run never touches the ban API;
- a failed ban is recorded and the member is NOT marked banned;
- already-banned members are never banned twice (idempotent re-runs).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from . import bandm
from .gateway import BanError, Gateway
from .store import Store
from .util import Clock

log = logging.getLogger(__name__)

DECISION_AUTO_CONFIRMED = "auto_confirmed"
DECISION_MOD_APPROVED = "mod_approved"


class BanOutcome(str, Enum):
    BANNED = "banned"
    WOULD_BAN = "would_ban"  # dry-run
    FAILED = "ban_failed"
    ALREADY_BANNED = "already_banned"


@dataclass(frozen=True)
class BanRequest:
    discord_id: int
    roblox_id: int | None
    roblox_username: str | None
    nickname_at_ban: str | None
    provider: str
    status_name: str
    raw_response_json: str
    decision_path: str
    approved_by: int | None = None


@dataclass(frozen=True)
class BanResult:
    outcome: BanOutcome
    audit_id: int | None
    error: str | None = None


class Banner:
    def __init__(
        self, *, guild_id: int, store: Store, gateway: Gateway, clock: Clock, dry_run: bool, ban_delay_s: float = 0.0,
        dm_template: str | None = None,
    ):
        self._guild_id = guild_id
        self._store = store
        self._gateway = gateway
        self._clock = clock
        self.dry_run = dry_run
        self._ban_delay_s = ban_delay_s  # gentle pacing between real bans during a sweep
        self._dm_template = dm_template  # None = do not message banned members

    async def ban(self, req: BanRequest) -> BanResult:
        if self._store.is_banned(self._guild_id, req.discord_id):
            log.info("ban skipped: discord %s already banned (idempotent)", req.discord_id)
            return BanResult(BanOutcome.ALREADY_BANNED, None)

        now = self._clock.now()
        audit_id = self._store.write_audit(
            self._guild_id,
            discord_id=req.discord_id,
            roblox_id=req.roblox_id,
            roblox_username=req.roblox_username,
            nickname_at_ban=req.nickname_at_ban,
            provider=req.provider,
            status_name=req.status_name,
            raw_response_json=req.raw_response_json,
            decision_path=req.decision_path,
            approved_by=req.approved_by,
            dry_run=self.dry_run,
            at=now,
        )

        if self.dry_run:
            log.warning(
                "DRY RUN - would ban discord=%s roblox=%s (%s) nickname=%r path=%s approved_by=%s audit=%s",
                req.discord_id, req.roblox_id, req.roblox_username, req.nickname_at_ban,
                req.decision_path, req.approved_by, audit_id,
            )
            return BanResult(BanOutcome.WOULD_BAN, audit_id)

        # The DM has to go out BEFORE the ban: once banned, the member no longer shares a server with the
        # bot and Discord will refuse to deliver it. A failed DM never blocks the ban.
        await self._notify(req, audit_id)

        source = {"rotector": "Rotector", "banbot": "banbot ban-evasion check"}.get(req.provider, req.provider)
        reason = f"[banbot] {source}: {req.status_name} | roblox {req.roblox_username} ({req.roblox_id}) | {req.decision_path}"
        if req.approved_by:
            reason += f" by {req.approved_by}"
        try:
            await self._gateway.ban(req.discord_id, reason=reason[:512])
        except BanError as e:
            log.error("BAN FAILED discord=%s: %s (audit=%s)", req.discord_id, e, audit_id)
            self._store.set_audit_result(audit_id, succeeded=False, error=str(e))
            return BanResult(BanOutcome.FAILED, audit_id, str(e))
        except Exception as e:  # anything unexpected is still a failure, never a silent success
            log.exception("BAN FAILED (unexpected) discord=%s (audit=%s)", req.discord_id, audit_id)
            self._store.set_audit_result(audit_id, succeeded=False, error=f"{type(e).__name__}: {e}")
            return BanResult(BanOutcome.FAILED, audit_id, str(e))

        self._store.set_audit_result(audit_id, succeeded=True, error=None)
        self._store.mark_banned(self._guild_id, req.discord_id, req.roblox_id, audit_id, self._clock.now())
        if self._ban_delay_s > 0:
            await self._clock.sleep(self._ban_delay_s)
        log.warning(
            "BANNED discord=%s roblox=%s (%s) path=%s approved_by=%s audit=%s",
            req.discord_id, req.roblox_id, req.roblox_username, req.decision_path, req.approved_by, audit_id,
        )
        return BanResult(BanOutcome.BANNED, audit_id)

    async def _notify(self, req: BanRequest, audit_id: int) -> None:
        if not self._dm_template:
            return
        text = bandm.render(
            self._dm_template,
            server=self._gateway.guild_name(),
            roblox_username=req.roblox_username,
            roblox_id=req.roblox_id,
            status=req.status_name,
        )
        try:
            sent = await self._gateway.send_dm(req.discord_id, text)
            self._store.set_audit_dm(audit_id, sent=sent, error=None if sent else "not delivered")
        except Exception as e:  # never let a DM problem stop the ban
            log.warning("ban DM to %s raised: %s", req.discord_id, e)
            self._store.set_audit_dm(audit_id, sent=False, error=f"{type(e).__name__}: {e}")
