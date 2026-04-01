import asyncio
import random
from typing import Optional

import discord
from discord.ext import commands

from bot.config import load_config
from bot.db import ReportDB
from bot.views import ReportActionView, TicketResolveView

try:
    import aiohttp
except Exception:
    aiohttp = None  # type: ignore


IPTV_FLAVOR = [
    "IPTV playlists",
    "Live TV",
    "EPG updates",
    "Channel scans",
    "Buffering fixes",
    "Stream health",
    "CDN routes",
    "Catch-up TV",
]

LOCAL_CHANNELS = [
    "BBC One",
    "BBC Two",
    "ITV 1",
    "Channel 4",
    "Sky Sports News",
    "Sky Sports Main Event",
    "TNT Sports 1",
    "Eurosport 1",
    "Discovery",
    "National Geographic",
]


DEFAULT_GUILD_ID_FOR_SYNC = 1457559352717086917


class SigmaReportsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.cfg = load_config()
        self.db = ReportDB(self.cfg.db_path)

        self._tmdb_cache: list[str] = []
        self._presence_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        # Persistent views
        self.add_view(
            ReportActionView(
                self.db,
                self.cfg.staff_channel_id,
                self.cfg.support_channel_id,
                self.cfg.public_updates,
                self.cfg.staff_role_id,
            )
        )
        self.add_view(
            TicketResolveView(
                self.db,
                self.cfg.staff_channel_id,
                self.cfg.support_channel_id,
                self.cfg.public_updates,
                self.cfg.staff_role_id,
            )
        )

        # Load cogs
        await self.load_extension("bot.cogs.reports")

        for ext in (
            "bot.cogs.moderation",
            "bot.cogs.panel",
            "bot.cogs.liveboard",
            "bot.cogs.plex_liveboard",
        ):
            try:
                await self.load_extension(ext)
            except Exception as e:
                print(f"⚠️  Skipping {ext}: {repr(e)}")

        # Sync to a single guild for fast iteration
        guild_id = getattr(self.cfg, "guild_id", None) or DEFAULT_GUILD_ID_FOR_SYNC
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"Synced {len(synced)} commands to guild {guild_id}")
            except Exception as e:
                print(f"⚠️  Command sync failed: {repr(e)}")
        else:
            print("⚠️  No guild_id configured; skipping guild sync.")

        self._presence_task = asyncio.create_task(self._presence_rotator())

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")

    async def _presence_rotator(self):
        await self.wait_until_ready()

        try:
            await self._refresh_tmdb_cache()
        except Exception as e:
            print("Presence: TMDB refresh failed:", repr(e))

        ticks = 0
        while not self.is_closed():
            try:
                await self._set_random_presence()
            except Exception as e:
                print("Presence: change_presence failed:", repr(e))

            await asyncio.sleep(300)
            ticks += 1

            if ticks % 72 == 0:
                try:
                    await self._refresh_tmdb_cache()
                except Exception as e:
                    print("Presence: TMDB refresh failed:", repr(e))

    def _build_status_pool(self) -> list[str]:
        pool: list[str] = []
        pool.extend(IPTV_FLAVOR)
        pool.extend(LOCAL_CHANNELS)
        pool.extend(self._tmdb_cache)
        return [p for p in pool if p]

    async def _set_random_presence(self):
        pool = self._build_status_pool()
        if not pool:
            print("Presence: status pool empty (nothing to display).")
            return

        choice = random.choice(pool)
        activity = discord.Activity(type=discord.ActivityType.watching, name=choice)
        await self.change_presence(status=discord.Status.online, activity=activity)
        print(f"Presence set: Watching {choice}")

    async def _refresh_tmdb_cache(self):
        token = getattr(self.cfg, "tmdb_bearer_token", "") or ""
        if not token or aiohttp is None:
            self._tmdb_cache = []
            return

        headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
        urls = [
            "https://api.themoviedb.org/3/trending/movie/day",
            "https://api.themoviedb.org/3/trending/tv/day",
        ]

        titles: list[str] = []
        async with aiohttp.ClientSession(headers=headers) as session:
            for url in urls:
                try:
                    async with session.get(url, timeout=15) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for item in data.get("results", [])[:25]:
                            name = item.get("title") or item.get("name")
                            if name:
                                titles.append(name)
                except Exception:
                    continue

        deduped = []
        seen = set()
        for t in titles:
            k = t.lower()
            if k in seen:
                continue
            seen.add(k)
            deduped.append(t)

        self._tmdb_cache = deduped[:50]
        print(f"Presence: refreshed TMDB cache ({len(self._tmdb_cache)} titles).")


def main():
    bot = SigmaReportsBot()
    bot.run(bot.cfg.token)


if __name__ == "__main__":
    main()
