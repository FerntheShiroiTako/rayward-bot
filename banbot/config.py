"""Two layers of settings.

`GlobalConfig` comes from the environment (or a .env file): the bot token, the key used to encrypt
per-guild secrets, and tuning knobs that make sense to share across every server the bot is in.

`Config` is the *per-guild* settings a moderation pipeline actually runs on: which API keys to use,
which role/channel to post to, which safety switches are on. It used to be loaded from the
environment directly (one guild per bot process); now it is built by `build_guild_config` out of a
`GlobalConfig` plus a `GuildSettings` row that server admins set via /setup. Nothing below this line
in `Config`'s shape changed, so the whole pipeline (pipeline.py, sweep.py, enforcement.py, review.py,
identity.py, budget.py) needed zero changes to go multi-guild.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

ROBLOX_USERNAMES_BATCH_CAP = 200  # verified empirically: 201 usernames -> 400 "Too many usernames"
ROTECTOR_BATCH_CAP = 100  # per Rayward OpenAPI spec (ids maxItems: 100)


class ConfigError(ValueError):
    pass


def _str(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    v = env.get(key)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _required(env: Mapping[str, str], key: str) -> str:
    v = _str(env, key)
    if v is None:
        raise ConfigError(f"{key} is required")
    return v


def _int(env: Mapping[str, str], key: str, default: int | None = None) -> int | None:
    v = _str(env, key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer, got {v!r}") from e


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    v = _str(env, key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number, got {v!r}") from e


@dataclass(frozen=True)
class RetryConfig:
    """Inconclusive-bucket retry policy. Total attempts = 1 initial + max_retries."""

    max_retries: int = 5
    base_delay_s: float = 30.0
    max_delay_s: float = 900.0
    poll_interval_s: float = 30.0

    def delay_for(self, failed_attempts: int) -> float:
        """Exponential backoff after the Nth failed attempt (1-based)."""
        return min(self.base_delay_s * (2 ** max(failed_attempts - 1, 0)), self.max_delay_s)


@dataclass(frozen=True)
class RateLimitConfig:
    roblox_min_interval_s: float = 1.0
    roblox_thumbnail_min_interval_s: float = 1.0
    rotector_min_interval_s: float = 0.2
    # Bloxlink is one request per member with no batch endpoint, and its published limit could not be
    # verified, so this defaults deliberately slow: ~10 requests/second.
    bloxlink_min_interval_s: float = 0.1
    http_timeout_s: float = 15.0
    http_max_retries: int = 4
    http_backoff_base_s: float = 2.0
    http_backoff_max_s: float = 60.0
    sweep_chunk_delay_s: float = 2.0
    ban_delay_s: float = 1.0


@dataclass(frozen=True)
class GlobalConfig:
    """Process-wide settings, loaded once at startup. Shared by every guild the bot is in."""

    discord_token: str
    master_key: str  # encrypts/decrypts per-guild API keys at rest (banbot/crypto.py)
    rayward_base_url: str = "https://roscoe.rayward.app"
    roblox_base_url: str = "https://users.roblox.com"
    roblox_thumbnails_base_url: str = "https://thumbnails.roblox.com"
    bloxlink_base_url: str = "https://api.blox.link"
    bloxlink_daily_limit: int = 2000  # Bloxlink Server API quota per UTC day, per guild's own key
    bloxlink_daily_reserve: int = 200  # kept back from sweeps so member-join checks keep working all day
    db_path: str = "data/banbot.sqlite3"
    sweep_batch_size: int = 50
    roblox_batch_size: int = 100
    rotector_batch_size: int = 100
    raw_retention_hours: int = 24
    dev_guild_id: int | None = None  # optional: also instant-sync commands to this guild for local testing
    retry: RetryConfig = field(default_factory=RetryConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    def __post_init__(self) -> None:
        if not (1 <= self.roblox_batch_size <= ROBLOX_USERNAMES_BATCH_CAP):
            raise ConfigError(f"ROBLOX_BATCH_SIZE must be 1..{ROBLOX_USERNAMES_BATCH_CAP}")
        if not (1 <= self.rotector_batch_size <= ROTECTOR_BATCH_CAP):
            raise ConfigError(f"ROTECTOR_BATCH_SIZE must be 1..{ROTECTOR_BATCH_CAP}")
        if self.sweep_batch_size < 1:
            raise ConfigError("SWEEP_BATCH_SIZE must be >= 1")
        if self.retry.max_retries < 0:
            raise ConfigError("RETRY_MAX_RETRIES must be >= 0")
        if self.bloxlink_daily_limit < 1:
            raise ConfigError("BLOXLINK_DAILY_LIMIT must be >= 1")
        if not (0 <= self.bloxlink_daily_reserve < self.bloxlink_daily_limit):
            raise ConfigError("BLOXLINK_DAILY_RESERVE must be >= 0 and below BLOXLINK_DAILY_LIMIT")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GlobalConfig":
        env = os.environ if env is None else env
        return cls(
            discord_token=_required(env, "DISCORD_TOKEN"),
            master_key=_required(env, "MASTER_KEY"),
            rayward_base_url=(_str(env, "RAYWARD_BASE_URL", "https://roscoe.rayward.app") or "").rstrip("/"),
            roblox_base_url=(_str(env, "ROBLOX_BASE_URL", "https://users.roblox.com") or "").rstrip("/"),
            roblox_thumbnails_base_url=(_str(env, "ROBLOX_THUMBNAILS_BASE_URL", "https://thumbnails.roblox.com") or "").rstrip("/"),
            bloxlink_base_url=(_str(env, "BLOXLINK_BASE_URL", "https://api.blox.link") or "").rstrip("/"),
            bloxlink_daily_limit=_int(env, "BLOXLINK_DAILY_LIMIT", 2000),
            bloxlink_daily_reserve=_int(env, "BLOXLINK_DAILY_RESERVE", 200),
            db_path=_str(env, "DB_PATH", "data/banbot.sqlite3"),
            sweep_batch_size=_int(env, "SWEEP_BATCH_SIZE", 50),
            roblox_batch_size=_int(env, "ROBLOX_BATCH_SIZE", 100),
            rotector_batch_size=_int(env, "ROTECTOR_BATCH_SIZE", 100),
            raw_retention_hours=_int(env, "RAW_RETENTION_HOURS", 24),
            dev_guild_id=_int(env, "DEV_GUILD_ID", None),
            retry=RetryConfig(
                max_retries=_int(env, "RETRY_MAX_RETRIES", 5),
                base_delay_s=_float(env, "RETRY_BASE_DELAY_S", 30.0),
                max_delay_s=_float(env, "RETRY_MAX_DELAY_S", 900.0),
                poll_interval_s=_float(env, "RETRY_POLL_INTERVAL_S", 30.0),
            ),
            rate_limit=RateLimitConfig(
                roblox_min_interval_s=_float(env, "ROBLOX_MIN_INTERVAL_S", 1.0),
                roblox_thumbnail_min_interval_s=_float(env, "ROBLOX_THUMBNAIL_MIN_INTERVAL_S", 1.0),
                rotector_min_interval_s=_float(env, "ROTECTOR_MIN_INTERVAL_S", 0.2),
                bloxlink_min_interval_s=_float(env, "BLOXLINK_MIN_INTERVAL_S", 0.1),
                http_timeout_s=_float(env, "HTTP_TIMEOUT_S", 15.0),
                http_max_retries=_int(env, "HTTP_MAX_RETRIES", 4),
                http_backoff_base_s=_float(env, "HTTP_BACKOFF_BASE_S", 2.0),
                http_backoff_max_s=_float(env, "HTTP_BACKOFF_MAX_S", 60.0),
                sweep_chunk_delay_s=_float(env, "SWEEP_CHUNK_DELAY_S", 2.0),
                ban_delay_s=_float(env, "BAN_DELAY_S", 1.0),
            ),
        )


@dataclass(frozen=True)
class Config:
    """Effective settings for one guild's pipeline. Built by guildconfig.build_guild_config; never
    loaded from the environment directly any more."""

    discord_token: str
    guild_id: int
    rayward_api_key: str
    mod_role_id: int
    mod_channel_id: int
    sweep_trigger_user_ids: frozenset[int] = frozenset()
    sweep_trigger_role_id: int | None = None
    summary_channel_id: int | None = None
    log_forum_channel_id: int | None = None  # optional: every detection also logged as its own forum thread
    rayward_base_url: str = "https://roscoe.rayward.app"
    roblox_base_url: str = "https://users.roblox.com"
    bloxlink_api_key: str | None = None  # None disables Bloxlink; the nickname parser is then the only source
    bloxlink_base_url: str = "https://api.blox.link"
    bloxlink_daily_limit: int = 2000  # Bloxlink Server API quota per UTC day
    bloxlink_daily_reserve: int = 200  # kept back from sweeps so member-join checks keep working all day
    db_path: str = "data/banbot.sqlite3"
    report_only: bool = False  # True = post detections only: never ban, no Approve/Deny buttons
    dry_run: bool = True  # last-resort safety net; every new guild starts here until an admin turns it off
    confirmed_requires_review: bool = True  # every ban goes through a mod pressing Approve
    notify_starter_on_sweep_complete: bool = True  # DM whoever ran /sweep start when it finishes
    sweep_batch_size: int = 50
    roblox_batch_size: int = 100
    rotector_batch_size: int = 100
    raw_retention_hours: int = 24
    retry: RetryConfig = field(default_factory=RetryConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    def __post_init__(self) -> None:
        if not self.sweep_trigger_user_ids and self.sweep_trigger_role_id is None:
            raise ConfigError("set a sweep trigger role and/or user(s) in /setup")
        if not (1 <= self.roblox_batch_size <= ROBLOX_USERNAMES_BATCH_CAP):
            raise ConfigError(f"roblox batch size must be 1..{ROBLOX_USERNAMES_BATCH_CAP}")
        if not (1 <= self.rotector_batch_size <= ROTECTOR_BATCH_CAP):
            raise ConfigError(f"rotector batch size must be 1..{ROTECTOR_BATCH_CAP}")
        if self.sweep_batch_size < 1:
            raise ConfigError("sweep batch size must be >= 1")
        if self.retry.max_retries < 0:
            raise ConfigError("retry max_retries must be >= 0")
        if self.bloxlink_daily_limit < 1:
            raise ConfigError("bloxlink daily limit must be >= 1")
        if not (0 <= self.bloxlink_daily_reserve < self.bloxlink_daily_limit):
            raise ConfigError("bloxlink daily reserve must be >= 0 and below the daily limit")

    @property
    def effective_summary_channel_id(self) -> int:
        return self.summary_channel_id or self.mod_channel_id

    @property
    def bloxlink_enabled(self) -> bool:
        return bool(self.bloxlink_api_key)
