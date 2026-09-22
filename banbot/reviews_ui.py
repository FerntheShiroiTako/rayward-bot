"""Paginated /reviews listing and the two /detections views: a CSV export and a quick embed list of
just the Roblox usernames that have been detected.
"""
from __future__ import annotations

import csv
import io

import discord

from .discord_gateway import reason_text, source_text
from .store import ReviewRow

PAGE_SIZE = 10
USERNAME_PAGE_SIZE = 40

CSV_COLUMNS = (
    "ID", "Detected At (UTC)", "Discord User ID", "Roblox Username", "Roblox ID", "Status", "Reason",
    "Identified Via", "Queue Status", "Resolved At (UTC)", "Resolved By", "Resolution Note", "Times Seen",
)

_QUEUE_STATUS_TEXT = {
    "pending": "Pending",
    "approved": "Approved",
    "denied": "Denied",
    "reported": "Reported",
}


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def build_detections_csv(rows: list[ReviewRow]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            r.id,
            _fmt_dt(r.created_at),
            r.discord_id,
            r.roblox_username,
            r.roblox_id or "",
            r.status_name,
            reason_text(r.reason),
            source_text(r.identity_source),
            _QUEUE_STATUS_TEXT.get(r.status, r.status),
            _fmt_dt(r.resolved_at),
            r.resolved_by or "",
            r.resolution_note or "",
            r.seen_count,
        ])
    return buf.getvalue().encode("utf-8")


def _line(r: ReviewRow) -> str:
    line = f"#{r.id} · <@{r.discord_id}> · {r.roblox_username} · {r.status_name}"
    if r.seen_count > 1:
        line += f" · seen {r.seen_count}×"
    return line


class _PaginatedEmbedView(discord.ui.View):
    page_size: int = PAGE_SIZE

    def __init__(self, *, owner_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.page = 0
        self._sync_buttons()

    def total_items(self) -> int:
        raise NotImplementedError

    def embed(self) -> discord.Embed:
        raise NotImplementedError

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = (self.page + 1) * self.page_size >= self.total_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your list.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = max(self.page - 1, 0)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class ReviewsView(_PaginatedEmbedView):
    page_size = PAGE_SIZE

    def __init__(self, rows: list[ReviewRow], *, owner_id: int, title: str):
        self.rows = rows
        self.title = title
        super().__init__(owner_id=owner_id)

    def total_items(self) -> int:
        return len(self.rows)

    def embed(self) -> discord.Embed:
        start = self.page * self.page_size
        chunk = self.rows[start:start + self.page_size]
        e = discord.Embed(
            title=self.title,
            description="\n".join(_line(r) for r in chunk) or "No open cases.",
            color=discord.Color.blurple(),
        )
        pages = max(1, -(-len(self.rows) // self.page_size))
        e.set_footer(text=f"Page {self.page + 1}/{pages} · {len(self.rows)} total")
        return e


async def send_reviews(interaction: discord.Interaction, rows: list[ReviewRow], *, title: str) -> None:
    view = ReviewsView(rows, owner_id=interaction.user.id, title=title)
    await interaction.response.send_message(
        embed=view.embed(), view=view if len(rows) > PAGE_SIZE else None, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class UsernameListView(_PaginatedEmbedView):
    page_size = USERNAME_PAGE_SIZE

    def __init__(self, usernames: list[str], *, owner_id: int, total_detections: int):
        self.usernames = usernames
        self.total_detections = total_detections
        super().__init__(owner_id=owner_id)

    def total_items(self) -> int:
        return len(self.usernames)

    def embed(self) -> discord.Embed:
        start = self.page * self.page_size
        chunk = self.usernames[start:start + self.page_size]
        e = discord.Embed(
            title="Detected Roblox accounts",
            description="\n".join(chunk) or "No detections recorded yet.",
            color=discord.Color.blurple(),
        )
        pages = max(1, -(-len(self.usernames) // self.page_size))
        e.set_footer(text=f"Page {self.page + 1}/{pages} · {len(self.usernames)} unique account(s) "
                          f"· {self.total_detections} detection(s) total")
        return e


async def send_username_list(interaction: discord.Interaction, rows: list[ReviewRow]) -> None:
    usernames = sorted({r.roblox_username for r in rows if r.roblox_username}, key=str.lower)
    view = UsernameListView(usernames, owner_id=interaction.user.id, total_detections=len(rows))
    await interaction.response.send_message(
        embed=view.embed(), view=view if len(usernames) > USERNAME_PAGE_SIZE else None, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
