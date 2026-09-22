"""Step 1: parse `nickname (@robloxusername)` and validate the Roblox username.

Roblox username rules (verified): 3-20 characters, letters/digits/underscore,
at most one underscore, which may not be the first or last character.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# One or more alphanumerics, optionally a single underscore followed by more alphanumerics.
_USERNAME_SHAPE_RE = re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?$")
USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 20

# Trailing "(@username)" group at the very end of the nickname. Anchored at the end so
# parentheses/emoji in the display part don't matter; lenient about whitespace.
_TRAILING_TAG_RE = re.compile(r"\(\s*@\s*(?P<user>[^()]*?)\s*\)\s*$")


class ParseFailure(str, Enum):
    NO_NICKNAME = "no_nickname"
    NO_MATCH = "no_match"
    INVALID_USERNAME = "invalid_username"


@dataclass(frozen=True)
class NicknameParse:
    raw: str | None
    username: str | None = None
    failure: ParseFailure | None = None

    @property
    def ok(self) -> bool:
        return self.username is not None


def is_valid_roblox_username(candidate: str) -> bool:
    if not (USERNAME_MIN_LEN <= len(candidate) <= USERNAME_MAX_LEN):
        return False
    return _USERNAME_SHAPE_RE.match(candidate) is not None


def parse_nickname(nickname: str | None) -> NicknameParse:
    if nickname is None or nickname.strip() == "":
        return NicknameParse(raw=nickname, failure=ParseFailure.NO_NICKNAME)
    m = _TRAILING_TAG_RE.search(nickname)
    if m is None:
        return NicknameParse(raw=nickname, failure=ParseFailure.NO_MATCH)
    candidate = m.group("user")
    if not is_valid_roblox_username(candidate):
        return NicknameParse(raw=nickname, failure=ParseFailure.INVALID_USERNAME)
    return NicknameParse(raw=nickname, username=candidate)


def same_username(a: str | None, b: str | None) -> bool:
    """Roblox usernames are case-insensitive."""
    return a is not None and b is not None and a.lower() == b.lower()
