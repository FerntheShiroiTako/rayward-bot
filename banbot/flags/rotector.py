"""Rayward -> Rotector source. Contract taken from https://rayward.app/docs/rotector.json (OpenAPI 3.0.3).

- Base URL   : https://roscoe.rayward.app
- Auth       : Authorization: Bearer rwd_...
- Batch      : POST /v2/lookup/rotector/roblox/user  {"ids": [..]}  (max 100 ids)
- Response   : {"success": true, "data": {"<id>": {"id", "flagType", "statusLabel", "reasons": [...], ...}}}
               Every id sent is present in `data`, unflagged ones included.
- Errors     : {"success": false, "error", "code", "requestId"}; 429 carries Retry-After; 503 = source
               did not answer and explicitly "does not mean the account is clean".
- Terms      : responses may not be stored > 24h; anything shown must be labelled as Rotector data.

THE status mapping lives in FLAG_TYPES below and nowhere else.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from ..net import HttpError, NetworkError, Requester
from ..util import chunked, unique
from .base import FlagOutcome, FlagProvider, FlagResult

log = logging.getLogger(__name__)

PROVIDER_NAME = "rotector"

# flagType -> (Rotector's name for it, our outcome)
# Decided with Fern on 2026-09-10: everything that is not Clear/Past Offender/Confirmed goes to review,
# including the process states Queued (3), Provisional Flag (4) and Redacted (8).
FLAG_TYPES: dict[int, tuple[str, FlagOutcome]] = {
    0: ("Unflagged", FlagOutcome.CLEAR),
    1: ("Flagged", FlagOutcome.REVIEW),
    2: ("Confirmed", FlagOutcome.CONFIRMED),
    3: ("Queued", FlagOutcome.REVIEW),
    4: ("Provisional Flag", FlagOutcome.REVIEW),
    5: ("Mixed", FlagOutcome.REVIEW),
    6: ("Past Offender", FlagOutcome.PAST_OFFENDER),
    8: ("Redacted", FlagOutcome.REVIEW),
}


def map_flag_type(flag_type: Any) -> tuple[int | None, str, FlagOutcome]:
    """Map a raw `flagType` value to (code, name, outcome). Unknown -> UNMAPPED."""
    code: int | None = None
    if isinstance(flag_type, bool):  # bool is an int subclass; never accept it
        code = None
    elif isinstance(flag_type, int):
        code = flag_type
    elif isinstance(flag_type, float) and flag_type.is_integer():
        code = int(flag_type)
    elif isinstance(flag_type, str) and flag_type.strip().isdigit():
        code = int(flag_type.strip())
    if code is None or code not in FLAG_TYPES:
        return code, f"unmapped({flag_type!r})", FlagOutcome.UNMAPPED
    name, outcome = FLAG_TYPES[code]
    return code, name, outcome


def summarize_entry(entry: dict[str, Any], *, max_reasons: int = 4, max_len: int = 900) -> str:
    """Short, human-readable digest of a Rotector user entry for embeds/logs."""
    parts: list[str] = []
    label = entry.get("statusLabel")
    if label:
        parts.append(f"Status: {label}")
    category = entry.get("categoryLabel") or entry.get("category")
    if category:
        parts.append(f"Category: {category}")
    reasons = entry.get("reasons")
    if isinstance(reasons, list) and reasons:
        lines = []
        for r in reasons[:max_reasons]:
            if not isinstance(r, dict):
                continue
            title = r.get("title") or r.get("type") or "?"
            srcs = [s.get("label") or s.get("id") for s in r.get("sources", []) if isinstance(s, dict)]
            line = f"- {title}"
            if srcs:
                line += f" ({', '.join(str(s) for s in srcs[:3])})"
            ev_text = next(
                (e.get("text") for e in r.get("evidence", []) if isinstance(e, dict) and e.get("kind") == "text"),
                None,
            )
            if ev_text:
                line += f": {str(ev_text)[:160]}"
            lines.append(line)
        if len(reasons) > max_reasons:
            lines.append(f"- ... {len(reasons) - max_reasons} more")
        parts.append("Reasons:\n" + "\n".join(lines))
    elif reasons is None:
        parts.append("Reasons: none returned")
    text = "\n".join(parts) if parts else "(no details)"
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


class RotectorProvider(FlagProvider):
    name = PROVIDER_NAME

    def __init__(self, requester: Requester, *, api_key: str, base_url: str, batch_size: int):
        self._request = requester
        self._url = f"{base_url.rstrip('/')}/v2/lookup/rotector/roblox/user"
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._batch = batch_size

    async def lookup(self, roblox_ids: Sequence[int]) -> dict[int, FlagResult]:
        out: dict[int, FlagResult] = {}
        ids = unique(int(i) for i in roblox_ids)
        for chunk in chunked(ids, self._batch):
            try:
                resp = await self._request("POST", self._url, json_body={"ids": chunk}, headers=self._headers)
            except (NetworkError, HttpError) as e:
                self._log_failure(e)
                for rid in chunk:
                    out[rid] = FlagResult.inconclusive(self.name, f"request failed: {e}")
                continue

            body = resp.body
            if resp.status != 200 or not isinstance(body, dict) or body.get("success") is not True:
                code = body.get("code") if isinstance(body, dict) else None
                err = body.get("error") if isinstance(body, dict) else str(body)[:200]
                detail = f"HTTP {resp.status} code={code!r}: {err}"
                if resp.status in (401, 403):
                    log.error("Rotector rejected our API key / access (%s). Check RAYWARD_API_KEY.", detail)
                else:
                    log.error("Rotector error response: %s", detail)
                for rid in chunk:
                    out[rid] = FlagResult.inconclusive(self.name, detail)
                continue

            data = body.get("data")
            if not isinstance(data, dict):
                for rid in chunk:
                    out[rid] = FlagResult.inconclusive(self.name, "response has no data object")
                continue

            for rid in chunk:
                entry = data.get(str(rid))
                if entry is None:
                    entry = data.get(rid)  # defensive: int keys if the body was not JSON-decoded strictly
                if not isinstance(entry, dict):
                    out[rid] = FlagResult.inconclusive(self.name, "id missing from batch response")
                    continue
                code, name, outcome = map_flag_type(entry.get("flagType"))
                if outcome is FlagOutcome.UNMAPPED:
                    log.error(
                        "ROTECTOR RETURNED AN UNMAPPED flagType=%r for roblox id %s - routing to Inconclusive. "
                        "Update FLAG_TYPES in banbot/flags/rotector.py. Entry: %s",
                        entry.get("flagType"), rid, str(entry)[:500],
                    )
                out[rid] = FlagResult(outcome, self.name, code, name, entry, summarize_entry(entry))
        return out

    @staticmethod
    def _log_failure(e: Exception) -> None:
        if isinstance(e, HttpError) and e.status in (401, 403):
            log.error("Rotector auth/access failure: %s. Check RAYWARD_API_KEY / account status.", e)
        elif isinstance(e, HttpError) and e.status == 429:
            log.warning("Rotector rate limit still exceeded after retries: %s", e)
        elif isinstance(e, HttpError) and e.status == 503:
            log.warning("Rotector source did not answer (503). Not clean, will retry: %s", e)
        else:
            log.warning("Rotector request failed: %s", e)
