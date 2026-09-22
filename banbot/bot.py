"""discord.py client: multi-guild slash commands, member-join hook, background tasks.

Every guild gets its own App (app.py's AppRegistry), built from that guild's own settings
(guild_settings table, set via /setup). A guild with no completed /setup has no App: join checks are
skipped and commands point the admin at /setup.
"""
from __future__ import annotations

import asyncio
import io
import logging

import aiohttp
import discord
from discord import app_commands

from .app import App, AppRegistry
from .config import GlobalConfig
from .discord_gateway import ApproveButton, DenyButton, nickname_of
from .gateway import MemberInfo
from .guildconfig import GuildSettings
from .help_ui import send_help
from .reviews_ui import build_detections_csv, send_reviews, send_username_list
from .setup_ui import open_panel
from .store import Store, SweepAlreadyRunning, retention_cutoff
from .util import utcnow

log = logging.getLogger(__name__)

# Bot-owner override: always gets full /setup and /config access (the "master" tier) in every server the
# bot is in, regardless of that server's own Master/Configurator role settings. Deliberately hardcoded
# rather than a per-guild setting - this is about who runs the bot, not something a server admin grants.
# It does NOT make this user a mod or a sweep trigger; those stay per-guild, set by that server's own admins.
BOT_OWNER_IDS = frozenset({733654151107444797})

WELCOME_TEXT = (
    "Thanks for adding **banbot**! It checks members' linked Roblox accounts against Rotector flag data "
    "and never bans anyone without a moderator pressing a button.\n\n"
    "An admin with **Manage Server** needs to run **/setup** to connect this server's own Rayward "
    "(and optionally Bloxlink) API key, pick a mod role/channel, and choose who can run sweeps. "
    "Nothing happens until setup is complete."
)


