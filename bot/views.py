from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot.db import ReportDB
from bot.utils import _vod_4k_label, _vod_language_label, _vod_requested_label, _vod_type_label, build_staff_embed, report_subject, try_dm


CLOSED_STATUSES = {"Resolved", "Can't replicate", "Fixed", "Not Resolved"}
TICKETS_CATEGORY_ID = 1481229575777423362


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nice_ref_label(url: str) -> str:
    u = (url or "").strip()
    low = u.lower()
    if "thetvdb" in low:
        return "TheTVDB"
    if "themoviedb" in low or "tmdb" in low:
        return "TMDB"
    if "imdb" in low:
        return "IMDb"
    return "Reference"


def _build_ticket_embed(report: dict, reporter: discord.abc.User, guild: discord.Guild) -> discord.Embed:
    rid = int(report["id"])
    rtype = str(report.get("report_type") or "").upper()
    payload = report.get("payload") or {}
    subject = report_subject(report.get("report_type") or "", payload)

    src = guild.get_channel(int(report["source_channel_id"])) if report.get("source_channel_id") else None
    src_text = src.mention if isinstance(src, discord.TextChannel) else "Unknown"

    embed = discord.Embed(
        title=f"Ticket for Report #{rid}",
        description=(
            f"**Reporter:** {reporter.mention}\n"
            f"**Subject:** {subject}\n"
            f"**Type:** {rtype}\n"
            f"**Reported from:** {src_text}\n\n"
            f"Use **Resolve** or **Not Resolved** below when this is finished."
        ),
    )

    if rtype == "TV":
        ch_name = (payload.get("channel_name") or "Unknown").strip()
        ch_cat = (payload.get("channel_category") or "Unknown").strip()
        issue = (payload.get("issue") or "—").strip()

        embed.add_field(name="Channel", value=ch_name or "Unknown", inline=True)
        embed.add_field(name="Category", value=ch_cat or "Unknown", inline=True)
        embed.add_field(name="Issue", value=issue[:1024] if issue else "—", inline=False)

    elif rtype == "VOD":
        title = (payload.get("title") or "Unknown").strip()
        requested = _vod_requested_label(payload)
        language = _vod_language_label(payload)
        is_4k = _vod_4k_label(payload)
        content_type_label = _vod_type_label(payload)

        issue = (payload.get("issue") or "—").strip()
        ref = (payload.get("reference_link") or "").strip()

        embed.add_field(name="Title", value=title or "Unknown", inline=False)
        embed.add_field(name="Requested Through Bot", value=requested, inline=True)
        embed.add_field(name="English or Foreign", value=language, inline=True)

        if ref:
            label = _nice_ref_label(ref)
            embed.add_field(name="Reference", value=f"[{label}]({ref})", inline=True)

        embed.add_field(name="4K Title", value=is_4k, inline=True)
        embed.add_field(name="Movie or TV Show", value=content_type_label, inline=True)

        embed.add_field(name="Issue", value=issue[:1024] if issue else "—", inline=False)

    return embed


