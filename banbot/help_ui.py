"""The /help command: a short overview plus a topic picker for the rest, written the way you'd actually
explain the bot to a new moderator rather than as a dry feature list.
"""
from __future__ import annotations

import discord

TOPICS: dict[str, tuple[str, str]] = {
    "overview": (
        "Overview",
        "banbot keeps an eye on the Roblox accounts linked to your members and flags anything Rotector "
        "has concerns about. It never bans anyone on its own. Every flagged account lands in your mod "
        "channel with Ban and Dismiss buttons, and only a mod pressing Ban actually removes someone.\n\n"
        "If a lookup fails, times out, or comes back looking wrong, that counts as \"we don't know,\" not "
        "\"this person is fine,\" so nothing gets waved through just because an API had a bad day. Every "
        "server that adds the bot runs its own checks with its own Rayward key, and starts in dry-run mode "
        "until an admin decides it's ready to go live.",
    ),
    "checks": (
        "How a check works",
        "When a member joins, or a sweep reaches them, the bot first works out which Roblox account is "
        "theirs. If they've verified through Bloxlink, that's used and trusted outright. If not, it looks "
        "for a `(@username)` tag at the end of their nickname. Either way, once it has a Roblox account, "
        "that account gets checked against Rotector's flag data through Rayward.\n\n"
        "Unflagged accounts are left alone. Past offenders (flagged before, cleared since) get logged but "
        "not touched. Anything Rotector actually flags goes to your mod queue for a human to look at, "
        "unless a server has turned off *Confirmed requires review*, in which case Confirmed accounts get "
        "banned automatically. Even then, the bot re-checks their identity live right before the ban, in "
        "case anything changed since the original check.\n\n"
        "One more thing gets checked first, before any of that: if the Roblox account showing up was "
        "already banned in this server under a *different* Discord account, that's treated as ban evasion "
        "and routed exactly like a Confirmed hit. No separate setting needed for that one.",
    ),
    "reviews": (
        "Review queue & logs",
        "Flagged cases post to your mod channel as an embed with Ban and Dismiss buttons. Only your mod "
        "role can press them (everyone else gets told no), and resolved cases have their buttons removed "
        "so nobody double-clicks something that's already been decided.\n\n"
        "**/reviews** shows what's still open, ten at a time with buttons to page through. **/detections** "
        "goes further back: every detection the bot has ever recorded for your server, resolved or not, "
        "either as a CSV you can open in a spreadsheet or a quick list of just the Roblox usernames if you "
        "don't need the full detail.\n\n"
        "If you'd rather keep a permanent record outside the mod queue, set a forum channel as the "
        "detection log in **/config** → More Settings. Every detection gets its own thread there the "
        "moment it's found, kept separate from whatever happens to the case afterward.",
    ),
    "sweeps": (
        "Sweeps",
        "**/sweep start** walks every member in the server, checking each one the same way a join does. "
        "Only whoever your server names as a sweep trigger, whether that's a role, specific people, or "
        "both, can start one, and only one can run at a time. If the bot restarts partway through, it "
        "picks up where it left off.\n\n"
        "**/sweep status** shows how far a sweep has gotten, **/sweep resume** restarts a paused one, and "
        "**/sweep abort** stops it outright. When a sweep finishes, a summary posts to your summary "
        "channel (or the mod channel, if you haven't set one) and gets DMed to whoever started it, unless "
        "that's been turned off.",
    ),
    "modes": (
        "Safety modes",
        "Every server starts cautious. **Dry run** means the bot still does everything except actually "
        "ban; you get a log line and an audit record instead of a real ban. **Report only** goes further "
        "still: nothing gets banned or even queued for a decision, detections just get posted as plain "
        "notices. **Confirmed requires review** means nothing bans itself, full stop, until a mod presses "
        "the button.\n\n"
        "All three, plus whether banned members get DMed first and whether sweep summaries go to whoever "
        "ran them, live in **/config** → Safety Modes. Turning off Dry run or Confirmed requires review "
        "asks you to confirm first, since those two are what stand between a false positive and an actual "
        "ban.",
    ),
    "setup": (
        "Setup & access",
        "**/setup** walks a new server through connecting its own Rayward key, picking a mod role and "
        "channel, and choosing who can run sweeps. Nothing gets checked until that's done. **/config** "
        "opens the same panel afterward to change any of it.\n\n"
        "By default only Manage Server can touch this, but a server can delegate it without handing that "
        "permission out. A **Master role** gets full access including the API keys, and a **Configurator "
        "role** gets everything else (roles, channels, safety modes) but never the keys themselves. Both "
        "are set from the panel's Access Control section.",
    ),
}

DEFAULT_TOPIC = "overview"


def _embed(topic: str) -> discord.Embed:
    title, body = TOPICS[topic]
    e = discord.Embed(title=f"banbot help — {title}", description=body, color=discord.Color.blurple())
    e.set_footer(text="Pick a topic below for more.")
    return e


class _TopicSelect(discord.ui.Select["HelpView"]):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(label=title, value=key, default=(key == current))
            for key, (title, _body) in TOPICS.items()
        ]
        super().__init__(placeholder="Jump to a topic…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        topic = self.values[0]
        view: HelpView = self.view  # type: ignore[assignment]
        view.set_topic(topic)
        await interaction.response.edit_message(embed=_embed(topic), view=view)


class HelpView(discord.ui.View):
    def __init__(self, *, owner_id: int, topic: str = DEFAULT_TOPIC):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.topic = topic
        self.add_item(_TopicSelect(topic))

    def set_topic(self, topic: str) -> None:
        self.topic = topic
        self.clear_items()
        self.add_item(_TopicSelect(topic))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run /help yourself to browse this.", ephemeral=True)
            return False
        return True


async def send_help(interaction: discord.Interaction) -> None:
    view = HelpView(owner_id=interaction.user.id)
    await interaction.response.send_message(embed=_embed(DEFAULT_TOPIC), view=view, ephemeral=True)