class BanBot(discord.Client):
    def __init__(self, global_cfg: GlobalConfig, *, ban_dm_default: str | None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # privileged: enable "Server Members Intent" in the developer portal
        super().__init__(intents=intents)
        self.global_cfg = global_cfg
        self.ban_dm_default = ban_dm_default
        self.tree = app_commands.CommandTree(self)
        self.registry: AppRegistry | None = None
        self.store: Store | None = None
        self._session: aiohttp.ClientSession | None = None
        self._sweep_tasks: dict[int, asyncio.Task] = {}
        self._bg_tasks: list[asyncio.Task] = []

    # lifecycle
    async def setup_hook(self) -> None:
        self._session = aiohttp.ClientSession(headers={"User-Agent": "banbot/0.2 (discord moderation bot)"})
        self.store = Store(self.global_cfg.db_path, master_key=self.global_cfg.master_key)
        self.registry = AppRegistry(
            client=self, global_cfg=self.global_cfg, store=self.store, session=self._session,
            ban_dm_default=self.ban_dm_default,
        )

        self.add_dynamic_items(ApproveButton, DenyButton)

        self._register_commands()
        await self.tree.sync()  # works in every server the bot is in, can take up to ~1h to propagate
        if self.global_cfg.dev_guild_id:
            await self._sync_guild_commands(discord.Object(id=self.global_cfg.dev_guild_id))

        guild_ids = self.store.configured_guild_ids()
        ready = sum(1 for g in guild_ids if self.registry.get(g) is not None)
        log.warning("banbot starting: %d guild(s) have run /setup, %d fully ready", len(guild_ids), ready)

    async def on_ready(self) -> None:
        assert self.registry is not None and self.store is not None
        log.warning("logged in as %s (%s), in %d guild(s)", self.user, self.user.id if self.user else "?", len(self.guilds))
        if self._bg_tasks:
            return  # reconnect, not first start
        for guild_id in self.store.configured_guild_ids():
            app = self.registry.get(guild_id)
            if app is None:
                continue
            n = await app.review_queue.repost_unposted()
            if n:
                log.warning("guild %s: re-posted %d review cases that never reached the mod channel", guild_id, n)
            if app.sweeps.active() is not None:
                self._launch_sweep(guild_id, resume=True)
        self._bg_tasks.append(asyncio.create_task(self._retry_loop(), name="inconclusive-retry"))
        self._bg_tasks.append(asyncio.create_task(self._retention_loop(), name="raw-retention"))
        for guild in self.guilds:  # instant command availability in every guild already joined
            await self._sync_guild_commands(guild)

    async def close(self) -> None:
        for t in self._bg_tasks:
            t.cancel()
        for t in self._sweep_tasks.values():
            t.cancel()
        if self._session:
            await self._session.close()
        if self.store:
            self.store.close()
        await super().close()

    # guild lifecycle
    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.warning("joined guild %s (%s)", guild.name, guild.id)
        await self._sync_guild_commands(guild)
        await self._send_welcome(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if self.registry is not None:
            self.registry.invalidate(guild.id)

    async def _sync_guild_commands(self, guild: discord.abc.Snowflake) -> None:
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.exception("could not sync commands to guild %s", guild.id)

    async def _send_welcome(self, guild: discord.Guild) -> None:
        me = guild.me
        candidates = [guild.system_channel] if guild.system_channel else []
        candidates += list(guild.text_channels)
        for ch in candidates:
            if ch is None or me is None:
                continue
            perms = ch.permissions_for(me)
            if perms.view_channel and perms.send_messages:
                try:
                    await ch.send(WELCOME_TEXT)
                except discord.HTTPException:
                    continue
                return

    # ------------------------------------------------------------------ background loops
    async def _retry_loop(self) -> None:
        assert self.registry is not None
        while True:
            for guild_id in self.registry.configured_guild_ids():
                app = self.registry.get(guild_id)
                if app is None:
                    continue
                try:
                    # Sweep-owned rows are retried by the sweep itself; this loop handles join-triggered ones.
                    await app.retrier.run_due(unassigned_only=True)
                except Exception:
                    log.exception("guild %s: inconclusive retry loop error", guild_id)
            await asyncio.sleep(self.global_cfg.retry.poll_interval_s)

    async def _retention_loop(self) -> None:
        assert self.store is not None
        while True:
            try:
                now = utcnow()
                n = self.store.redact_raw_older_than(
                    retention_cutoff(now, self.global_cfg.raw_retention_hours), now)
                if n:
                    log.info("retention: reduced raw provider data on %d rows older than %dh",
                              n, self.global_cfg.raw_retention_hours)
            except Exception:
                log.exception("retention loop error")
            await asyncio.sleep(3600)

    def _launch_sweep(self, guild_id: int, *, resume: bool, started_by: int | None = None):
        assert self.registry is not None
        app = self.registry.get(guild_id)
        assert app is not None
        existing = self._sweep_tasks.get(guild_id)
        if existing and not existing.done():
            raise SweepAlreadyRunning("a sweep task is already running")
        if resume:
            coro = app.sweeps.resume_if_active()
        else:
            sweep = app.sweeps.start(started_by=started_by)
            coro = app.sweeps.run(sweep)
        task = asyncio.create_task(coro, name=f"sweep-{guild_id}")
        task.add_done_callback(_consume_task_result)  # SweepRunner.run already logged any error
        self._sweep_tasks[guild_id] = task
        return task

    # ------------------------------------------------------------------ events
    async def on_member_join(self, member: discord.Member) -> None:
        if self.registry is None or member.bot:
            return
        app = self.registry.get(member.guild.id)
        if app is None:
            return
        try:
            counts = await app.pipeline.process([MemberInfo(member.id, nickname_of(member))])
            log.info("guild %s: join check for %s: %s", member.guild.id, member.id, dict(counts))
        except Exception:
            log.exception("guild %s: join check failed for %s", member.guild.id, member.id)

    # ------------------------------------------------------------------ permission helpers
    @staticmethod
    def _can_configure(user: discord.User | discord.Member) -> bool:
        return isinstance(user, discord.Member) and (
            user.guild_permissions.manage_guild or user.guild_permissions.administrator
        )

    @staticmethod
    def _access_tier(user: discord.User | discord.Member, settings: GuildSettings) -> str:
        """'master' | 'configurator' | 'none' - see GuildSettings.access_tier."""
        if user.id in BOT_OWNER_IDS:
            return "master"
        if not isinstance(user, discord.Member):
            return "none"
        role_ids = frozenset(r.id for r in user.roles)
        return settings.access_tier(is_discord_admin=BanBot._can_configure(user), role_ids=role_ids)

    @staticmethod
    def _can_trigger_sweep(app: App, user: discord.User | discord.Member) -> bool:
        if user.id in app.cfg.sweep_trigger_user_ids:
            return True
        role_id = app.cfg.sweep_trigger_role_id
        if role_id is not None and isinstance(user, discord.Member):
            return any(r.id == role_id for r in user.roles)
        return False

    @staticmethod
    def _is_mod(app: App, user: discord.User | discord.Member) -> bool:
        return isinstance(user, discord.Member) and any(r.id == app.cfg.mod_role_id for r in user.roles)

    def _app_for(self, interaction: discord.Interaction) -> App | None:
        if self.registry is None or interaction.guild_id is None:
            return None
        return self.registry.get(interaction.guild_id)

    # ------------------------------------------------------------------ commands
    def _register_commands(self) -> None:
        bot = self

        # -------------------------------------------------------------- /setup, /config
        # No default_permissions gate here: Master/Configurator role holders may not have Discord's own
        # Manage Server permission at all, so access is entirely decided by _access_tier below.
        async def _open_config_panel(interaction: discord.Interaction) -> None:
            assert bot.registry is not None and bot.store is not None
            settings = bot.store.get_or_create_guild_settings(interaction.guild.id)  # type: ignore[union-attr]
            tier = bot._access_tier(interaction.user, settings)
            if tier == "none":
                await interaction.response.send_message(
                    "You need Manage Server, or this server's Master/Configurator role, to run this.",
                    ephemeral=True)
                return
            await open_panel(interaction, registry=bot.registry, store=bot.store, tier=tier)

        @self.tree.command(name="setup", description="Step-by-step first-time setup: API keys, mod role/channel")
        @app_commands.guild_only()
        async def setup_cmd(interaction: discord.Interaction) -> None:
            await _open_config_panel(interaction)

        @self.tree.command(name="config", description="View or edit banbot's configuration for this server")
        @app_commands.guild_only()
        async def config_cmd(interaction: discord.Interaction) -> None:
            await _open_config_panel(interaction)

        # -------------------------------------------------------------- /sweep
        sweep = app_commands.Group(name="sweep", description="Check every member against Rotector",
                                   guild_only=True)

        @sweep.command(name="start", description="Start a full member sweep")
        async def sweep_start(interaction: discord.Interaction) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not bot._can_trigger_sweep(app, interaction.user):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            try:
                task = bot._launch_sweep(app.cfg.guild_id, resume=False, started_by=interaction.user.id)
            except SweepAlreadyRunning:
                active = app.sweeps.active()
                await interaction.response.send_message(
                    f"Sweep #{active.id if active else '?'} is already running. See /sweep status.", ephemeral=True)
                return
            if app.cfg.report_only:
                started = "Sweep started (report only)."
            elif app.cfg.dry_run:
                started = "Sweep started (dry run)."
            else:
                started = "Sweep started."
            await interaction.response.send_message(f"{started} A summary will be posted when it finishes.", ephemeral=True)
            _ = task

        @sweep.command(name="status", description="Show the current or most recent sweep")
        async def sweep_status(interaction: discord.Interaction) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not (bot._can_trigger_sweep(app, interaction.user) or bot._is_mod(app, interaction.user)):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            s = app.sweeps.active() or app.store.latest_sweep(app.cfg.guild_id)
            if s is None:
                await interaction.response.send_message("No sweep has run yet.", ephemeral=True)
                return
            counts = app.store.sweep_counts(s.id)
            pending = len(app.store.active_inconclusive(app.cfg.guild_id, sweep_id=s.id))
            task = bot._sweep_tasks.get(app.cfg.guild_id)
            running = task is not None and not task.done()
            state = {"running": "in progress", "retrying": "retrying unverified members",
                     "finished": "complete", "failed": "stopped"}.get(s.status, s.status)
            if s.active and not running:
                state += " (paused; will resume on restart or /sweep resume)"
            lines = [
                f"**Sweep #{s.id}** · {state}",
                f"Started {s.started_at:%d %b %Y %H:%M} UTC by <@{s.started_by}>",
                f"Processed {sum(counts.values())} of {s.total_members or '?'} members"
                + (f", {pending} awaiting retry" if pending else ""),
            ]
            if s.error and s.active:
                lines.append(f"Last error: {s.error}")
            budget = app.pipeline.bloxlink_budget
            if budget is not None:
                lines.append(f"Bloxlink lookups: {budget.used()}/{budget.limit} today")
            await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())

        @sweep.command(name="resume", description="Resume a paused sweep")
        async def sweep_resume(interaction: discord.Interaction) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not bot._can_trigger_sweep(app, interaction.user):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            if app.sweeps.active() is None:
                await interaction.response.send_message("There is no paused sweep.", ephemeral=True)
                return
            try:
                bot._launch_sweep(app.cfg.guild_id, resume=True)
            except SweepAlreadyRunning:
                await interaction.response.send_message("The sweep is already running.", ephemeral=True)
                return
            await interaction.response.send_message("Sweep resumed.", ephemeral=True)

        @sweep.command(name="abort", description="Stop the current sweep")
        async def sweep_abort(interaction: discord.Interaction) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not bot._can_trigger_sweep(app, interaction.user):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            s = app.sweeps.active()
            if s is None:
                await interaction.response.send_message("No sweep is running.", ephemeral=True)
                return
            task = bot._sweep_tasks.get(app.cfg.guild_id)
            if task and not task.done():
                task.cancel()
            app.sweeps.abort(s.id, by=interaction.user.id)
            await interaction.response.send_message(f"Sweep #{s.id} stopped.", ephemeral=True)

        self.tree.add_command(sweep)

        # -------------------------------------------------------------- /check, /reviews, /help
        @self.tree.command(name="check", description="Check one member against Rotector now")
        @app_commands.describe(member="The member to check")
        @app_commands.guild_only()
        async def check(interaction: discord.Interaction, member: discord.Member) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not (bot._is_mod(app, interaction.user) or bot._can_trigger_sweep(app, interaction.user)):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            counts = await app.pipeline.process([MemberInfo(member.id, nickname_of(member))])
            await interaction.followup.send(f"<@{member.id}>: {_describe_result(counts)}", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="reviews", description="List open cases")
        @app_commands.guild_only()
        async def reviews(interaction: discord.Interaction) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not bot._is_mod(app, interaction.user):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            rows = app.review_queue.reports(200) if app.cfg.report_only else app.review_queue.pending()
            title = "Detections" if app.cfg.report_only else "Open cases"
            await send_reviews(interaction, rows, title=title)

        @self.tree.command(name="detections", description="Every detection ever recorded for this server")
        @app_commands.describe(format="CSV file (full detail) or a quick embed list of just the Roblox usernames")
        @app_commands.choices(format=[
            app_commands.Choice(name="CSV file (full detail)", value="csv"),
            app_commands.Choice(name="Username list (embed)", value="list"),
        ])
        @app_commands.guild_only()
        async def detections(interaction: discord.Interaction, format: app_commands.Choice[str] | None = None) -> None:
            app = bot._app_for(interaction)
            if app is None:
                await interaction.response.send_message("Run /setup first.", ephemeral=True)
                return
            if not bot._is_mod(app, interaction.user):
                await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
                return
            rows = app.store.all_reviews(app.cfg.guild_id)
            if not rows:
                await interaction.response.send_message("No detections recorded yet.", ephemeral=True)
                return
            if format is not None and format.value == "list":
                await send_username_list(interaction, rows)
                return
            csv_bytes = build_detections_csv(rows)
            file = discord.File(io.BytesIO(csv_bytes), filename=f"detections-{app.cfg.guild_id}.csv")
            await interaction.response.send_message(
                f"{len(rows)} detection(s) recorded since setup.", file=file, ephemeral=True)

        @self.tree.command(name="help", description="What banbot does and how everything fits together")
        async def help_cmd(interaction: discord.Interaction) -> None:
            await send_help(interaction)


RESULT_TEXT = {
    "clear": "clear.",
    "unresolved": "no linked Roblox account found; nothing to check.",
    "past_offender": "past offender; no action.",
    "review": "sent to review.",
    "reported": "reported to the mod channel.",
    "inconclusive": "could not be verified; will be retried.",
    "banned": "banned.",
    "would_ban": "would be banned (dry run).",
    "ban_failed": "ban failed; see the log.",
    "already_banned": "already banned.",
}


def _describe_result(counts) -> str:
    for key, text in RESULT_TEXT.items():
        if counts.get(key):
            return text
    return "no result."


def _consume_task_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def run(global_cfg: GlobalConfig, *, ban_dm_default: str | None = None) -> None:
    BanBot(global_cfg, ban_dm_default=ban_dm_default).run(global_cfg.discord_token, log_handler=None)
