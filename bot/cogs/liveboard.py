import asyncio
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils import report_subject


# Only "Resolved" and "Not Resolved" are considered closed in your current workflow
CLOSED_STATUSES = {"Resolved", "Not Resolved"}


def _is_staff(member: discord.Member, staff_role_id: int) -> bool:
    return any(r.id == staff_role_id for r in member.roles)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _ts(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return f"<t:{int(dt.timestamp())}:R>"


def _normalized_provider(value: object) -> str:
    return str(value or "").strip().lower()


class LiveboardCog(commands.Cog):
    def __init__(self, bot, db, cfg):
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self._lock = asyncio.Lock()
        self.liveboard_loop.start()

    def cog_unload(self):
        self.liveboard_loop.cancel()

    # ----------------------------
    # Internal: build + update
    # ----------------------------

    def _staff_jump_link(self, guild_id: int, staff_message_id: Optional[int]) -> Optional[str]:
        if not staff_message_id or not self.cfg.staff_channel_id:
            return None
        return f"https://discord.com/channels/{guild_id}/{self.cfg.staff_channel_id}/{staff_message_id}"

    def _format_row(self, guild_id: int, r: dict) -> str:
        rid = r.get("id")
        status = (r.get("status") or "Open").strip()

        payload = r.get("payload") or {}
        rtype = (r.get("report_type") or "").strip()
        subject = report_subject(rtype, payload)

        # DB gives created_at as ISO string; parse it
        created_dt = _parse_iso_dt(r.get("created_at"))
        link = self._staff_jump_link(guild_id, r.get("staff_message_id"))

        parts = [f"**#{rid}**", f"`{status}`", subject]
        if created_dt:
            parts.append(_ts(created_dt))
        if link:
            parts.append(f"[staff]({link})")
        return " • ".join(parts)

    def _tv_provider_bucket(self, r: dict) -> str:
        payload = r.get("payload") or {}
        provider_name = _normalized_provider(payload.get("provider_name"))
        provider_id = _normalized_provider(payload.get("provider_id"))

        # Keep SS TV and SS TV+ explicitly separate in the liveboard.
        if provider_name in {"ss tv+", "ss tv plus"} or provider_id in {"ss-tv+", "ss-tv-plus", "sstv+"}:
            return "SS TV+"
        if provider_name == "ss tv" or provider_id == "ss-tv":
            return "SS TV"

        if provider_name:
            return str(payload.get("provider_name")).strip()
        if provider_id:
            return str(payload.get("provider_id")).strip()
        return "Other IPTV"

    def _add_tv_fields(self, embed: discord.Embed, guild_id: int, tv_rows: list[dict]) -> None:
        if not tv_rows:
            embed.add_field(name="📺 IPTV", value="No active IPTV reports.", inline=False)
            return

        buckets: dict[str, list[dict]] = {}
        ordered_bucket_names: list[str] = ["SS TV", "SS TV+"]

        for row in tv_rows:
            bucket = self._tv_provider_bucket(row)
            if bucket not in buckets:
                buckets[bucket] = []
                if bucket not in ordered_bucket_names:
                    ordered_bucket_names.append(bucket)
            buckets[bucket].append(row)

        for bucket_name in ordered_bucket_names:
            rows = buckets.get(bucket_name) or []
            if not rows:
                continue
            lines = [self._format_row(guild_id, r) for r in rows[:20]]
            embed.add_field(name=f"📺 IPTV — {bucket_name}", value="\n".join(lines), inline=False)

    def build_liveboard_embed(self, guild_id: int, tv_rows: list[dict], vod_rows: list[dict]) -> discord.Embed:
        embed = discord.Embed(
            title="📡 Liveboard — Active Reports",
            description=(
                "This board updates automatically.\n"
                "Closed reports are removed.\n\n"
                f"Last update: {_ts(_utcnow())}"
            ),
        )

        if not tv_rows and not vod_rows:
            embed.add_field(
                name="All clear",
                value="No active reports right now.",
                inline=False,
            )
            return embed

        self._add_tv_fields(embed, guild_id, tv_rows)

        if vod_rows:
            lines = [self._format_row(guild_id, r) for r in vod_rows[:20]]
            embed.add_field(name="🎬 VOD (Plex/Emby/Jellyfin)", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🎬 VOD (Plex/Emby/Jellyfin)", value="No active VOD reports.", inline=False)

        return embed

    async def update_liveboard(self, guild_id: int):
        settings = self.db.get_liveboard(guild_id)
        if not settings:
            return

        channel_id = settings["channel_id"]
        message_id = settings["message_id"]

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        # Pull active reports (excluding closed)
        reports = self.db.list_active_reports(guild_id, closed_statuses=CLOSED_STATUSES)

        tv_rows = [r for r in reports if (r.get("report_type") or "").strip().upper() == "TV"]
        vod_rows = [r for r in reports if (r.get("report_type") or "").strip().upper() == "VOD"]

        embed = self.build_liveboard_embed(guild_id, tv_rows, vod_rows)

        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed, view=None)
        except discord.NotFound:
            self.db.clear_liveboard(guild_id)
        except discord.Forbidden:
            pass

    @tasks.loop(minutes=3)
    async def liveboard_loop(self):
        async with self._lock:
            for s in self.db.list_liveboards():
                try:
                    await self.update_liveboard(s["guild_id"])
                except Exception:
                    continue

    @liveboard_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    # ----------------------------
    # Slash commands
    # ----------------------------

    @app_commands.command(
        name="liveboardstart",
        description="Create (or move) the liveboard message to a channel (staff only).",
    )
    @app_commands.describe(channel="Channel to post the liveboard message in")
    async def liveboardstart(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        reports = self.db.list_active_reports(interaction.guild.id, closed_statuses=CLOSED_STATUSES)
        tv_rows = [r for r in reports if (r.get("report_type") or "").strip().upper() == "TV"]
        vod_rows = [r for r in reports if (r.get("report_type") or "").strip().upper() == "VOD"]
        embed = self.build_liveboard_embed(interaction.guild.id, tv_rows, vod_rows)

        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I can’t post in that channel.", ephemeral=True)

        self.db.set_liveboard(interaction.guild.id, channel.id, msg.id)
        await interaction.response.send_message(f"✅ Liveboard started in {channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="liveboardrefresh",
        description="Manually refresh the liveboard right now (staff only).",
    )
    async def liveboardrefresh(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        await interaction.response.send_message("Refreshing…", ephemeral=True)
        await self.update_liveboard(interaction.guild.id)

    @app_commands.command(
        name="liveboardstop",
        description="Stop the liveboard updates (staff only).",
    )
    async def liveboardstop(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        self.db.clear_liveboard(interaction.guild.id)
        await interaction.response.send_message("✅ Liveboard stopped.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(LiveboardCog(bot, bot.db, bot.cfg))
