"""Step 2: map between Roblox usernames and user IDs.

Facts verified against the live API:
- POST /v1/usernames/users accepts up to 200 usernames per request (201 -> 400 "Too many usernames")
  and also matches *previous* usernames, so `name` may differ from `requestedUsername`
- POST /v1/users accepts up to 200 ids per request and returns {id, name, displayName, hasVerifiedBadge};
  this is how members identified by Bloxlink (who give us an id, not a name) get a username to display
- both rate-limit aggressively (429), hence the throttle + backoff in net.py
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

from .net import HttpError, NetworkError, Requester
from .util import chunked, unique

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RobloxUser:
    id: int
    name: str  # canonical current username
    display_name: str
    requested_username: str  # what we asked for (lowercased key)

    @property
    def profile_url(self) -> str:
        return f"https://www.roblox.com/users/{self.id}/profile"


@dataclass(frozen=True)
class ResolveFailure:
    kind: Literal["not_found", "error"]
    detail: str


ResolveResult = dict[str, RobloxUser | ResolveFailure]  # keyed by lowercased username


LookupResult = dict[int, RobloxUser | ResolveFailure]  # keyed by Roblox user id


class RobloxResolver(Protocol):
    async def resolve(self, usernames: Sequence[str]) -> ResolveResult: ...

    async def lookup_ids(self, roblox_ids: Sequence[int]) -> LookupResult: ...


class HttpRobloxResolver:
    def __init__(self, requester: Requester, *, base_url: str, batch_size: int):
        self._request = requester
        self._url = f"{base_url.rstrip('/')}/v1/usernames/users"
        self._ids_url = f"{base_url.rstrip('/')}/v1/users"
        self._batch = batch_size

    async def resolve(self, usernames: Sequence[str]) -> ResolveResult:
        out: ResolveResult = {}
        keys = unique(u.lower() for u in usernames)
        for chunk in chunked(keys, self._batch):
            try:
                resp = await self._request(
                    "POST", self._url, json_body={"usernames": chunk, "excludeBannedUsers": False}
                )
            except (NetworkError, HttpError) as e:
                log.warning("Roblox resolve failed for %d usernames: %s", len(chunk), e)
                for u in chunk:
                    out[u] = ResolveFailure("error", str(e))
                continue

            body = resp.body
            if resp.status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
                detail = f"unexpected response (HTTP {resp.status}): {str(body)[:200]}"
                log.error("Roblox resolve: %s", detail)
                for u in chunk:
                    out[u] = ResolveFailure("error", detail)
                continue

            for item in body["data"]:
                try:
                    requested = str(item["requestedUsername"]).lower()
                    user = RobloxUser(
                        id=int(item["id"]),
                        name=str(item["name"]),
                        display_name=str(item.get("displayName", "")),
                        requested_username=requested,
                    )
                except (KeyError, TypeError, ValueError) as e:
                    log.error("Roblox resolve: malformed entry %r (%s)", item, e)
                    continue
                out[requested] = user

            for u in chunk:
                out.setdefault(u, ResolveFailure("not_found", "Roblox returned no user for this username"))
        return out

    async def lookup_ids(self, roblox_ids: Sequence[int]) -> LookupResult:
        """Roblox user id -> username, for accounts identified by Bloxlink rather than by nickname."""
        out: LookupResult = {}
        ids = unique(int(i) for i in roblox_ids)
        for chunk in chunked(ids, self._batch):
            try:
                resp = await self._request(
                    "POST", self._ids_url, json_body={"userIds": chunk, "excludeBannedUsers": False}
                )
            except (NetworkError, HttpError) as e:
                log.warning("Roblox id lookup failed for %d ids: %s", len(chunk), e)
                for rid in chunk:
                    out[rid] = ResolveFailure("error", str(e))
                continue

            body = resp.body
            if resp.status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
                detail = f"unexpected response (HTTP {resp.status}): {str(body)[:200]}"
                log.error("Roblox id lookup: %s", detail)
                for rid in chunk:
                    out[rid] = ResolveFailure("error", detail)
                continue

            for item in body["data"]:
                try:
                    rid = int(item["id"])
                    name = str(item["name"])
                except (KeyError, TypeError, ValueError) as e:
                    log.error("Roblox id lookup: malformed entry %r (%s)", item, e)
                    continue
                out[rid] = RobloxUser(
                    id=rid, name=name, display_name=str(item.get("displayName", "")), requested_username=name.lower()
                )

            for rid in chunk:
                out.setdefault(rid, ResolveFailure("not_found", "Roblox returned no user for this id"))
        return out
