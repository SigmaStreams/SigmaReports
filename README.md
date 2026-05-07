# SigmaReports

SigmaReports is a Discord bot for collecting Live TV and VOD issue reports, routing them to staff, and tracking them through resolution.

It is built around Discord modals, persistent button views, a staff review workflow, optional private ticket channels, and SQLite-backed report storage.

## What The Bot Actually Does

### User-facing reporting
- Posts a persistent report panel with separate buttons for Live TV and Movies / TV Shows.
- Supports `/report-tv` as a manual fallback for Live TV reports.
- Supports `/report-vod` for Movies / TV Shows reports.
- Restricts slash-command reporting to configured report channels.
- Blocks users from reporting if staff has placed a report-system block on them.

### Live TV flow
- Preferred path is the panel-based Live TV flow.
- If IPTV selector data is available, users can:
  - search channels globally
  - browse IPTV categories
  - pick a channel first, then choose a common issue
- Common issue shortcuts include offline, buffering, wrong content, no audio, black screen / no video, audio sync, guide / EPG, and an "other" fallback.
- Some issues use a short follow-up selector to reduce typing.
- If IPTV selector data is not deployed, the Live TV flow still works through manual entry.

### VOD flow
- Uses a guided questionnaire instead of a single freeform form.
- Collects whether the title was requested through the Requests Bot.
- Collects language, 4K status, and whether the title is a movie or TV show.
- Validates reference links for:
  - TMDB movie links for movies
  - TheTVDB series links for TV shows
- Finishes with issue details and sends the report to staff.

### Staff workflow
- Sends each new report to the configured staff channel.
- Adds persistent staff action buttons to report messages:
  - `Resolved`
  - `Not Resolved`
  - `Open ticket`
- Supports opening a private ticket channel for staff plus the reporter.
- Lets staff resolve or close reports directly from either the staff report message or the ticket channel.
- Sends best-effort DM updates to the reporter.
- Can also post public response updates in a configured responses channel.
- Can generate and send plain-text ticket transcripts when tickets are closed, if a transcripts channel is configured.

### Staff/admin tools
- `/reportpanel` posts the user-facing report panel.
- `/liveboardstart`, `/liveboardrefresh`, and `/liveboardstop` manage an auto-updating board of active reports.
- `/list-open-reports` lists currently open reports.
- `/close-open-reports` bulk-closes all open reports in a server.
- `/editreport` reassigns a report to a different reporter.
- `/reportreactivate` reopens a previously closed report and restores its staff buttons.
- `/reportblock`, `/reportunblock`, and `/reportblocks` manage report-system blocks.
- `/reportpings` toggles new-report staff pings for the configured owner account.
- `/synccommands` force-syncs slash commands for the current server for the configured owner account.

### Presence
- Rotates a `Watching ...` presence every 5 minutes.
- Uses built-in TV/IPTV-themed phrases and a built-in local channel list.
- Optionally mixes in trending movie and TV titles from TMDB.
- Does not depend on the IPTV JSON datasets.

## Requirements

- Python 3.12.x for local runs
- Docker is already pinned to Python 3.12
- `discord.py==2.4.0` is not compatible with Python 3.13 because Python 3.13 removed `audioop`

### Create a local venv

Use any Python 3.12 interpreter available on your machine.

On many systems, one of these will work:

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

If `python3.12` is not available but your default `python3` is already Python 3.12, use:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

You can confirm the interpreter version with:

```bash
python3.12 --version
```

or:

```bash
python3 --version
```

If you already have a `.venv` created with the wrong Python version, remove it and recreate it with Python 3.12.

## Configuration

Copy `.env.example` to `.env` and fill in your values.

Required settings:
- `DISCORD_TOKEN`
- `STAFF_CHANNEL_ID`
- `STAFF_ROLE_ID`
- `REPORTS_CHANNEL_IDS` or legacy `REPORTS_CHANNEL_ID`

Required only when `PUBLIC_UPDATES=true`:
- `RESPONSES_CHANNEL_ID`

Optional settings:
- `SUPPORT_CHANNEL_ID`
- `MODLOGS_CHANNEL_ID`
- `TRANSCRIPTS_CHANNEL_ID`
- `DB_PATH`
- `TMDB_BEARER_TOKEN`
- `STAFF_PING_USER_IDS`
- `TV_STAFF_PING_USER_IDS`
- `VOD_STAFF_PING_USER_IDS`
- `PUBLIC_UPDATES`

