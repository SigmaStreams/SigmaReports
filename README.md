# Discord Reports Bot

A Discord bot for handling TV and VOD issue reports using slash commands, modals, and staff workflows.

## Features
- `/report-tv` and `/report-vod` slash commands
- Modal-based report submission
- Staff review buttons:
  - Fixed
  - Can't replicate
  - More info required
  - Send follow-up
- Public + DM updates to reporters
- Configurable staff pings with on/off toggle
- Multi-channel support for reporting and testing
- Dockerized deployment
- SQLite persistence
- **Dynamic “Watching” bot status (IPTV + TMDB)**

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

### Python version
- Local runs should use **Python 3.12.x**.
- The current `discord.py==2.4.0` dependency imports `audioop`, which is removed in Python 3.13 and causes the bot to crash during startup.
- Docker is already pinned to Python 3.12 in the existing Dockerfile.

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
