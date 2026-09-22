"""Per-guild settings: what /setup and /config read, write and validate.

`GuildSettings` is the row stored in SQLite (store.py's guild_settings table), with API keys already
decrypted for use in this process. `build_guild_config` turns one of these, plus the process-wide
GlobalConfig, into the `Config` object the pipeline actually runs on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import Config, GlobalConfig

DEFAULT_BAN_DM_TEXT = (
    "You have been removed from {server}.\n\n"
    "Our records show your linked Roblox account ({roblox_username}) is flagged as {status} by Rotector. "
    "If you believe this is a mistake, you may appeal by contacting the server's moderators."
)


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    rayward_api_key: str | None = None
    bloxlink_api_key: str | None = None
    mod_role_id: int | None = None
    mod_channel_id: int | None = None
    summary_channel_id: int | None = None
    log_forum_channel_id: int | None = None  # optional: every detection also logged as its own forum thread
    master_role_id: int | None = None  # full /config access, same as Manage Server - set by an admin
    configurator_role_id: int | None = None  # /config access to everything except the two API keys
    sweep_trigger_user_ids: frozenset[int] = frozenset()
    sweep_trigger_role_id: int | None = None
    report_only: bool = False
    dry_run: bool = True
    confirmed_requires_review: bool = True
    ban_dm_enabled: bool = True
    ban_dm_text: str | None = None  # None = use the bot-wide default template (ban_dm.txt or the built-in one)
    notify_starter_on_sweep_complete: bool = True
    setup_completed: bool = False
    setup_by: int | None = None
    setup_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: int | None = None

    # ------------------------------------------------------------------ readiness
    def missing_fields(self) -> list[str]:
        missing = []
        if not self.rayward_api_key:
            missing.append("Rayward API key")
        if self.mod_role_id is None:
            missing.append("mod role")
        if self.mod_channel_id is None:
            missing.append("mod channel")
        if not self.sweep_trigger_user_ids and self.sweep_trigger_role_id is None:
            missing.append("sweep trigger role or user(s)")
        return missing

    @property
    def is_ready(self) -> bool:
        return not self.missing_fields()

    def effective_ban_dm_text(self, bot_default: str | None) -> str | None:
        if not self.ban_dm_enabled:
            return None
        return self.ban_dm_text or bot_default or DEFAULT_BAN_DM_TEXT

    # ------------------------------------------------------------------ /setup and /config access
    def access_tier(self, *, is_discord_admin: bool, role_ids: frozenset[int]) -> str:
        """'master' (everything, including the API keys and who holds these two roles), 'configurator'
        (everything else), or 'none'. Discord's own Administrator/Manage Server permission always grants
        master, independent of whether a Master Role has even been set."""
        if is_discord_admin:
            return "master"
        if self.master_role_id is not None and self.master_role_id in role_ids:
            return "master"
        if self.configurator_role_id is not None and self.configurator_role_id in role_ids:
            return "configurator"
        return "none"


def build_guild_config(global_cfg: GlobalConfig, settings: GuildSettings) -> Config:
    """Raises ConfigError (via Config.__post_init__) if settings are incomplete/invalid."""
    return Config(
        discord_token=global_cfg.discord_token,
        guild_id=settings.guild_id,
        rayward_api_key=settings.rayward_api_key or "",
        mod_role_id=settings.mod_role_id or 0,
        mod_channel_id=settings.mod_channel_id or 0,
        sweep_trigger_user_ids=settings.sweep_trigger_user_ids,
        sweep_trigger_role_id=settings.sweep_trigger_role_id,
        summary_channel_id=settings.summary_channel_id,
        log_forum_channel_id=settings.log_forum_channel_id,
        rayward_base_url=global_cfg.rayward_base_url,
        roblox_base_url=global_cfg.roblox_base_url,
        bloxlink_api_key=settings.bloxlink_api_key,
        bloxlink_base_url=global_cfg.bloxlink_base_url,
        bloxlink_daily_limit=global_cfg.bloxlink_daily_limit,
        bloxlink_daily_reserve=global_cfg.bloxlink_daily_reserve,
        db_path=global_cfg.db_path,
        report_only=settings.report_only,
        dry_run=settings.dry_run,
        confirmed_requires_review=settings.confirmed_requires_review,
        notify_starter_on_sweep_complete=settings.notify_starter_on_sweep_complete,
        sweep_batch_size=global_cfg.sweep_batch_size,
        roblox_batch_size=global_cfg.roblox_batch_size,
        rotector_batch_size=global_cfg.rotector_batch_size,
        raw_retention_hours=global_cfg.raw_retention_hours,
        retry=global_cfg.retry,
        rate_limit=global_cfg.rate_limit,
    )
