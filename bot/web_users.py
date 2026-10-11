"""Resolve browser identities even when Discord has not cached the reporter."""
import asyncio
from collections import OrderedDict
import time
from urllib.parse import urlsplit

import aiohttp
import discord


def identity(user, uid):
    name = user.name if user is not None else "User unavailable"
    display_name = getattr(user, "display_name", None) if user is not None else None
    avatar_url = None
    if user is not None:
        avatar_url = str(user.display_avatar.with_size(64).url)
        parsed = urlsplit(avatar_url)
        if parsed.scheme != "https" or parsed.netloc != "cdn.discordapp.com":
            avatar_url = None
    return {"id": str(uid), "username": name,
            "display_name": display_name if isinstance(display_name, str) else name,
            "avatar_url": avatar_url}


class UserIdentities:
    def __init__(self, bot):
        self.bot = bot
        self.cache = OrderedDict()
        self.pending = {}
        self.limit = asyncio.Semaphore(4)

    async def get(self, guild, uid):
        if not uid:
            return None
        uid = int(uid)
        user = guild.get_member(uid) or self.bot.get_user(uid)
        if user is not None:
            return identity(user, uid)
        key = (guild.id, uid)
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1]
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(self.fetch(key))
            self.pending[key] = task
            task.add_done_callback(lambda done: self.pending.pop(key, None))
        return await asyncio.shield(task)

    async def fetch(self, key):
        _, uid = key
        async with self.limit:
            try:
                user = await asyncio.wait_for(self.bot.fetch_user(uid), timeout=10)
            except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
                user = None
        result = identity(user, uid)
        self.cache[key] = (time.monotonic() + (300 if user is not None else 30), result)
        self.cache.move_to_end(key)
        while len(self.cache) > 1000:
            self.cache.popitem(last=False)
        return result

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.clear()
