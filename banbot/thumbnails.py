"""Roblox avatar headshots, attached to detection embeds so mods can see who they're looking at.

Purely cosmetic. Unlike every other integration in this bot, a failure here is never treated as
"unknown" and never blocks, delays or retries a detection - it just means the embed posts without a
picture. `client.lookup_headshots` returning fewer ids than asked for (or none at all) is a normal,
expected outcome, not an error condition.

Verified against the live API (2026-09):
- GET /v1/users/avatar-headshot?userIds=1,2,3&size=150x150&format=Png&isCircular=false, on
  thumbnails.roblox.com - a different host to users.roblox.com, no key needed, max 100 ids per request.
- Response: {"data": [{"targetId": id, "state": "Completed"|..., "imageUrl": str, "version": str}]}.
  Only "Completed" entries carry a usable imageUrl; anything else (still rendering, moderated account,
  a state this client doesn't recognise) is skipped the same way a failed lookup is.
"""
from __future__ import annotations

import logging
from typing import Protocol, Sequence
from urllib.parse import urlencode

from .net import HttpError, NetworkError, Requester
from .util import chunked, unique

log = logging.getLogger(__name__)

THUMBNAIL_BATCH_CAP = 100
DEFAULT_SIZE = "150x150"


class RobloxThumbnailClient(Protocol):
    async def lookup_headshots(self, roblox_ids: Sequence[int]) -> dict[int, str]:
        """Only ids with a usable image right now are present in the result. A missing id means no
        picture is available - never raise, and never treat a miss as meaning anything else."""
        ...


class HttpRobloxThumbnailClient:
    def __init__(self, requester: Requester, *, base_url: str, batch_size: int = THUMBNAIL_BATCH_CAP,
                size: str = DEFAULT_SIZE):
        self._request = requester
        self._url = f"{base_url.rstrip('/')}/v1/users/avatar-headshot"
        self._batch = min(batch_size, THUMBNAIL_BATCH_CAP)
        self._size = size

    async def lookup_headshots(self, roblox_ids: Sequence[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        ids = unique(int(i) for i in roblox_ids)
        for chunk in chunked(ids, self._batch):
            query = urlencode({
                "userIds": ",".join(str(i) for i in chunk),
                "size": self._size,
                "format": "Png",
                "isCircular": "false",
            })
            try:
                resp = await self._request("GET", f"{self._url}?{query}")
            except (NetworkError, HttpError) as e:
                log.info("thumbnail lookup failed for %d id(s), posting without pictures: %s", len(chunk), e)
                continue

            if resp.status != 200:
                log.info("thumbnail lookup: HTTP %s for %d id(s), posting without pictures",
                         resp.status, len(chunk))
                continue

            data = resp.body.get("data") if isinstance(resp.body, dict) else None
            if not isinstance(data, list):
                log.info("thumbnail lookup: unexpected response shape for %d id(s), posting without pictures",
                         len(chunk))
                continue

            for entry in data:
                if not isinstance(entry, dict) or entry.get("state") != "Completed":
                    continue
                target, url = entry.get("targetId"), entry.get("imageUrl")
                if isinstance(target, int) and isinstance(url, str) and url:
                    out[target] = url
        return out
