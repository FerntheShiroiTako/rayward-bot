# banbot

Discord bot that works out each member's Roblox account (from **Bloxlink**, falling back to a
`nickname (@robloxusername)` server nickname), checks it against **Rotector** flag data through the **Rayward**
API, and puts anything worth a look in front of a moderator.

**The bot never bans anyone on its own.** Every flagged account is posted to the mod channel with
**Ban** / **Dismiss** buttons, and a ban happens only when a mod presses Ban.

**banbot works in any server.** One bot process can moderate many Discord servers at once. Each server's own
admins connect their own Rayward/Bloxlink API keys and choose their own safety settings through `/setup`, so
nobody has to touch the bot's `.env` or restart the process just to add a server.

Safety rules baked in:

- **Inconclusive is never clean.** Any API error, timeout, missing entry or unknown status goes to a retry bucket,
  never to "no action". A Bloxlink outage cannot make a member look unflagged.
- **No automatic bans.** "Confirmed requires review" is on for every server by default; the auto-ban path exists
  behind that switch and still requires a live identity re-check if a server ever turns it off.
- **Every ban gets an audit record** (Discord ID, Roblox ID, nickname at ban time, raw provider response,
  timestamp, decision path, approving mod).
- **Idempotent**: re-running a sweep never double-bans, double-posts or double-logs.
- A **report-only** mode is also available per server: detections are posted as plain notices with no buttons.
- Every new server starts in **dry run** (no real bans) until an admin turns it off in `/setup` or `/config`.

## Running the bot (once, for whoever hosts it)

On Windows, `run-bot.bat` creates the virtual environment, installs dependencies on first run, and starts the
bot (it refuses to start if `.env` is missing).

```
copy .env.example .env
```

Manual setup, if you prefer:

```bash
python -m venv .venv && .venv\Scripts\activate    # or source .venv/bin/activate
pip install -e .
copy .env.example .env                              # then fill it in
python -m banbot
```

`.env` only holds **process-wide** settings now; there's no server-specific config in it at all.

