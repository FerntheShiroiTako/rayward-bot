"""Live checks against a guild's configured keys and Discord permissions: what /setup's "Test & Finish"
button and /diagnose report. Pure logic here so it is testable without Discord; setup_ui.py renders it.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord

from .bloxlink import BloxlinkClient, BloxlinkFailure
from .config import Config
from .flags import FlagProvider

ROBLOX_TEST_USER_ID = 1  # the "Roblox" system account - always exists, safe to look up
DISCORD_TEST_USER_ID = 1  # not a real linkable account; any non-auth answer proves the key works


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


async def check_rayward(provider: FlagProvider) -> CheckResult:
    try:
        results = await provider.lookup([ROBLOX_TEST_USER_ID])
    except Exception as e:
        return CheckResult("Rayward API key", False, f"{type(e).__name__}: {e}")
    r = results.get(ROBLOX_TEST_USER_ID)
    if r is None:
        return CheckResult("Rayward API key", False, "no response for the test lookup")
    if r.outcome.value != "inconclusive":
        return CheckResult("Rayward API key", True, "connected")
    low = r.detail.lower()
    if "401" in low or "403" in low or "auth" in low:
        return CheckResult("Rayward API key", False, r.detail)
    return CheckResult("Rayward API key", False, f"test lookup failed: {r.detail}")


async def check_bloxlink(bloxlink: BloxlinkClient | None) -> CheckResult | None:
    if bloxlink is None:
        return None
    try:
        result = await bloxlink.lookup(DISCORD_TEST_USER_ID)
    except Exception as e:
        return CheckResult("Bloxlink API key", False, f"{type(e).__name__}: {e}")
    if isinstance(result, BloxlinkFailure):
        low = result.detail.lower()
        if "auth" in low or "401" in low or "403" in low:
            return CheckResult("Bloxlink API key", False, result.detail)
        if result.kind == "unparsable":
            return CheckResult("Bloxlink API key", False, f"unexpected response: {result.detail}")
    return CheckResult("Bloxlink API key", True, "connected")


def check_discord_permissions(guild: discord.Guild, cfg: Config) -> list[CheckResult]:
    out: list[CheckResult] = []
    me = guild.me
    if me is None:
        return [CheckResult("Bot permissions", False, "bot member not cached yet; try again in a moment")]

    perms = me.guild_permissions
    out.append(CheckResult(
        "Ban Members permission", perms.ban_members,
        "" if perms.ban_members else "grant Ban Members to the bot's role",
    ))

    mod_role = guild.get_role(cfg.mod_role_id)
    if mod_role is None:
        out.append(CheckResult("Mod role", False, "role not found (deleted since /setup?)"))
    elif me.top_role <= mod_role:
        out.append(CheckResult(
            "Role position", False, "the bot's own role must be positioned above the mod role to ban its members",
        ))
    else:
        out.append(CheckResult("Role position", True))

    out.append(_check_channel(guild, me, cfg.mod_channel_id, "Mod channel"))
    if cfg.summary_channel_id and cfg.summary_channel_id != cfg.mod_channel_id:
        out.append(_check_channel(guild, me, cfg.summary_channel_id, "Summary channel"))
    if cfg.log_forum_channel_id:
        out.append(_check_channel(guild, me, cfg.log_forum_channel_id, "Detection log forum"))
    return out


def _check_channel(guild: discord.Guild, me: discord.Member, channel_id: int, label: str) -> CheckResult:
    ch = guild.get_channel(channel_id)
    if ch is None:
        return CheckResult(label, False, "channel not found (deleted, or the bot cannot see it)")
    perms = ch.permissions_for(me)
    ok = perms.view_channel and perms.send_messages and perms.embed_links
    if ok:
        return CheckResult(label, True)
    missing = [n for n, has in (
        ("View Channel", perms.view_channel), ("Send Messages", perms.send_messages), ("Embed Links", perms.embed_links)
    ) if not has]
    return CheckResult(label, False, f"bot needs: {', '.join(missing)}")
