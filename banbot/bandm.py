"""The message a member receives by DM immediately before they are banned.

The text lives in a plain file (BAN_DM_FILE, default ban_dm.txt) so it can be edited without touching
code. Placeholders in curly braces are filled in per ban; an unknown placeholder is left as-is rather
than crashing the ban. An empty or missing file disables the DM.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def load_template(path: str | Path | None) -> str | None:
    """Read the DM template. Returns None (DM disabled) if the path is unset, missing or blank."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        log.warning("BAN_DM_FILE %s does not exist; banned members will not be messaged", p)
        return None
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        log.warning("BAN_DM_FILE %s is empty; banned members will not be messaged", p)
        return None
    return text


def render(template: str, **values: object) -> str:
    return template.format_map(_Safe({k: "" if v is None else v for k, v in values.items()}))[:2000]