Notes:
- Split TV and VOD ping lists fall back to `STAFF_PING_USER_IDS` if the split lists are empty.
- Runtime data is stored under `./data` by default.
- Do not commit `.env`.

## Running The Bot

### Local

```bash
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m bot.main
```

### Docker

```bash
docker compose up -d --build
```

## IPTV Datasets

The IPTV datasets are optional deployment assets used only to improve the Live TV report experience.

If they are present, users can search and browse IPTV categories/channels from the report panel.

If you configure multiple providers in a local `providers.json`, the Live TV panel will prompt the user to choose a provider first. If exactly one provider is enabled, the flow skips that extra prompt and behaves like the current single-provider flow.

If they are absent, unreadable, or invalid, the bot falls back to manual Live TV entry instead of breaking.

Files:
- `data/iptv_channels.json` is the raw parsed M3U export.
- `data/iptv_channels_selector.json` is the selector-friendly dataset used by the panel flow.

Optional multi-provider setup:
- copy `providers.example.json` to `providers.json`
- add one entry per provider
- point each provider at its own M3U source, raw export, and selector dataset paths
- `providers.json` is ignored by git so deployments can keep provider-specific local paths

Rebuild them with:

```bash
./.venv/bin/python scripts/build_iptv_json.py
./.venv/bin/python scripts/build_iptv_selector_json.py
```

The raw export must be built first, then the selector dataset.

To rebuild assets for a specific configured provider instead, use:

```bash
./.venv/bin/python scripts/build_iptv_json.py --provider provider_a
./.venv/bin/python scripts/build_iptv_selector_json.py --provider provider_a
```

### Rebuild without a local venv

If you prefer to run the dataset scripts inside Docker instead of creating a local Python environment, use:

```bash
docker compose run --rm --volume "$PWD:/app" --workdir /app reports-bot python scripts/build_iptv_json.py
docker compose run --rm --volume "$PWD:/app" --workdir /app reports-bot python scripts/build_iptv_selector_json.py
```

That bind-mounts the full repo into the container so the scripts, playlist file, and `data/` output directory are all available.

### Bring Your Own M3U

If your playlist file is named `channels.m3u` and lives in the repo root, you can build the datasets locally with:

```bash
./.venv/bin/python scripts/build_iptv_json.py
./.venv/bin/python scripts/build_iptv_selector_json.py
```

If you want to do the same thing inside Docker instead:

```bash
docker compose run --rm --volume "$PWD:/app" --workdir /app reports-bot python scripts/build_iptv_json.py
docker compose run --rm --volume "$PWD:/app" --workdir /app reports-bot python scripts/build_iptv_selector_json.py
```

If the playlist lives elsewhere, build it locally with:

```bash
./.venv/bin/python scripts/build_iptv_json.py --input /path/to/playlist.m3u --output data/iptv_channels.json
./.venv/bin/python scripts/build_iptv_selector_json.py --input data/iptv_channels.json --output data/iptv_channels_selector.json
```

For multiple providers, a common layout is:

```text
data/providers/provider_a/iptv_channels.json
data/providers/provider_a/iptv_channels_selector.json
data/providers/provider_b/iptv_channels.json
data/providers/provider_b/iptv_channels_selector.json
```

Or inside Docker with:

```bash
docker compose run --rm --volume "$PWD:/app" --workdir /app reports-bot python scripts/build_iptv_json.py --input /app/path/to/playlist.m3u --output data/iptv_channels.json
docker compose run --rm --volume "$PWD:/app" --workdir /app reports-bot python scripts/build_iptv_selector_json.py --input data/iptv_channels.json --output data/iptv_channels_selector.json
```

## Command Summary

User/reporting commands:
- `/report-tv`
- `/report-vod`

Staff commands:
- `/reportpanel`
- `/liveboardstart`
- `/liveboardrefresh`
- `/liveboardstop`
- `/list-open-reports`
- `/close-open-reports`
- `/editreport`
- `/reportreactivate`
- `/reportblock`
- `/reportunblock`
- `/reportblocks`

Owner-only commands:
- `/reportpings`
- `/synccommands`

## Current Workflow Notes

- The panel flow is the primary user experience for Live TV reporting.
- `/report-tv` is intentionally documented as a manual fallback when the panel is unavailable.
- The liveboard tracks active reports and removes closed ones.
- Ticket creation is optional and happens from the staff side.
- The bot syncs commands to a single guild during startup for faster iteration.
