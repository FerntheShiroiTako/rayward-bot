"""The /setup and /config wizard: one interactive panel (role/channel selects, modals for the two API
keys, toggle buttons for the safety switches, and a Test & Finish button that runs diagnostics before
marking the guild ready). Both commands open the same ConfigPanel - /setup is just the first visit.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from . import diagnostics
from .app import build_guild_http_clients
from .guildconfig import GuildSettings, build_guild_config
from .config import ConfigError
from .util import utcnow

if TYPE_CHECKING:
    from .app import AppRegistry
    from .store import Store

log = logging.getLogger(__name__)


def _check(ok: bool) -> str:
    return "✅" if ok else "❌"


def summary_embed(settings: GuildSettings, guild: discord.Guild, tier: str) -> discord.Embed:
    missing = settings.missing_fields()
    color = discord.Color.green() if settings.is_ready else discord.Color.orange()
    e = discord.Embed(title="banbot configuration", color=color)
    e.add_field(name="Rayward API key", value="Set" if settings.rayward_api_key else f"{_check(False)} Not set (required)")
    e.add_field(name="Bloxlink API key",
                value="Set" if settings.bloxlink_api_key else "Off (nickname parsing only)")
    e.add_field(name="Master role",
                value=f"<@&{settings.master_role_id}>" if settings.master_role_id else "(Manage Server permission only)")
    e.add_field(name="Configurator role",
                value=f"<@&{settings.configurator_role_id}>" if settings.configurator_role_id else "Not set")
    e.add_field(name="Mod role",
                value=f"<@&{settings.mod_role_id}>" if settings.mod_role_id else f"{_check(False)} Not set")
    e.add_field(name="Mod channel",
                value=f"<#{settings.mod_channel_id}>" if settings.mod_channel_id else f"{_check(False)} Not set")
    e.add_field(name="Summary channel",
                value=f"<#{settings.summary_channel_id}>" if settings.summary_channel_id else "(same as mod channel)")
    e.add_field(name="Detection log (forum)",
                value=f"<#{settings.log_forum_channel_id}>" if settings.log_forum_channel_id else "Off")
    trig = []
    if settings.sweep_trigger_role_id:
        trig.append(f"<@&{settings.sweep_trigger_role_id}>")
    if settings.sweep_trigger_user_ids:
        trig.append(", ".join(f"<@{u}>" for u in sorted(settings.sweep_trigger_user_ids)))
    e.add_field(name="Sweep trigger", value=" and ".join(trig) if trig else f"{_check(False)} Not set")
    e.add_field(name="Dry run", value=("ON - no real bans" if settings.dry_run else "**OFF - bans are real**"))
    e.add_field(name="Report only", value=("ON - detections posted, never banned" if settings.report_only else "OFF"))
    e.add_field(name="Confirmed requires review",
                value=("ON - a mod must approve every ban" if settings.confirmed_requires_review
                       else "**OFF - Confirmed accounts auto-ban**"))
    if missing:
        e.description = f"{_check(False)} **Still needed:** " + ", ".join(missing)
    elif not settings.setup_completed:
        e.description = f"{_check(True)} Ready. Click **Test & Finish** to verify and go live."
    else:
        e.description = f"{_check(True)} Configured. Edit anything below, or run /sweep start."
    access = "Master access" if tier == "master" else "Configurator access (no API keys)"
    e.set_footer(text=f"{guild.name} · {access} · changes save immediately")
    return e


class ConfigPanel(discord.ui.View):
    def __init__(self, *, registry: "AppRegistry", store: "Store", guild: discord.Guild, settings: GuildSettings,
                tier: str):
        super().__init__(timeout=900)
        self.registry = registry
        self.store = store
        self.guild = guild
        self.settings = settings
        self.tier = tier  # "master" | "configurator" - "none" never reaches this class
        self.add_item(_ModRoleSelect())
        self.add_item(_ModChannelSelect())
        self.add_item(_TriggerRoleSelect())
        if tier == "master":
            self.add_item(_RaywardKeyButton())
            self.add_item(_BloxlinkKeyButton())
        self.add_item(_ModesButton())
        self.add_item(_MoreSettingsButton())
        if tier == "master":
            self.add_item(_AccessControlButton())
        self.add_item(_TestAndFinishButton())

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.settings = self.store.update_guild_settings(self.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.registry.invalidate(self.guild.id)
        await interaction.response.edit_message(embed=summary_embed(self.settings, self.guild, self.tier), view=self)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.settings = self.store.get_guild_settings(self.guild.id) or self.settings
        embed = summary_embed(self.settings, self.guild, self.tier)
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)


# ---------------------------------------------------------------------- selects (role / channel)

class _ModRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Mod role — can press Ban / Dismiss on review cases", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, mod_role_id=self.values[0].id)


class _TriggerRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Sweep trigger role (optional) — can run /sweep start", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, sweep_trigger_role_id=self.values[0].id)


class _ModChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Mod channel — where review cases are posted",
                         channel_types=[discord.ChannelType.text], row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, mod_channel_id=self.values[0].id)


class _SummaryChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Summary channel (optional) — defaults to the mod channel",
                         channel_types=[discord.ChannelType.text], row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, summary_channel_id=self.values[0].id)


class _LogForumChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Detection log forum (optional) — every detection gets its own thread",
                         channel_types=[discord.ChannelType.forum], row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, log_forum_channel_id=self.values[0].id)


# ---------------------------------------------------------------------- modals (secrets / free text)

class _KeyModal(discord.ui.Modal):
    def __init__(self, panel: ConfigPanel, *, field_name: str, title: str, label: str, current: str | None):
        super().__init__(title=title)
        self.panel = panel
        self.field_name = field_name
        self.value_input = discord.ui.TextInput(
            label=label, required=False, max_length=300,
            placeholder="pasted here, not shown to anyone else — leave blank to clear",
            default="" if not current else None,  # never echo a real key back into the box
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.value_input.value.strip() or None
        await self.panel.apply(interaction, **{self.field_name: value})


class _RaywardKeyButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Rayward Key", style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        modal = _KeyModal(
            panel, field_name="rayward_api_key", title="Rayward API key (rayward.app/signin)",
            label="Rayward key (starts with rwd_)", current=panel.settings.rayward_api_key,
        )
        await interaction.response.send_modal(modal)


class _BloxlinkKeyButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Bloxlink Key", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        modal = _KeyModal(
            panel, field_name="bloxlink_api_key", title="Bloxlink API key (blox.link)",
            label="Bloxlink server key", current=panel.settings.bloxlink_api_key,
        )
        await interaction.response.send_modal(modal)


class _TriggerUsersModal(discord.ui.Modal, title="Sweep trigger users"):
    def __init__(self, target: "ConfigPanel | _MoreSettingsView"):
        super().__init__()
        self.target = target
        current = ",".join(str(u) for u in sorted(target.settings.sweep_trigger_user_ids))
        self.ids_input = discord.ui.TextInput(
            label="Comma-separated Discord user IDs", required=False, max_length=500,
            default=current or None, placeholder="e.g. 123456789012345678, 234567890123456789",
        )
        self.add_item(self.ids_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.ids_input.value.strip()
        try:
            ids = frozenset(int(p.strip()) for p in raw.split(",") if p.strip())
        except ValueError:
            await interaction.response.send_message(
                "Could not parse that as a comma-separated list of numeric user IDs.", ephemeral=True)
            return
        await self.target.apply(interaction, sweep_trigger_user_ids=ids)


class _TriggerUsersButton(discord.ui.Button["_MoreSettingsView"]):
    def __init__(self):
        super().__init__(label="Trigger Users", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _MoreSettingsView = self.view  # type: ignore[assignment]
        await interaction.response.send_modal(_TriggerUsersModal(view))


# ---------------------------------------------------------------------- more settings (channels used less often)

def _more_settings_embed(settings: GuildSettings) -> discord.Embed:
    e = discord.Embed(title="More settings", color=discord.Color.blurple())
    e.add_field(name="Summary channel",
                value=f"<#{settings.summary_channel_id}>" if settings.summary_channel_id else "(same as mod channel)",
                inline=False)
    e.add_field(name="Detection log (forum)",
                value=f"<#{settings.log_forum_channel_id}>" if settings.log_forum_channel_id else "Off", inline=False)
    trig_users = ", ".join(f"<@{u}>" for u in sorted(settings.sweep_trigger_user_ids)) if settings.sweep_trigger_user_ids else "None"
    e.add_field(name="Sweep trigger users", value=trig_users, inline=False)
    return e


class _MoreSettingsButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="More Settings", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=_more_settings_embed(panel.settings), view=_MoreSettingsView(panel), ephemeral=True)


class _MoreSettingsView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=300)
        self.panel = panel
        self.add_item(_SummaryChannelSelect())
        self.add_item(_LogForumChannelSelect())
        self.add_item(_TriggerUsersButton())

    @property
    def settings(self) -> GuildSettings:
        return self.panel.settings

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(embed=_more_settings_embed(self.panel.settings), view=self)


# ---------------------------------------------------------------------- access control (master-tier only)

def _access_control_embed(settings: GuildSettings) -> discord.Embed:
    e = discord.Embed(
        title="Access control", color=discord.Color.blurple(),
        description=(
            "**Master role**: full access, same as Manage Server, including the API keys and this panel.\n"
            "**Configurator role**: everything else (roles, channels, safety modes), but never the API keys."
        ),
    )
    e.add_field(name="Master role",
                value=f"<@&{settings.master_role_id}>" if settings.master_role_id else "(Manage Server permission only)",
                inline=False)
    e.add_field(name="Configurator role",
                value=f"<@&{settings.configurator_role_id}>" if settings.configurator_role_id else "Not set",
                inline=False)
    return e


class _MasterRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Master role — full access, same as Manage Server", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, master_role_id=self.values[0].id)


class _ConfiguratorRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Configurator role — every setting except the API keys", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, configurator_role_id=self.values[0].id)


class _AccessControlButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Access Control", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=_access_control_embed(panel.settings), view=_AccessControlView(panel), ephemeral=True)


class _AccessControlView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=300)
        self.panel = panel
        self.add_item(_MasterRoleSelect())
        self.add_item(_ConfiguratorRoleSelect())

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(embed=_access_control_embed(self.panel.settings), view=self)


# ---------------------------------------------------------------------- mode toggles

class _ModesButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Safety Modes", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        await interaction.response.send_message(
            "Safety modes for **" + panel.guild.name + "**. Turning off *Dry run* or *Confirmed requires review* "
            "needs a second confirmation - these are what stop the bot from banning people by mistake.",
            view=_ModesView(panel), ephemeral=True,
        )


class _ModesView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=300)
        self.panel = panel
        self.add_item(_ToggleButton("report_only", "Report Only", row=0))
        self.add_item(_ToggleButton("dry_run", "Dry Run", row=0, confirm_on_disable=True))
        self.add_item(_ToggleButton("confirmed_requires_review", "Confirmed Requires Review", row=1,
                                    confirm_on_disable=True))
        self.add_item(_ToggleButton("notify_starter_on_sweep_complete", "DM Sweep Starter", row=1))
        self.add_item(_ToggleButton("ban_dm_enabled", "Ban DM", row=2))


class _ToggleButton(discord.ui.Button["_ModesView"]):
    def __init__(self, field: str, label: str, *, row: int, confirm_on_disable: bool = False):
        self.field = field
        self.confirm_on_disable = confirm_on_disable
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _ModesView = self.view  # type: ignore[assignment]
        panel = view.panel
        current = getattr(panel.settings, self.field)
        turning_off = current is True
        if turning_off and self.confirm_on_disable:
            await interaction.response.send_message(
                f"Turn **{self.label}** off? " + _danger_text(self.field),
                view=_ConfirmView(panel, self.field, False), ephemeral=True,
            )
            return
        panel.settings = panel.store.update_guild_settings(
            panel.guild.id, by=interaction.user.id, at=utcnow(), **{self.field: not current}
        )
        panel.registry.invalidate(panel.guild.id)
        await interaction.response.send_message(
            f"{self.label} is now **{'ON' if not current else 'OFF'}**.", ephemeral=True)


def _danger_text(field: str) -> str:
    if field == "dry_run":
        return "Bans will become **real** the moment a mod presses Ban."
    if field == "confirmed_requires_review":
        return "**Confirmed** accounts will be banned automatically, with no mod pressing Approve."
    return "This changes bot behaviour immediately."


class _ConfirmView(discord.ui.View):
    def __init__(self, panel: ConfigPanel, field: str, new_value: bool):
        super().__init__(timeout=60)
        self.panel = panel
        self.field = field
        self.new_value = new_value

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **{self.field: self.new_value}
        )
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(content=f"{self.field} is now **{self.new_value}**.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="No change made.", view=None)


# ---------------------------------------------------------------------- diagnostics

class _TestAndFinishButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Test & Finish", style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        settings = panel.settings
        missing = settings.missing_fields()
        if missing:
            await interaction.response.send_message(
                "Not ready yet - still needed: " + ", ".join(missing), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg = build_guild_config(panel.registry.global_cfg, settings)
        except ConfigError as e:
            await interaction.followup.send(f"Configuration is invalid: {e}", ephemeral=True)
            return

        provider, bloxlink = build_guild_http_clients(cfg, panel.registry.session)
        results = [await diagnostics.check_rayward(provider)]
        bloxlink_result = await diagnostics.check_bloxlink(bloxlink)
        if bloxlink_result is not None:
            results.append(bloxlink_result)
        results += diagnostics.check_discord_permissions(panel.guild, cfg)

        lines = [f"{_check(r.ok)} **{r.name}**" + (f" — {r.detail}" if r.detail else "") for r in results]
        all_ok = all(r.ok for r in results)
        if all_ok:
            panel.settings = panel.store.update_guild_settings(
                panel.guild.id, by=interaction.user.id, at=utcnow(), setup_completed=True, setup_by=interaction.user.id,
            )
            panel.registry.invalidate(panel.guild.id)
            lines.append("\n**All checks passed. banbot is live in this server.**")
        else:
            lines.append("\nFix the failing checks above, then click Test & Finish again.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        await panel.refresh(interaction)


async def open_panel(interaction: discord.Interaction, *, registry: "AppRegistry", store: "Store", tier: str) -> None:
    assert interaction.guild is not None
    settings = store.get_or_create_guild_settings(interaction.guild.id)
    panel = ConfigPanel(registry=registry, store=store, guild=interaction.guild, settings=settings, tier=tier)
    await interaction.response.send_message(
        embed=summary_embed(settings, interaction.guild, tier), view=panel, ephemeral=True)