| Key | Purpose |
|---|---|
| `DISCORD_TOKEN` | The bot's Discord application token |
| `MASTER_KEY` | Encrypts every server's Rayward/Bloxlink keys before they're written to SQLite. Generate one with `python -m banbot.crypto` and keep it secret; losing it means every server has to run `/setup` again. |
| `DEV_GUILD_ID` | Optional: also instant-sync slash commands to this guild (global sync can take up to ~1h to reach every server) |
| `RAYWARD_BASE_URL`, `ROBLOX_BASE_URL`, `BLOXLINK_BASE_URL` | API hosts, shared by every server |
| `BLOXLINK_MIN_INTERVAL_S`, `BLOXLINK_DAILY_LIMIT`, `BLOXLINK_DAILY_RESERVE` | Bloxlink pacing and its 2,000 requests/UTC-day quota (tracked separately per server's own key) |
| `SWEEP_BATCH_SIZE`, `ROBLOX_BATCH_SIZE`, `ROTECTOR_BATCH_SIZE` | Batch sizes (caps 200 / 100, verified) |
| `*_MIN_INTERVAL_S`, `HTTP_*`, `SWEEP_CHUNK_DELAY_S`, `BAN_DELAY_S` | Rate limiting and 429/5xx backoff |
| `RETRY_MAX_RETRIES`, `RETRY_BASE_DELAY_S`, `RETRY_MAX_DELAY_S`, `RETRY_POLL_INTERVAL_S` | Inconclusive-bucket retries (exponential backoff) |
| `BAN_DM_FILE` | Default ban-DM text for servers that haven't set their own via `/config` |
| `DB_PATH` | SQLite file (default `data/banbot.sqlite3`), shared by every server and isolated internally by guild id |
| `RAW_RETENTION_HOURS` | After this, stored raw provider JSON is reduced to a minimal summary (Rotector terms: 24 h) |

**Discord developer portal, once:** enable the **Server Members Intent** under Bot → Privileged Gateway
Intents. It's required; without it the bot can't see who's actually in a server to check them. Then generate
an invite URL with the `bot` + `applications.commands` scopes and these permissions:

| Permission | What it's for |
|---|---|
| **Ban Members** | the only permission actually used to remove anyone, and it's server-wide, not per-channel |
| **View Channel**, **Send Messages**, **Embed Links** | posting review cases and summaries, needed wherever a server points the bot: the mod channel, the summary channel, and the detection log forum if one's set. Easiest is granting all three on the bot's role server-wide, so whichever channels get picked later are already covered |

Anyone can then add the bot to their own server with that invite link. No code change or restart needed per
server. One thing Discord doesn't expose as an invite permission: the bot's own role has to sit **above** the
mod role (and generally above anyone it might need to ban) in Role settings, or the ban itself will fail even
with Ban Members granted. `/setup`'s **Test & Finish** checks all of this, role position included, before
marking a server ready, and `/config` can re-run the same check any time.

## Adding banbot to a server (per server, by that server's own admins)

1. Invite the bot using the link above.
2. Run **`/setup`** (requires the **Manage Server** permission; see **Who can run /setup and /config** below
   to delegate this without handing that permission out). It opens a panel where you:
   - paste your own **Rayward** key (from <https://rayward.app/signin>), required;
   - optionally paste a **Bloxlink** server key (from <https://blox.link/dashboard/developers>, scoped to
     this server) - leave it unset and the bot falls back to nickname parsing alone;
   - pick the **mod role** (who can press Ban/Dismiss) and **mod channel** (where cases are posted);
   - open **More Settings** for the less-common options: a **summary channel**, specific **trigger users**,
     and an optional **detection log forum**. Pick a Discord *forum* channel there and every detection gets
     its own thread, a permanent searchable archive kept separate from the mod queue;
   - leave **Dry run** and **Confirmed requires review** on unless you've read what they do (below).
3. Click **Test & Finish**. It makes a real (harmless) call with each key, checks the bot's permissions and
   role position in your server, and only marks setup complete once everything passes.
4. Re-open the same panel any time with **`/config`** to change anything: keys, roles, channels, or the
   safety switches, without redoing the whole wizard. Make sure the bot's own role sits **above** the mod
   role, or it won't be able to ban anyone with it.

### Who can run /setup and /config

By default that's anyone with Discord's own **Manage Server** permission. To delegate it without handing
that out, an admin (Manage Server, or Administrator) opens **Access Control** in the panel and sets:

- **Master role**: full access. Every setting, including pasting or replacing the two API keys, and who
  holds this role or the Configurator role. Basically Manage Server, just scoped to this bot.
- **Configurator role**: access to everything *except* the API keys. Mod role/channel, trigger role/users,
  the detection log forum, every safety-mode toggle. Someone with only this role sees no more than
  "Set"/"Not set" for a key, and has no way to view, replace or clear it.

Holding either role is enough on its own (no Manage Server needed); a member with both is treated as Master.
Leaving both unset (the default) means only Manage Server/Administrator can configure the bot, same as
before this existed.

## How a check works

**1. Identify the Roblox account** ([banbot/identity.py](banbot/identity.py)), in priority order:

| Source | How | Result |
|---|---|---|
| **Bloxlink** | `GET api.blox.link/v4/public/guilds/{guild}/discord-to-roblox/{user}` | The member verified this account, so it wins outright. Gives a Roblox ID; usernames are then fetched in bulk via `POST users.roblox.com/v1/users` (max 200/request). |
| **Nickname** | trailing `(@username)`, validated against Roblox username rules (3–20 chars, letters/digits, at most one `_`, not first/last) | Used only when Bloxlink has no link. Resolved to an ID via `POST /v1/usernames/users` (max 200/request). |

The distinction that matters: Bloxlink saying **"this member never verified"** is a real answer, so the
nickname is tried next, and if that fails too, the member is *Unresolved* (logged, no action). Bloxlink
**failing** (network error, bad key, rate limit, or a response shape the client can't read) is *not* an
answer, so it becomes *Inconclusive* and gets retried. A member is never skipped because a lookup broke, and
a Bloxlink outage never silently falls through to a nickname a bad actor controls.

Which source was used is stored on the case and shown to mods in the embed, so an unverified nickname match is
never mistaken for a verified link.

**Bloxlink's daily quota.** Every lookup is counted in SQLite against the UTC day (so a restart can't reset the
count). A sweep may spend down to `BLOXLINK_DAILY_RESERVE` and no further; join checks and pre-ban re-checks may
use the reserve. When a sweep can't pay for its next chunk it **pauses until the UTC reset** and tells the mod
channel; members are never failed into the retry bucket just because the bot ran out of quota. If a join check
lands after the quota is gone, the member is parked until the reset without using up one of their retry attempts.
At sweep start, if the member count exceeds today's remaining budget, the bot posts how many days the sweep will
take. `/sweep status` shows the live count.

**2. Look the account up on Rotector** (`POST /v2/lookup/rotector/roblox/user`, batched, max 100 ids, using
*this server's own* Rayward key). The whole status mapping is the `FLAG_TYPES` table in
[banbot/flags/rotector.py](banbot/flags/rotector.py):

| Rotector `flagType` | As shipped | With report-only mode on |
|---|---|---|
| 0 Unflagged | Clear – no action | Nothing (counted as Clear) |
| 2 Confirmed | **Review queue, with buttons** | Posted to mod channel |
| 1 Flagged, 5 Mixed, 3 Queued, 4 Provisional Flag, 8 Redacted | **Review queue, with buttons** | Posted to mod channel |
| 6 Past Offender | Allowed, logged only | Posted to mod channel (informational) |
| error / timeout / 503 / missing / anything else | Inconclusive → retried → review queue | Inconclusive → retried → posted if still unverified |

Unknown `flagType` values are logged at ERROR and treated as inconclusive, never as clean.

**3. A mod presses Ban** → the member is DMed, then banned, then an audit record is written. Nothing else bans.

The same pipeline runs for every member on join, in every server the bot has completed `/setup` in.

### If a server turns auto-ban on ("Confirmed requires review" off, in `/config`)

`Confirmed` accounts then ban without a button press, but only after the identity is **re-checked live,
immediately before the ban**, in a way that matches how it was established:

- **Nickname-sourced** → re-fetch the live nickname; it must still carry the same `(@username)`.
- **Bloxlink-sourced** → re-query Bloxlink; it must still return the same Roblox ID.

Changed, link removed, member left, or the re-check itself failed → review queue or inconclusive. Never a ban.

### Ban evasion

Discord bots have no access to IP addresses at all. That's Discord's own Trust & Safety territory, not
something any bot can see. What banbot *can* do, and does automatically: if a member's resolved Roblox
account was already banned in this server under a **different** Discord account, that counts as a
Confirmed-equivalent hit and goes through the exact same gate as any other Confirmed detection. With
`Confirmed requires review` on (the default) it lands in the review queue with the reason "Ban evasion" and
the prior account named; off, it goes through the same live re-check and ban as any other auto-ban. The
Rotector flag lookup isn't even consulted for these; a returning banned account is already reason enough.
This runs on every join and every sweep, for every server, with no extra setup.

## Running the sweep

`/sweep start` runs it, restricted to the trigger role/user(s) chosen in `/setup`. It walks every non-bot
member in ascending ID order in chunks, persists its cursor after each chunk, and only one sweep can be
active per server at a time. If the bot crashes or is restarted, every server's in-progress sweep resumes
automatically on startup (or via `/sweep resume`); members already processed are skipped.

After the main pass the sweep waits for its inconclusive members to be retried (exponential backoff, up to
`RETRY_MAX_RETRIES`). Members that still can't be verified are **escalated to the review queue** and counted
as inconclusive. Then a summary is posted to the summary channel and, unless turned off in `/config`, DMed to
whoever ran `/sweep start`:

```
Sweep #3 complete - 1234 members, 14m07s
• Unresolved (no Bloxlink link and no valid (@username) tag): 87
• Inconclusive (retries exhausted, escalated to review): 2
• Clear: 1120
• Past offender (allowed, logged): 4
• Queued for review: 21
• Banned: 0
```

`Banned` counts bans the bot issued by itself, which is always 0 as shipped; bans from Ban-button presses
land in the audit log, not in the sweep that queued them.

The header states completion only. Which mode the sweep ran in is visible from the count lines themselves:
`Banned` for real bans, `Would ban (dry-run)` under dry run, `Reported to mods` under report-only.

Other commands: `/sweep status`, `/sweep resume`, `/sweep abort`, `/check @member` (run the pipeline for one
member), `/reviews` (paginated list of *open* cases, 10 per page with Prev/Next), `/detections` (every
detection ever recorded, any status - see below), `/help` (command summary).

## Detection log (optional forum archive)

Set a forum channel under `/config` → **More Settings** → **Detection log forum**, and every detection that
would go to the review queue or a report also gets its own thread there (title: `#<id> · <roblox username> ·
<status>`), posted once, when first detected. It's a permanent, searchable log rather than another
actionable queue, so it's never edited or removed afterward, even once the case is resolved in the mod
channel. Leave it unset to skip this entirely; nothing changes about how cases are handled.

For a full export instead of a live archive, `/detections` covers every row ever written to the review queue
for the server (pending, approved, denied, and reported) in two forms, picked with its `format` option:

- **CSV file** (default) — one row per detection: when, the Discord and Roblox accounts, status, reason and
  identification method in the same plain-English wording mods see in the review embeds, queue status, and
  who resolved it and when. Opens straight into a spreadsheet.
- **Username list** — a quick paginated embed of just the distinct Roblox usernames that have ever been
  detected (deduplicated, alphabetical), for a fast "who's in here" look without downloading anything.

## Review queue

Cases are posted to the mod channel as an embed (member, Roblox account, status, why it's here, how the
account was linked, and any provider detail) with **Ban** / **Dismiss** buttons.

- Only members with the mod role can press the buttons. The role is checked server-side at click time, so
  editing the message or replaying the interaction gets you nothing; everyone else gets an ephemeral "you
  don't have permission to do that".
- **Ban** → the member is DMed (see below), then banned, then an audit record is written naming the approving mod.
- **Dismiss** → logged (who, when), no action.
- Once resolved, the embed title changes to **"Resolved · Case #N"**, the outcome line is appended (who
  acted and what happened), and the buttons are removed, so a case a mod has already looked at can't be
  double-clicked or mistaken for one still awaiting a decision.
- One pending case per member + Roblox ID, so a re-run never duplicates. Cases live in SQLite and the buttons keep
  working across restarts (persistent `DynamicItem` handlers); anything that failed to reach Discord is re-posted
  on the next startup.

Every case reaches this queue, `Confirmed` included. That's the point of "confirmed requires review" being
on by default, and it matches Rotector's own terms, which note that flags "should be reviewed by a human
before you act on them".

## Ban DM

Right before a member is banned (auto-ban or a mod pressing **Ban**), the bot DMs them. Each server can set its
own text in `/config` (the **Safety Modes** panel also has a **Ban DM** on/off toggle); a server that hasn't set
its own falls back to `BAN_DM_FILE` (default `ban_dm.txt`, in the project root — edited directly, no restart
needed) and then to a built-in default.

Placeholders, filled in per ban:

| Placeholder | Value |
|---|---|
| `{server}` | The guild's name |
| `{roblox_username}` | The Roblox username the ban is based on |
| `{roblox_id}` | The Roblox user ID |
| `{status}` | Rotector's status for the account, e.g. `Confirmed` |

Turn the **Ban DM** toggle off in `/config` to send nothing. A DM that fails (the member has DMs closed,
blocked the bot, or already left) never blocks or delays the ban; the attempt and outcome get recorded on the
audit row (`dm_sent`, `dm_error`) either way. In dry-run mode no DM is sent, since no ban happens.

## Turning bans off again

Each server controls its own safety switches from `/config` → **Safety Modes** (turning off *Dry run* or
*Confirmed requires review* asks for a second confirmation, since those are what stop the bot from banning
people by mistake):

- **Dry run ON** (the default for every new server): the full pipeline still runs and cases still get
  buttons, but every ban (automatic or approved) becomes a "would ban" log line plus an audit row. Inspect
  them with `sqlite3 data/banbot.sqlite3 "select * from audit_log where guild_id = <your server id>"`.
- **Report only ON**: no bans, no buttons. Detections are posted as plain notices for mods to act on
  manually. One notice per member + Roblox ID + status; a re-run bumps `seen_count` rather than re-posting,
  and a status change (e.g. `Flagged` → `Confirmed`) posts a fresh notice.

Changes from `/config` take effect immediately, no restart needed. `/config` also shows the server's current
settings at a glance, and `/setup`'s **Test & Finish** button (also reachable from `/config`) re-checks both
API keys and the bot's Discord permissions on demand.

## Upgrading

The bot's schema is defined fresh in `store.py`'s `SCHEMA`; on startup any table/index that doesn't exist yet is
created (`CREATE TABLE IF NOT EXISTS`). Just restart. An in-progress sweep resumes from where it stopped, in
every server, automatically.
