import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks


PLEX_LOGS_CHANNEL_ID = 1475676107960356977

SERVER_LABELS = {
    "OMEGA": {"OMEGA", "SS EAST"},
    "ALPHA": {"ALPHA"},
    "DELTA": {"DELTA"},
}

DEFAULT_STATUS = {
    "OMEGA": "Unknown",
    "ALPHA": "Unknown",
    "DELTA": "Unknown",
}


def _is_staff(member: discord.Member, staff_role_id: int) -> bool:
    return any(r.id == staff_role_id for r in member.roles)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:R>"


def _normalize_server_name(raw: str) -> str | None:
    s = (raw or "").strip().upper()
    for canonical, aliases in SERVER_LABELS.items():
        if s in aliases:
            return canonical
    return None


def _extract_message_text(msg: discord.Message) -> str:
    parts: list[str] = []

    if msg.content:
        parts.append(msg.content)

    for e in msg.embeds:
        if e.title:
            parts.append(e.title)
        if e.description:
            parts.append(e.description)
        for field in e.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)

    return "\n".join(p for p in parts if p).strip()


def _parse_server_from_message(content: str) -> str | None:
    text = (content or "").strip().upper()
    if not text:
        return None

    for candidate in ("SS EAST", "OMEGA", "ALPHA", "DELTA"):
        if f"NOTIFICATION FOR ({candidate})" in text:
            return _normalize_server_name(candidate)
        if f"TAUTULLI ({candidate})" in text:
            return _normalize_server_name(candidate)

    return None


def _parse_state_from_message(content: str) -> str | None:
    text = (content or "").lower()
    if "the plex media server is down" in text:
        return "Down"
    if "the plex media server is up" in text:
        return "Up"
    return None


class PlexServerChoice(app_commands.Choice[str]):
    pass


class PlexStatusChoice(app_commands.Choice[str]):
    pass


