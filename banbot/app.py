"""Wires the pieces together.

`build_app` builds one guild's pipeline out of already-constructed adapters (used directly by tests,
which build fakes). `AppRegistry` is what the bot actually uses: it lazily builds and caches one `App`
per guild, out of that guild's own Rayward/Bloxlink keys (store.py, encrypted at rest) plus the
process-wide GlobalConfig. A guild with no /setup yet, or an incomplete one, has no App - callers get
None and should point the user at /setup.
"""
from __future__ import annotations

from dataclasses import dataclass

import aiohttp
import discord

from .bloxlink import BloxlinkClient, HttpBloxlinkClient
from .budget import DailyBudget
from .config import Config, GlobalConfig
from .discord_gateway import DiscordGateway, DiscordReviewPoster
from .enforcement import Banner
from .guildconfig import build_guild_config
from .identity import IdentityResolver
from .flags import FlagProvider
from .flags.rotector import RotectorProvider
from .gateway import Gateway
from .net import AiohttpRequester, Throttle, ThreadedHttpRequester
from .pipeline import Pipeline
from .retrier import InconclusiveRetrier
from .review import ReviewPoster, ReviewQueue
from .roblox import HttpRobloxResolver, RobloxResolver
from .store import Store
from .sweep import SweepRunner
from .thumbnails import HttpRobloxThumbnailClient, RobloxThumbnailClient
from .util import Clock, SystemClock


@dataclass
class App:
    cfg: Config
    store: Store
    gateway: Gateway
    clock: Clock
    banner: Banner
    review_queue: ReviewQueue
    pipeline: Pipeline
    retrier: InconclusiveRetrier
    sweeps: SweepRunner


def build_app(
    cfg: Config,
    *,
    store: Store,
    gateway: Gateway,
    poster: ReviewPoster,
    resolver: RobloxResolver,
    provider: FlagProvider,
    bloxlink: BloxlinkClient | None = None,
    thumbnails: RobloxThumbnailClient | None = None,
    clock: Clock | None = None,
    ban_dm_template: str | None = None,
) -> App:
    clock = clock or SystemClock()
    banner = Banner(
        guild_id=cfg.guild_id, store=store, gateway=gateway, clock=clock, dry_run=cfg.dry_run,
        ban_delay_s=cfg.rate_limit.ban_delay_s, dm_template=ban_dm_template,
    )
    review_queue = ReviewQueue(
        guild_id=cfg.guild_id, store=store, poster=poster, banner=banner, gateway=gateway, clock=clock,
        mod_role_id=cfg.mod_role_id, report_only=cfg.report_only, thumbnails=thumbnails,
    )
    budget = DailyBudget(
        guild_id=cfg.guild_id, store=store, clock=clock, api="bloxlink",
        limit=cfg.bloxlink_daily_limit, reserve=cfg.bloxlink_daily_reserve,
    )
    identity = IdentityResolver(resolver=resolver, bloxlink=bloxlink, gateway=gateway, bloxlink_budget=budget)
    pipeline = Pipeline(
        cfg=cfg, store=store, gateway=gateway, identity=identity, provider=provider,
        review_queue=review_queue, banner=banner, clock=clock,
    )
    retrier = InconclusiveRetrier(guild_id=cfg.guild_id, store=store, gateway=gateway, pipeline=pipeline, clock=clock)
    sweeps = SweepRunner(cfg=cfg, store=store, gateway=gateway, pipeline=pipeline, retrier=retrier, clock=clock)
    return App(
        cfg=cfg, store=store, gateway=gateway, clock=clock, banner=banner, review_queue=review_queue,
        pipeline=pipeline, retrier=retrier, sweeps=sweeps,
    )


def build_guild_http_clients(
    cfg: Config, session: aiohttp.ClientSession
) -> tuple[FlagProvider, BloxlinkClient | None]:
    """The two integrations that are keyed per-guild: Rayward (always) and Bloxlink (optional)."""
    rl = cfg.rate_limit
    rotector_req = AiohttpRequester(
        session, throttle=Throttle(rl.rotector_min_interval_s), timeout_s=rl.http_timeout_s,
        max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s, backoff_max_s=rl.http_backoff_max_s,
        name=f"rotector[{cfg.guild_id}]",
    )
    provider = RotectorProvider(
        rotector_req, api_key=cfg.rayward_api_key, base_url=cfg.rayward_base_url, batch_size=cfg.rotector_batch_size
    )

    bloxlink: BloxlinkClient | None = None
    if cfg.bloxlink_api_key:
        bloxlink_req = AiohttpRequester(
            session, throttle=Throttle(rl.bloxlink_min_interval_s), timeout_s=rl.http_timeout_s,
            max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s,
            backoff_max_s=rl.http_backoff_max_s, name=f"bloxlink[{cfg.guild_id}]",
        )
        bloxlink = HttpBloxlinkClient(
            bloxlink_req, api_key=cfg.bloxlink_api_key, base_url=cfg.bloxlink_base_url, guild_id=cfg.guild_id
        )
    return provider, bloxlink


