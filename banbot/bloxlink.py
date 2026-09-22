"""Bloxlink: Discord user id -> verified Roblox user id.

Verified against the live API on 2026-09-16:
- GET https://api.blox.link/v4/public/guilds/{guildId}/discord-to-roblox/{discordUserId}
- Header: `Authorization: <api key>` (the raw key, NOT `Bearer <key>`)
- No key      -> {"error": "You must provide an api-key"}
- Bad key     -> {"error": "Invalid API Key"}
- Success   -> 200 {"robloxID": "146941966"}   (a STRING; confirmed from the Server API docs page)
- Not linked-> 404 (the docs page documents a 404 response for this endpoint)
- There is NO global (non-guild) discord-to-roblox route; it 404s.
- One member per request: there is no batch endpoint.
- Quota: 2,000 requests per day per server key (the developer dashboard shows "N/2000 Requests Today").
  budget.py meters this; the sweep pauses at the limit instead of failing members into retries.

`extract_roblox_id` reads `robloxID` first and also tolerates a few alternative shapes. An
unrecognised body is INCONCLUSIVE and logged in full at ERROR - never treated as "no link".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .net import HttpError, NetworkError, Requester

log = logging.getLogger(__name__)

# Bloxlink says "not linked" with one of these, rather than an HTTP error.
NOT_LINKED_MARKERS = (
    "not linked",
    "is not linked",
    "no roblox account",
    "not verified",
    "could not be found",
    "user not found",
)


@dataclass(frozen=True)
class BloxlinkLink:
    """Bloxlink has a verified Roblox account for this Discord user."""

    discord_id: int
    roblox_id: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class BloxlinkNoLink:
    """A definite answer: this member has not verified with Bloxlink."""

    discord_id: int
    detail: str


@dataclass(frozen=True)
class BloxlinkFailure:
    """We learned nothing: network error, bad key, rate limit, or an unrecognised body."""

    discord_id: int
    detail: str
    kind: Literal["error", "unparsable", "quota"] = "error"


BloxlinkResult = BloxlinkLink | BloxlinkNoLink | BloxlinkFailure


class BloxlinkClient(Protocol):
    async def lookup(self, discord_id: int) -> BloxlinkResult: ...


def _coerce_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else None
    if isinstance(value, str):
        v = value.strip()
        if v.isdigit():
            n = int(v)
            return n if n > 0 else None
    return None


def extract_roblox_id(body: Any) -> int | None:
    """Pull the Roblox user id out of a Bloxlink response, or None if the shape is unrecognised.

    Deliberately conservative: only the documented/observed locations are consulted. A None result
    means "we do not understand this response", never "there is no link".
    """
    if not isinstance(body, dict):
        return None

    # Flat forms: {"robloxID": "123"} / {"robloxId": 123} / {"roblox_id": "123"}
    for key in ("robloxID", "robloxId", "roblox_id"):
        found = _coerce_id(body.get(key))
        if found is not None:
            return found

    # Nested: {"resolved": {"roblox": {"id": 123, ...}}}
    resolved = body.get("resolved")
    if isinstance(resolved, dict):
        roblox = resolved.get("roblox")
        if isinstance(roblox, dict):
            for key in ("id", "userId", "robloxId", "robloxID"):
                found = _coerce_id(roblox.get(key))
                if found is not None:
                    return found

    # Nested: {"primaryAccount": {"id": 123}} / {"user": {"robloxId": 123}}
    for container in ("primaryAccount", "user", "account"):
        sub = body.get(container)
        if isinstance(sub, dict):
            for key in ("id", "userId", "robloxId", "robloxID", "roblox_id"):
                found = _coerce_id(sub.get(key))
                if found is not None:
                    return found
    return None


def looks_like_no_link(body: Any) -> str | None:
    """Return the error text when Bloxlink is telling us the member simply is not verified."""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, str):
        return None
    low = err.lower()
    if any(marker in low for marker in NOT_LINKED_MARKERS):
        return err
    return None


class HttpBloxlinkClient(BloxlinkClient):
    def __init__(self, requester: Requester, *, api_key: str, base_url: str, guild_id: int):
        self._request = requester
        self._base = f"{base_url.rstrip('/')}/v4/public/guilds/{guild_id}/discord-to-roblox"
        # Bloxlink takes the raw key; sending "Bearer <key>" is rejected as an invalid key.
        self._headers = {"Authorization": api_key, "Accept": "application/json"}

    async def lookup(self, discord_id: int) -> BloxlinkResult:
        try:
            resp = await self._request("GET", f"{self._base}/{discord_id}", headers=self._headers)
        except (NetworkError, HttpError) as e:
            log.warning("Bloxlink lookup failed for discord=%s: %s", discord_id, e)
            return BloxlinkFailure(discord_id, f"request failed: {e}")

        body = resp.body

        if resp.status == 404:
            # Bloxlink uses 404 both for "no link" and for a bad route; the message distinguishes them.
            text = looks_like_no_link(body) or (body.get("error") if isinstance(body, dict) else None)
            if text:
                return BloxlinkNoLink(discord_id, str(text))
            log.error("Bloxlink 404 with an unrecognised body for discord=%s: %s", discord_id, str(body)[:500])
            return BloxlinkFailure(discord_id, f"404: {str(body)[:200]}", "unparsable")

        if resp.status in (401, 403):
            log.error("Bloxlink rejected our API key (HTTP %s): %s. Check BLOXLINK_API_KEY.", resp.status, str(body)[:200])
            return BloxlinkFailure(discord_id, f"auth failure HTTP {resp.status}: {str(body)[:200]}")

        if resp.status != 200:
            return BloxlinkFailure(discord_id, f"HTTP {resp.status}: {str(body)[:200]}")

        roblox_id = extract_roblox_id(body)
        if roblox_id is not None:
            return BloxlinkLink(discord_id, roblox_id, body if isinstance(body, dict) else {"body": body})

        no_link = looks_like_no_link(body)
        if no_link:
            return BloxlinkNoLink(discord_id, no_link)

        # A 200 we cannot read. Never assume "no link" - that would silently skip a member's check.
        log.error(
            "BLOXLINK RETURNED A RESPONSE WE COULD NOT PARSE for discord=%s - treating as inconclusive. "
            "Add the correct field to extract_roblox_id in banbot/bloxlink.py. Full body: %s",
            discord_id, str(body)[:1000],
        )
        return BloxlinkFailure(discord_id, f"unrecognised response: {str(body)[:200]}", "unparsable")