class TicketResolveView(discord.ui.View):
    def __init__(
        self,
        db: ReportDB,
        staff_channel_id: int,
        support_channel_id: int,
        public_updates: bool,
        staff_role_id: int,
    ):
        super().__init__(timeout=None)
        self.db = db
        self.staff_channel_id = int(staff_channel_id or 0)
        self.support_channel_id = int(support_channel_id or 0)
        self.public_updates = bool(public_updates)
        self.staff_role_id = int(staff_role_id or 0)

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        if not self.staff_role_id:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.id == self.staff_role_id for r in interaction.user.roles)

    def _extract_report_id(self, channel: discord.abc.GuildChannel) -> int | None:
        topic = getattr(channel, "topic", "") or ""
        if "report_id=" not in topic:
            return None
        try:
            return int(topic.split("report_id=", 1)[1].split()[0].strip())
        except Exception:
            return None

    @discord.ui.button(label="Resolve", style=discord.ButtonStyle.success, custom_id="ticket:resolve")
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.channel:
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        report_id = self._extract_report_id(interaction.channel)
        if not report_id:
            return await interaction.response.send_message("❌ Couldn’t determine report ID for this ticket.", ephemeral=True)

        report = self.db.get_report_by_id(report_id)
        if not report:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        from bot.modals import ResolveReportModal  # lazy import to avoid circulars

        modal = ResolveReportModal(
            db=self.db,
            staff_channel_id=self.staff_channel_id,
            support_channel_id=self.support_channel_id,
            public_updates=self.public_updates,
            staff_role_id=self.staff_role_id,
            report_id=report_id,
            delete_current_channel=True,   # this IS the ticket channel
            close_ticket_channel=False,    # modal will clear DB + delete current channel
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Not Resolved", style=discord.ButtonStyle.danger, custom_id="ticket:not_resolved")
    async def not_resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.channel:
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        if not self._is_staff(interaction):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        report_id = self._extract_report_id(interaction.channel)
        if not report_id:
            return await interaction.response.send_message("❌ Couldn’t determine report ID for this ticket.", ephemeral=True)

        report = self.db.get_report_by_id(report_id)
        if not report:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        from bot.modals import NotResolvedReportModal  # lazy import to avoid circulars

        modal = NotResolvedReportModal(
            db=self.db,
            staff_channel_id=self.staff_channel_id,
            support_channel_id=self.support_channel_id,
            public_updates=self.public_updates,
            staff_role_id=self.staff_role_id,
            report_id=report_id,
            delete_current_channel=True,   # this IS the ticket channel
            close_ticket_channel=False,    # modal will clear DB + delete current channel
        )
        await interaction.response.send_modal(modal)


class ReportActionView(discord.ui.View):
    def __init__(
        self,
        db: ReportDB,
        staff_channel_id: int,
        support_channel_id: int,
        public_updates: bool,
        staff_role_id: int,
    ):
        super().__init__(timeout=None)
        self.db = db
        self.staff_channel_id = int(staff_channel_id or 0)
        self.support_channel_id = int(support_channel_id or 0)
        self.public_updates = bool(public_updates)
        self.staff_role_id = int(staff_role_id or 0)

    def disable_all(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        if not self.staff_role_id:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.id == self.staff_role_id for r in interaction.user.roles)

    async def _ensure_staff_channel(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return False

        if self.staff_channel_id and interaction.channel.id != self.staff_channel_id:
            await interaction.response.send_message("❌ Use this in the staff reports channel.", ephemeral=True)
            return False

        if not self._is_staff(interaction):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return False

        return True

    async def _close_ticket_channel_if_any(self, guild: discord.Guild, report_id: int):
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
                await ch.delete(reason=f"Report #{report_id} closed from staff channel")
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

    @discord.ui.button(label="Resolved", style=discord.ButtonStyle.success, custom_id="report:resolved")
    async def resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_staff_channel(interaction):
            return

        if not interaction.message:
            return await interaction.response.send_message("❌ Couldn’t read the report message.", ephemeral=True)

        report = self.db.get_by_staff_message_id(interaction.message.id)
        if not report:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        report_id = int(report["id"])

        from bot.modals import ResolveReportModal  # lazy import to avoid circulars

        modal = ResolveReportModal(
            db=self.db,
            staff_channel_id=self.staff_channel_id,
            support_channel_id=self.support_channel_id,
            public_updates=self.public_updates,
            staff_role_id=self.staff_role_id,
            report_id=report_id,
            delete_current_channel=False,  # staff channel message, don't delete this channel
            close_ticket_channel=True,     # close any ticket for this report first
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Not Resolved", style=discord.ButtonStyle.danger, custom_id="report:not_resolved")
    async def not_resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_staff_channel(interaction):
            return

        if not interaction.message:
            return await interaction.response.send_message("❌ Couldn’t read the report message.", ephemeral=True)

        report = self.db.get_by_staff_message_id(interaction.message.id)
        if not report:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        report_id = int(report["id"])

        from bot.modals import NotResolvedReportModal  # lazy import to avoid circulars

        modal = NotResolvedReportModal(
            db=self.db,
            staff_channel_id=self.staff_channel_id,
            support_channel_id=self.support_channel_id,
            public_updates=self.public_updates,
            staff_role_id=self.staff_role_id,
            report_id=report_id,
            delete_current_channel=False,  # staff channel message, don't delete this channel
            close_ticket_channel=True,     # close any ticket for this report first
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Open ticket", style=discord.ButtonStyle.primary, custom_id="report:ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_staff_channel(interaction):
            return

        if not interaction.message:
            return await interaction.response.send_message("❌ Couldn’t read the report message.", ephemeral=True)

        report = self.db.get_by_staff_message_id(interaction.message.id)
        if not report:
            return await interaction.response.send_message("❌ Report not found.", ephemeral=True)

        if (report.get("status") or "").strip() in CLOSED_STATUSES:
            return await interaction.response.send_message("⚠️ This report is already closed.", ephemeral=True)

        guild = interaction.guild
        reporter = await interaction.client.fetch_user(int(report["reporter_id"]))

        existing_id = self.db.get_ticket_channel_id(report["id"])
        if existing_id:
            ch = guild.get_channel(int(existing_id))
            if ch:
                return await interaction.response.send_message(f"ℹ️ Ticket already exists: {ch.mention}", ephemeral=True)
            self.db.set_ticket_channel_id(report["id"], None)

        me = guild.me
        if not me:
            return await interaction.response.send_message("❌ Couldn’t read my permissions.", ephemeral=True)

        if not me.guild_permissions.manage_channels:
            return await interaction.response.send_message("❌ I don’t have permission to create channels or set permissions.", ephemeral=True)

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            reporter: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        }
        if self.staff_role_id:
            role = guild.get_role(self.staff_role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        category = guild.get_channel(TICKETS_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            category = None

        channel_name = f"report-{report['id']}"
        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket for report #{report['id']} | report_id={report['id']}",
                reason=f"Ticket opened for report #{report['id']}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to create channels or set permissions.", ephemeral=True)
        except Exception as e:
            return await interaction.response.send_message(f"❌ Failed to create ticket channel: {e!r}", ephemeral=True)

        self.db.set_ticket_channel_id(report["id"], ticket_channel.id)

        summary = _build_ticket_embed(report=report, reporter=reporter, guild=guild)

        resolve_view = TicketResolveView(
            db=self.db,
            staff_channel_id=self.staff_channel_id,
            support_channel_id=self.support_channel_id,
            public_updates=self.public_updates,
            staff_role_id=self.staff_role_id,
        )

        await ticket_channel.send(content=reporter.mention, embed=summary, view=resolve_view)

        # Claim info (cosmetic): record + show on embed
        claimed_by_user_id = int(interaction.user.id)
        claimed_at = _now_iso()

        if hasattr(self.db, "mark_claimed"):
            try:
                self.db.mark_claimed(int(report["id"]), claimed_by_user_id, claimed_at)  # type: ignore[attr-defined]
            except Exception:
                pass

        self.db.update_status(report["id"], "Ticket Open")

        source = guild.get_channel(int(report["source_channel_id"])) or interaction.channel

        embed = build_staff_embed(
            report["id"],
            report["report_type"],
            reporter,
            source,
            report["payload"],
            "Ticket Open",
            ticket_channel_id=ticket_channel.id,
            claimed_by_user_id=claimed_by_user_id,
            claimed_at=claimed_at,
        )

        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "report:ticket":
                child.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True)
