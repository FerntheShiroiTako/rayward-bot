"""Provider-agnostic flag outcomes. Every provider maps its raw statuses onto exactly these."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, Sequence


class FlagOutcome(str, Enum):
    CONFIRMED = "confirmed"  # -> Step 4 (live nickname re-check, then ban) or review if configured
    REVIEW = "review"  # Flagged / Mixed / anything a human must look at
    PAST_OFFENDER = "past_offender"  # allow, log only
    CLEAR = "clear"  # no action
    INCONCLUSIVE = "inconclusive"  # API error / timeout / missing -> retry, never clean
    UNMAPPED = "unmapped"  # status we do not know -> treated as inconclusive, logged loudly


@dataclass(frozen=True)
class FlagResult:
    outcome: FlagOutcome
    provider: str
    status_code: int | None  # raw provider status (Rotector flagType), None on error
    status_name: str  # human label: "Confirmed", "Mixed", "error", "unmapped(7)"...
    raw: dict[str, Any] | None  # the provider's raw entry for this id, None when we got nothing
    detail: str = ""  # short human summary of the raw response, or the error text

    def raw_json(self) -> str:
        if self.raw is not None:
            return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)
        return json.dumps({"error": self.detail, "provider": self.provider, "status": self.status_name})

    @staticmethod
    def inconclusive(provider: str, detail: str) -> "FlagResult":
        return FlagResult(FlagOutcome.INCONCLUSIVE, provider, None, "error", None, detail)


class FlagProvider(Protocol):
    name: str

    async def lookup(self, roblox_ids: Sequence[int]) -> dict[int, FlagResult]:
        """Must return an entry for every id requested. Errors become INCONCLUSIVE, never CLEAR."""
        ...
