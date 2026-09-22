"""The narrow slice of Discord the pipeline needs, as a protocol so tests can fake it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MemberInfo:
    id: int
    nickname: str | None  # the name shown in the server (see discord_gateway.nickname_of)


class MemberNotFound(Exception):
    pass


class BanError(Exception):
    pass


class Gateway(Protocol):
    async def list_members(self) -> list[MemberInfo]:
        """All non-bot members of the configured guild."""
        ...

    async def fetch_nickname(self, discord_id: int) -> str | None:
        """LIVE fetch from the Discord API (not the cache). Raises MemberNotFound if they left."""
        ...

    async def ban(self, discord_id: int, *, reason: str) -> None:
        """Raises BanError on failure. Must not be called in dry-run mode."""
        ...

    async def send_text(self, channel_id: int, text: str) -> int | None:
        """Send a plain message; returns the message id (None if the channel is unavailable)."""
        ...

    async def send_dm(self, discord_id: int, text: str) -> bool:
        """Direct-message a user. Returns False if it could not be delivered (DMs closed, user gone)."""
        ...

    def guild_name(self) -> str:
        ...
