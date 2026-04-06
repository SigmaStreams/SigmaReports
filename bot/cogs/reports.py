import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from bot.modals import TVReportModal, VODTypePickerView
from bot.views import ReportActionView
from bot.utils import build_staff_embed

OWNER_ID = 1229271933736976395


def _iso_to_discord_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        return f"<t:{ts}:R>"
    except Exception:
        return iso


class Reports(commands.Cog):
    def __init__(self, bot, db, cfg):
        self.bot = bot
        self.db = db
        self.cfg = cfg

    # ----------------------------
    # Helpers
    # ----------------------------

    def _allowed_channel(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.channel) and interaction.channel.id in set(self.cfg.reports_channel_ids)

    def _allowed_channels_hint(self, interaction: discord.Interaction) -> str:
        if not interaction.guild:
            return "the allowed channels"
        mentions = []
        for cid in self.cfg.reports_channel_ids:
            ch = interaction.guild.get_channel(cid)
            if ch:
                mentions.append(ch.mention)
        return ", ".join(mentions) if mentions else "the allowed channels"

    def _support_channel_mention(self, interaction: discord.Interaction) -> str:
        if not interaction.guild or not self.cfg.support_channel_id:
            return "the support channel"
        ch = interaction.guild.get_channel(self.cfg.support_channel_id)
        return ch.mention if ch else "the support channel"

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return False
        return any(r.id == self.cfg.staff_role_id for r in member.roles)

    async def _refresh_staff_report_message(self, guild: discord.Guild, report: dict, *, disable_actions: bool) -> None:
        staff_message_id = report.get("staff_message_id")
        if not staff_message_id:
            return

        staff_ch = guild.get_channel(self.cfg.staff_channel_id)
        if not isinstance(staff_ch, discord.TextChannel):
            return

        try:
            staff_msg = await staff_ch.fetch_message(int(staff_message_id))
        except Exception:
            return

        try:
            reporter = await self.bot.fetch_user(int(report["reporter_id"]))
        except Exception:
            reporter = self.bot.get_user(int(report["reporter_id"]))
            if reporter is None:
                return

        source = guild.get_channel(int(report["source_channel_id"])) or staff_ch
        status = str(report.get("status") or "Open")
        ticket_channel_id = report.get("ticket_channel_id")

        embed = build_staff_embed(
            int(report["id"]),
            report["report_type"],
            reporter,
            source,
            report["payload"],
            status,
            ticket_channel_id=int(ticket_channel_id) if ticket_channel_id else None,
            claimed_by_user_id=report.get("claimed_by_user_id"),
            claimed_at=report.get("claimed_at"),
            resolved_by_id=report.get("resolved_by"),
            resolved_note=report.get("resolved_note"),
        )

        view = ReportActionView(
            self.db,
            self.cfg.staff_channel_id,
            self.cfg.support_channel_id,
            self.cfg.public_updates,
            self.cfg.staff_role_id,
        )
        if disable_actions:
            view.disable_all()

        try:
            await staff_msg.edit(embed=embed, view=view)
        except Exception:
            return

    async def _close_ticket_channel_if_any(self, guild: discord.Guild, report_id: int) -> None:
        ticket_id = None
        try:
            ticket_id = self.db.get_ticket_channel_id(report_id)
        except Exception:
            ticket_id = None

        if not ticket_id:
            return

        ch = guild.get_channel(int(ticket_id))
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.delete(reason=f"Report #{report_id} closed from bulk cleanup")
            except discord.Forbidden:
                try:
                    await ch.edit(name=f"closed-report-{report_id}")
                except Exception:
                    pass
            except Exception:
                pass

        try:
            self.db.set_ticket_channel_id(report_id, None)
        except Exception:
            pass

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

        await interaction.response.send_message(msg)
        return False

    # ----------------------------
    # Report Commands
    # ----------------------------

    @app_commands.command(
        name="report-tv",
        description="Report an issue with a live TV channel.",
    )
    async def report_tv(self, interaction: discord.Interaction):
        if not self._allowed_channel(interaction):
            return await interaction.response.send_message(
                f"Use this command in: {self._allowed_channels_hint(interaction)}."
            )

        if not await self._block_gate(interaction):
            return

        await interaction.response.send_modal(TVReportModal(self.db, self.cfg))

    @app_commands.command(
        name="report-vod",
        description="Report an issue with a movie or TV show.",
    )
    async def report_vod(self, interaction: discord.Interaction):
        if not self._allowed_channel(interaction):
            return await interaction.response.send_message(
                f"Use this command in: {self._allowed_channels_hint(interaction)}."
            )

        if not await self._block_gate(interaction):
            return

        await interaction.response.send_message(
            "Is this a **TV show** or a **movie**?",
            view=VODTypePickerView(self.db, self.cfg),
            ephemeral=True,
        )

    # ----------------------------
    # Staff: edit report
    # ----------------------------

    @app_commands.command(
        name="editreport",
        description="Edit a report (staff only).",
    )
    @app_commands.describe(
        report_id="The numeric report ID (e.g. 123)",
        reporter="Set the reporter to this user",
    )
    async def editreport(self, interaction: discord.Interaction, report_id: int, reporter: discord.User):
        if not interaction.guild:
            return await interaction.response.send_message(
                "This must be used in a server.",
                ephemeral=True,
            )

        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        report = self.db.get_report_by_id(int(report_id))
        if not report or int(report.get("guild_id", 0)) != interaction.guild.id:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        old_reporter_id = int(report.get("reporter_id", 0) or 0)
        new_reporter_id = int(reporter.id)

        if old_reporter_id == new_reporter_id:
            return await interaction.response.send_message("Nothing changed (same reporter).", ephemeral=True)

        ok = False
        try:
            ok = bool(self.db.update_reporter_id(int(report_id), new_reporter_id))
        except Exception:
            ok = False

        if not ok:
            return await interaction.response.send_message("❌ Failed to update reporter.", ephemeral=True)

        # Refresh local report + update staff embed message if possible
        report = self.db.get_report_by_id(int(report_id)) or report

        staff_message_id = report.get("staff_message_id")
        if staff_message_id:
            staff_ch = interaction.guild.get_channel(self.cfg.staff_channel_id)
            if isinstance(staff_ch, discord.TextChannel):
                try:
                    staff_msg = await staff_ch.fetch_message(int(staff_message_id))

                    # rebuild embed with updated reporter
                    try:
                        new_reporter_user = await interaction.client.fetch_user(int(report["reporter_id"]))
                    except Exception:
                        new_reporter_user = interaction.client.get_user(int(report["reporter_id"])) or reporter

                    source = interaction.guild.get_channel(int(report["source_channel_id"])) or staff_ch
                    status = report.get("status") or "Open"

                    ticket_channel_id = report.get("ticket_channel_id")

                    embed = build_staff_embed(
                        int(report["id"]),
                        report["report_type"],
                        new_reporter_user,
                        source,
                        report["payload"],
                        str(status),
                        ticket_channel_id=int(ticket_channel_id) if ticket_channel_id else None,
                        claimed_by_user_id=report.get("claimed_by_user_id"),
                        claimed_at=report.get("claimed_at"),
                        resolved_by_id=report.get("resolved_by"),
                        resolved_note=report.get("resolved_note"),
                    )

                    view = ReportActionView(
                        self.db,
                        self.cfg.staff_channel_id,
                        self.cfg.support_channel_id,
                        self.cfg.public_updates,
                        self.cfg.staff_role_id,
                    )

                    await staff_msg.edit(embed=embed, view=view)
                except Exception:
                    pass

        await interaction.response.send_message(
            f"✅ Updated report **#{report_id}** reporter to {reporter.mention}.",
            ephemeral=True,
        )

    # ----------------------------
    # Owner / Admin
    # ----------------------------

    @app_commands.command(
        name="reportpings",
        description="Toggle staff pings for new reports (owner only).",
    )
    async def reportpings(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        enabled = self.db.toggle_report_pings()
        state = "ON 🔔" if enabled else "OFF 🔕"
        await interaction.response.send_message(
            f"Staff pings for new reports are now: **{state}**",
            ephemeral=True,
        )

    @app_commands.command(
        name="synccommands",
        description="Force re-sync slash commands for this server (owner only).",
    )
    async def synccommands(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message(
                "This must be used in a server.",
                ephemeral=True,
            )

        guild = discord.Object(id=interaction.guild.id)

        await interaction.response.send_message("Syncing…", ephemeral=True)

        self.bot.tree.copy_global_to(guild=guild)
        synced = await self.bot.tree.sync(guild=guild)

        await interaction.followup.send(
            f"✅ Synced **{len(synced)}** commands.",
            ephemeral=True,
        )

    @app_commands.command(
        name="close-open-reports",
        description="Mark all open reports as resolved in this server (staff only).",
    )
    async def close_open_reports(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "This must be used in a server.",
                ephemeral=True,
            )

        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        reports_to_close = self.db.list_reports_by_statuses(
            interaction.guild.id,
            ("Open", "Ticket Open"),
        )
        updated = self.db.close_open_reports(interaction.guild.id)

        if updated > 0:
            for report in reports_to_close:
                await self._close_ticket_channel_if_any(interaction.guild, int(report["id"]))
                report["status"] = "Resolved"
                report["ticket_channel_id"] = None
                await self._refresh_staff_report_message(
                    interaction.guild,
                    report,
                    disable_actions=True,
                )

        liveboard = self.bot.get_cog("LiveboardCog")
        if liveboard:
            try:
                await liveboard.update_liveboard(interaction.guild.id)
            except Exception:
                pass

        if updated == 0:
            return await interaction.response.send_message(
                "No reports with status **Open** or **Ticket Open** were found.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Marked **{updated}** report(s) as **Resolved**.",
            ephemeral=True,
        )

    # ----------------------------
    # Reactivate
    # ----------------------------

    @app_commands.command(
        name="reportreactivate",
        description="Re-activate staff buttons for a report (reopens it to Open).",
    )
    @app_commands.describe(report_id="The numeric report ID (e.g. 123)")
    async def reportreactivate(self, interaction: discord.Interaction, report_id: int):
        if not interaction.guild:
            return await interaction.response.send_message(
                "This must be used in a server.",
                ephemeral=True,
            )

        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=True)

        report = self.db.get_report_by_id(report_id)
        if not report or report["guild_id"] != interaction.guild.id:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        staff_message_id = report.get("staff_message_id")
        if not staff_message_id:
            return await interaction.response.send_message(
                "❌ This report has no linked staff message.",
                ephemeral=True,
            )

        staff_ch = interaction.guild.get_channel(self.cfg.staff_channel_id)
        if not staff_ch:
            return await interaction.response.send_message("❌ Staff channel not found.", ephemeral=True)

        try:
            staff_msg = await staff_ch.fetch_message(int(staff_message_id))
        except Exception:
            return await interaction.response.send_message(
                "❌ Could not fetch the staff report message.",
                ephemeral=True,
            )

        # Reopen
        self.db.update_status(report_id, "Open")
        report["status"] = "Open"

        try:
            reporter = await interaction.client.fetch_user(report["reporter_id"])
        except Exception:
            reporter = interaction.client.get_user(report["reporter_id"]) or interaction.user

        source = interaction.guild.get_channel(report["source_channel_id"]) or staff_ch

        ticket_channel_id = report.get("ticket_channel_id")
        claimed_by_user_id = report.get("claimed_by_user_id")
        claimed_at = report.get("claimed_at")

        embed = build_staff_embed(
            report["id"],
            report["report_type"],
            reporter,
            source,
            report["payload"],
            "Open",
            ticket_channel_id=ticket_channel_id if ticket_channel_id else None,
            claimed_by_user_id=claimed_by_user_id if claimed_by_user_id else None,
            claimed_at=claimed_at if claimed_at else None,
        )

        view = ReportActionView(
            self.db,
            self.cfg.staff_channel_id,
            self.cfg.support_channel_id,
            self.cfg.public_updates,
            self.cfg.staff_role_id,
        )

        await staff_msg.edit(embed=embed, view=view)

        await interaction.response.send_message(
            f"✅ Re-activated buttons for report **#{report_id}**.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Reports(bot, bot.db, bot.cfg))
