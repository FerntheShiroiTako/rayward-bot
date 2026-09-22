"""`python -m banbot` - load .env, validate global config, run the bot."""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from . import bandm
from .bot import run
from .config import ConfigError, GlobalConfig
from .crypto import CryptoError, SecretBox


def main() -> int:
    load_dotenv()
    # Windows consoles default to cp1252, which mangles the bullets/emoji in summaries and embeds.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        global_cfg = GlobalConfig.from_env()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        SecretBox(global_cfg.master_key)
    except CryptoError as e:
        print(f"config error: {e}", file=sys.stderr)
        print("generate one with: python -m banbot.crypto", file=sys.stderr)
        return 2

    log = logging.getLogger("banbot")
    log.warning(
        "banbot is multi-server: each server's admins connect their own Rayward/Bloxlink keys and choose "
        "their own safety settings via /setup. There is no single global DRY_RUN/REPORT_ONLY any more."
    )
    ban_dm_default = bandm.load_template(os.environ.get("BAN_DM_FILE", "ban_dm.txt"))
    run(global_cfg, ban_dm_default=ban_dm_default)
    return 0


if __name__ == "__main__":
    sys.exit(main())
