# Discord Reports Bot

A Discord bot for handling IPTV, live TV, movie, and TV show issue reports with structured intake, staff workflows, and persistent tracking.

## Features

### Report Intake
- Slash commands for both report types:
  - `/report-tv` for live TV and channel issues
  - `/report-vod` for movie and TV show issues
- Button-based report panel via `/reportpanel` for users who should not rely on slash commands
- Modal-driven submissions that collect the right fields for each report type
- Channel restrictions so reports can only be submitted in approved channels
- Automatic blocking checks to stop blocked users from opening new reports

### Staff Workflow
- Every report is stored in SQLite and mirrored to a staff review channel
- Staff action buttons on each report:
  - `Resolved`
  - `Not Resolved`
  - `Open ticket`
- Staff can reassign the reporter on an existing case with `/editreport`
- Optional per-report-type staff pings for TV and VOD reports
- Owner-only toggle for staff pings with `/reportpings`
- Owner-only command sync helper with `/synccommands`

### Tickets, Updates, and Follow-up
- Staff can open a private troubleshooting ticket channel tied to a report
- Ticket channels include resolution controls so staff can close the case from inside the ticket
- Reporters can receive updates by DM
- Optional public responses channel for status updates
- Ticket transcripts can be posted to a transcripts channel and sent to the reporter when a case is closed

### Moderation and Access Control
- Staff-only moderation commands for the reporting system:
  - `/reportblock`
  - `/reportunblock`
  - `/reportblocks`
- Temporary or permanent blocks with optional reasons
- Modlog support for block and unblock actions
- Support-channel appeal messaging for blocked users

### Liveboards
- `/liveboardstart`, `/liveboardrefresh`, and `/liveboardstop` manage an active report board
- The liveboard separates active Live TV and VOD reports and removes closed items automatically
- `/plexliveboardstart`, `/plexliveboardrefresh`, and `/plexliveboardstop` manage a Plex status board
- Plex statuses update automatically from webhook log messages and track multiple servers

### Presence and Reliability
- Rotating `Watching ...` bot presence themed around IPTV, channels, and trending media
- TMDB-backed status titles with safe fallback to local titles if TMDB is unavailable
- Persistent Discord views so buttons keep working across bot restarts
- Dockerized deployment for straightforward hosting
- SQLite persistence for reports, tickets, liveboards, and moderation state

## Bot Presence (Watching Status)

The bot displays a rotating **“Watching …”** status themed around IPTV, live TV, and popular shows/movies.

### How it works
- Status updates every **5 minutes**
- Titles are chosen from:
  - Local IPTV / TV channel names
  - IPTV-themed phrases
  - Trending TV shows and movies from **TMDB**
- TMDB titles are refreshed every **6 hours**
- If the TMDB token is missing or unavailable, the bot safely falls back to local lists only

### Example statuses
- Watching BBC One
- Watching Sky Sports News
- Watching Breaking Bad
- Watching Interstellar
- Watching IPTV playlists

### TMDB configuration
To enable TMDB-powered titles, add this to your `.env`:

TMDB_BEARER_TOKEN=your_tmdb_read_access_token

This should be the **TMDB API Read Access Token (v4)**.

## Setup

### 1. Clone the repo
```bash 
git clone https://github.com/yourname/discord-reports-bot.git
cd discord-reports-bot
```

### 2. Create environment file
```bash 
cp .env.example .env
```
Fill in your values (Discord token, channel IDs, optional TMDB token).

### 3. Run with Docker
```bash 
docker compose up -d --build
```
## Notes
- Do **not** commit your `.env`
- Runtime data is stored in `./data` via Docker volume
- TMDB integration is optional and non-blocking
