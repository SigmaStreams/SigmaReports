import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone


def _iso_to_discord_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except Exception:
        return iso


class ReportPanelView(discord.ui.View):
    """
    Persistent view for the report panel.
    Uses lazy imports inside button callbacks to avoid circular imports.
    """

    def __init__(self, db, cfg):
        super().__init__(timeout=None)
        self.db = db
        self.cfg = cfg

    def _support_channel_mention(self, interaction: discord.Interaction) -> str:
        if not interaction.guild or not self.cfg.support_channel_id:
            return "the support channel"
        ch = interaction.guild.get_channel(self.cfg.support_channel_id)
        return ch.mention if ch else "the support channel"

    async def _block_gate(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True

        blocked, is_perm, expires_at, reason = self.db.is_user_blocked(
            interaction.guild.id, interaction.user.id
        )
        if not blocked:
            return True

        support = self._support_channel_mention(interaction)
        reason_txt = f"\nReason: {reason}" if reason else ""

        if is_perm:
            msg = (
                f"🚫 {interaction.user.mention} you are blocked from using the report system.\n"
                f"To appeal, please open a ticket in {support}.{reason_txt}"
            )
        else:
            exp = f"\nBlock expires: {_iso_to_discord_ts(expires_at)}" if expires_at else ""
            msg = (
                f"🚫 {interaction.user.mention} you are temporarily blocked from using the report system."
                f"{exp}\nTo appeal, please open a ticket in {support}.{reason_txt}"
            )

        await interaction.response.send_message(msg, ephemeral=True)
        return False

    @discord.ui.button(
        label="Report Live TV",
        style=discord.ButtonStyle.primary,
        emoji="📺",
        custom_id="panel:report_tv",
    )
    async def report_tv_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._block_gate(interaction):
            return

        # Lazy import avoids circular imports
        from bot.modals import TVReportModal

        await interaction.response.send_modal(TVReportModal(self.db, self.cfg))

    @discord.ui.button(
        label="Report Movie / TV Show",
        style=discord.ButtonStyle.secondary,
        emoji="🎬",
        custom_id="panel:report_vod",
    )
    async def report_vod_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._block_gate(interaction):
            return

        # Lazy import avoids circular imports
        from bot.modals import VODQuestionnaireView

        await interaction.response.send_message(
            "English or Foreign?",
            view=VODQuestionnaireView(self.db, self.cfg, interaction.user.id),
            ephemeral=True,
        )


class ReportPanelCog(commands.Cog):
    def __init__(self, bot, db, cfg):
        self.bot = bot
        self.db = db
        self.cfg = cfg

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return False
        return any(r.id == self.cfg.staff_role_id for r in member.roles)

    @app_commands.command(
        name="reportpanel",
        description="Post a report panel embed with buttons (staff only).",
    )
    @app_commands.describe(channel="Channel to post the report panel in")
    async def reportpanel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)
        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        embed = discord.Embed(
            title="Report an issue",
            description=(
                "Use the buttons below to submit a report.\n\n"
                "📺 **Live TV** — buffering, offline channels, wrong content\n"
                "🎬 **Movies / TV Shows** — playback issues, missing episodes, quality problems\n\n"
                "**What happens next?**\n"
                "Staff will review your report. If we need more details, we may open a **private ticket channel** with you "
                "so we can troubleshoot properly.\n\n"
                "**Tips (the more detail, the faster we can fix it):**\n"
                "• what you expected vs what happened\n"
                "• when it happened\n"
                "• device/app used\n"
                "• any errors/screenshots (if applicable)"
            ),
        )
        embed.set_footer(text="You’ll receive updates via DM and/or in a ticket channel if one is opened.")

        view = ReportPanelView(self.db, self.cfg)

        try:
            await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don’t have permission to post in that channel.",
                ephemeral=True,
            )

        await interaction.response.send_message(f"✅ Posted a report panel in {channel.mention}.", ephemeral=True)


async def setup(bot):
    # Register the persistent view here so buttons keep working after restarts
    bot.add_view(ReportPanelView(bot.db, bot.cfg))
    await bot.add_cog(ReportPanelCog(bot, bot.db, bot.cfg))