def build_shared_roblox_resolver(global_cfg: GlobalConfig, session: aiohttp.ClientSession) -> RobloxResolver:
    """Roblox's username/id lookup takes no key and is rate-limited by source IP, not per guild, so every
    guild shares one resolver (and one throttle) instead of each hammering Roblox independently.

    On aiohttp specifically, requests to /v1/users and /v1/usernames/users have been observed to hang
    for the full timeout while curl and stdlib http.client succeed instantly against the same host from
    the same machine - see banbot/net.py's ThreadedHttpRequester. Bloxlink and Rayward/Rotector are
    unaffected and stay on AiohttpRequester (build_guild_http_clients, below)."""
    rl = global_cfg.rate_limit
    roblox_req = ThreadedHttpRequester(
        throttle=Throttle(rl.roblox_min_interval_s), timeout_s=rl.http_timeout_s,
        max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s, backoff_max_s=rl.http_backoff_max_s,
        name="roblox",
    )
    return HttpRobloxResolver(roblox_req, base_url=global_cfg.roblox_base_url, batch_size=global_cfg.roblox_batch_size)


def build_shared_thumbnail_client(global_cfg: GlobalConfig, session: aiohttp.ClientSession) -> RobloxThumbnailClient:
    """Same reasoning as the resolver above: no per-guild key, so one shared client and throttle."""
    rl = global_cfg.rate_limit
    thumb_req = ThreadedHttpRequester(
        throttle=Throttle(rl.roblox_thumbnail_min_interval_s), timeout_s=rl.http_timeout_s,
        max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s, backoff_max_s=rl.http_backoff_max_s,
        name="roblox-thumbnails",
    )
    return HttpRobloxThumbnailClient(thumb_req, base_url=global_cfg.roblox_thumbnails_base_url)


class AppRegistry:
    """Lazily builds and caches one App per guild. Call invalidate() after /setup or /config changes
    anything that feeds into Config or the HTTP clients (keys, batch/base-url settings)."""

    def __init__(
        self, *, client: discord.Client, global_cfg: GlobalConfig, store: Store, session: aiohttp.ClientSession,
        ban_dm_default: str | None,
    ):
        self._client = client
        self._global_cfg = global_cfg
        self._store = store
        self._session = session
        self._ban_dm_default = ban_dm_default
        self._resolver = build_shared_roblox_resolver(global_cfg, session)
        self._thumbnails = build_shared_thumbnail_client(global_cfg, session)
        self._apps: dict[int, App] = {}

    @property
    def global_cfg(self) -> GlobalConfig:
        return self._global_cfg

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    def get(self, guild_id: int) -> App | None:
        cached = self._apps.get(guild_id)
        if cached is not None:
            return cached
        settings = self._store.get_guild_settings(guild_id)
        if settings is None or not settings.is_ready:
            return None
        cfg = build_guild_config(self._global_cfg, settings)
        gateway = DiscordGateway(self._client, guild_id)
        poster = DiscordReviewPoster(gateway, cfg.mod_channel_id, log_forum_channel_id=cfg.log_forum_channel_id)
        provider, bloxlink = build_guild_http_clients(cfg, self._session)
        dm_template = settings.effective_ban_dm_text(self._ban_dm_default)
        app = build_app(
            cfg, store=self._store, gateway=gateway, poster=poster, resolver=self._resolver,
            provider=provider, bloxlink=bloxlink, thumbnails=self._thumbnails, ban_dm_template=dm_template,
        )
        self._apps[guild_id] = app
        return app

    def invalidate(self, guild_id: int) -> None:
        self._apps.pop(guild_id, None)

    def configured_guild_ids(self) -> list[int]:
        return self._store.configured_guild_ids()