class PlexLiveboardCog(commands.Cog):
    def __init__(self, bot, db, cfg):
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self._lock = asyncio.Lock()
        self.plex_liveboard_loop.start()

    def cog_unload(self):
        self.plex_liveboard_loop.cancel()

    def build_plex_embed(self, statuses: dict[str, str]) -> discord.Embed:
        embed = discord.Embed(
            title="🖥️ Plex Liveboard",
            description=(
                "This board updates automatically from Plex webhook logs.\n"
                "Only Plex server up/down notifications affect this board.\n\n"
                f"Last update: {_ts(_utcnow())}"
            ),
        )

        def fmt(value: str) -> str:
            if value == "Up":
                return "🟢 Up"
            if value == "Down":
                return "🔴 Down"
            return "⚪ Unknown"

        embed.add_field(name="Omega", value=fmt(statuses.get("OMEGA", "Unknown")), inline=True)
        embed.add_field(name="Alpha", value=fmt(statuses.get("ALPHA", "Unknown")), inline=True)
        embed.add_field(name="Delta", value=fmt(statuses.get("DELTA", "Unknown")), inline=True)

        embed.set_footer(text="Omega also accepts SS East as an alias.")
        return embed

    async def get_current_statuses(self, guild_id: int) -> dict[str, str]:
        stored = self.db.get_plex_statuses(guild_id)
        statuses = dict(DEFAULT_STATUS)
        statuses.update(stored)
        return statuses

    async def update_plex_liveboard(self, guild_id: int):
        settings = self.db.get_plex_liveboard(guild_id)
        if not settings:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = guild.get_channel(int(settings["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return

        statuses = await self.get_current_statuses(guild_id)
        embed = self.build_plex_embed(statuses)

        try:
            msg = await channel.fetch_message(int(settings["message_id"]))
            await msg.edit(embed=embed, view=None)
        except discord.NotFound:
            self.db.clear_plex_liveboard(guild_id)
        except discord.Forbidden:
            pass

    async def handle_plex_log_message(self, msg: discord.Message):
        if not msg.guild or msg.channel.id != PLEX_LOGS_CHANNEL_ID:
            return

        content = _extract_message_text(msg)
        server = _parse_server_from_message(content)
        state = _parse_state_from_message(content)

        if not server or not state:
            return

        self.db.set_plex_status(msg.guild.id, server, state, _utcnow().isoformat())
        await self.update_plex_liveboard(msg.guild.id)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.webhook_id is None:
            return

        try:
            await self.handle_plex_log_message(msg)
        except Exception:
            pass

    @tasks.loop(minutes=3)
    async def plex_liveboard_loop(self):
        async with self._lock:
            for s in self.db.list_plex_liveboards():
                try:
                    await self.update_plex_liveboard(int(s["guild_id"]))
                except Exception:
                    continue

    @plex_liveboard_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="plexliveboardstart",
        description="Create (or move) the Plex liveboard message to a channel (staff only).",
    )
    @app_commands.describe(channel="Channel to post the Plex liveboard in")
    async def plexliveboardstart(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        statuses = await self.get_current_statuses(interaction.guild.id)
        embed = self.build_plex_embed(statuses)

        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I can’t post in that channel.", ephemeral=True)

        self.db.set_plex_liveboard(interaction.guild.id, channel.id, msg.id)
        await interaction.response.send_message(f"✅ Plex liveboard started in {channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="plexliveboardrefresh",
        description="Manually refresh the Plex liveboard right now (staff only).",
    )
    async def plexliveboardrefresh(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        await interaction.response.send_message("Refreshing…", ephemeral=True)
        await self.update_plex_liveboard(interaction.guild.id)

    @app_commands.command(
        name="plexliveboardstop",
        description="Stop the Plex liveboard updates (staff only).",
    )
    async def plexliveboardstop(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        self.db.clear_plex_liveboard(interaction.guild.id)
        await interaction.response.send_message("✅ Plex liveboard stopped.", ephemeral=True)

    @app_commands.command(
        name="plexset",
        description="Manually set a Plex server status (staff only).",
    )
    @app_commands.describe(
        server="Which Plex server to update",
        status="The status to set",
    )
    @app_commands.choices(
        server=[
            app_commands.Choice(name="Omega", value="OMEGA"),
            app_commands.Choice(name="Alpha", value="ALPHA"),
            app_commands.Choice(name="Delta", value="DELTA"),
        ],
        status=[
            app_commands.Choice(name="Up", value="Up"),
            app_commands.Choice(name="Down", value="Down"),
            app_commands.Choice(name="Unknown", value="Unknown"),
        ],
    )
    async def plexset(
        self,
        interaction: discord.Interaction,
        server: app_commands.Choice[str],
        status: app_commands.Choice[str],
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        self.db.set_plex_status(interaction.guild.id, server.value, status.value, _utcnow().isoformat())
        await self.update_plex_liveboard(interaction.guild.id)

        await interaction.response.send_message(
            f"✅ Set **{server.name}** to **{status.value}**.",
            ephemeral=True,
        )

    @app_commands.command(
        name="plexstatus",
        description="Show the currently stored Plex server statuses (staff only).",
    )
    async def plexstatus(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        statuses = await self.get_current_statuses(interaction.guild.id)

        await interaction.response.send_message(
            (
                f"**Omega:** {statuses.get('OMEGA', 'Unknown')}\n"
                f"**Alpha:** {statuses.get('ALPHA', 'Unknown')}\n"
                f"**Delta:** {statuses.get('DELTA', 'Unknown')}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="plexclear",
        description="Reset all stored Plex server statuses to Unknown (staff only).",
    )
    async def plexclear(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _is_staff(interaction.user, self.cfg.staff_role_id):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        self.db.clear_plex_statuses(interaction.guild.id)
        await self.update_plex_liveboard(interaction.guild.id)

        await interaction.response.send_message("✅ Cleared stored Plex statuses.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlexLiveboardCog(bot, bot.db, bot.cfg))
