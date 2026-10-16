"""Cancellable ticket closure grace periods (restart safely leaves reports open)."""
import asyncio
from copy import deepcopy
import logging

import discord

from bot.views import CLOSED_STATUSES, ReportActionView, TicketResolveView

log = logging.getLogger(__name__)
CLOSURE_DELAY = 10 * 60
PENDING_CLOSURE_FIELDS = {"Pending closure", "Pending closure details"}


class CancelClosureView(discord.ui.View):
    def __init__(self, pending):
        super().__init__(timeout=None)
        self.pending = pending

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.pending.waiting:
            self.pending.cancelled.set()
            self.pending.waiting = False
            await interaction.response.send_message(
                "Closure cancelled. The ticket and report remain open.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "This closure is no longer pending.", ephemeral=True
            )


class PendingClosure:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.cancelled = asyncio.Event()
        self.waiting = True


class TicketClosureManager:
    def __init__(self, bot, *, delay=CLOSURE_DELAY):
        self.bot = bot
        self.delay = delay
        self.pending = {}

    async def _action_messages(self, guild, report):
        """Find existing controls, including tickets created before this feature."""
        messages = []
        staff_id = self.bot.cfg.staff_channel_id
        if staff_id and report.get("staff_message_id"):
            try:
                channel = guild.get_channel(staff_id) or await guild.fetch_channel(staff_id)
                message = await channel.fetch_message(int(report["staff_message_id"]))
                messages.append((message, ReportActionView))
            except discord.HTTPException:
                log.exception("Unable to find staff controls for report %s", report["id"])
        if report.get("ticket_channel_id"):
            try:
                ticket_id = int(report["ticket_channel_id"])
                channel = guild.get_channel(ticket_id) or await guild.fetch_channel(ticket_id)
                # The summary is normally the first message. Search from the start
                # so long-running tickets do not hide their controls.
                async for message in channel.history(limit=None, oldest_first=True):
                    if message.author.id != self.bot.user.id:
                        continue
                    if any(getattr(child, "custom_id", None) in {"ticket:resolve", "ticket:not_resolved"}
                           for row in message.components for child in row.children):
                        messages.append((message, TicketResolveView))
                        break
            except discord.NotFound:
                pass  # A completed closure may already have deleted the ticket.
            except discord.HTTPException:
                log.exception("Unable to find ticket controls for report %s", report["id"])
        return messages

    async def _update_report_messages(self, db, report, messages, *, closure=None, refresh=False):
        cfg = self.bot.cfg
        for message, view_type in messages:
            # Fresh views preserve callbacks and never mutate the shared fallback.
            view = view_type(db, cfg.staff_channel_id, cfg.support_channel_id,
                             cfg.public_updates, cfg.staff_role_id, cfg.tickets_category_id)
            view.apply_report_state(report)
            try:
                if refresh:
                    # Finalization can replace the embed. Clean the latest version
                    # so the completed outcome and details remain intact.
                    message = await message.fetch()
                embeds = [deepcopy(embed) for embed in message.embeds]
                changed = False
                for embed in embeds:
                    for index in reversed(range(len(embed.fields))):
                        if embed.fields[index].name in PENDING_CLOSURE_FIELDS:
                            embed.remove_field(index)
                            changed = True
                if closure:
                    if not embeds:
                        embeds.append(discord.Embed(title=f"Report #{report['id']}"))
                    embeds[0].add_field(
                        name="Pending closure",
                        value=f"**{closure['outcome']}** requested by <@{closure['staff_id']}>.\n"
                              "The report remains open until the countdown ends.",
                        inline=False,
                    )
                    embeds[0].add_field(
                        name="Pending closure details",
                        value=closure["details"].strip()[:1024] or "No details provided.",
                        inline=False,
                    )
                    changed = True
                kwargs = {"view": view}
                if changed:
                    kwargs["embeds"] = embeds
                await message.edit(**kwargs)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Unable to update report message for report %s", report["id"])

    async def recover_interrupted(self, db):
        """A restart cancels timers; restore controls left disabled on Discord."""
        for report in db.list_pending_closures():
            key = (report["guild_id"], report["id"])
            guild = self.bot.get_guild(report["guild_id"])
            if key in self.pending or guild is None:
                continue
            messages = await self._action_messages(guild, report)
            report["closure_pending"] = False
            await self._update_report_messages(db, report, messages)
            db.set_closure_pending(report["id"], False)

    async def on_message(self, message):
        # Ignore our own countdown/confirmation messages, but accept other activity.
        if self.bot.user and message.author.id == self.bot.user.id:
            return
        for pending in tuple(self.pending.values()):
            if pending.waiting and message.channel.id == pending.channel_id:
                pending.cancelled.set()
                pending.waiting = False

    async def run(self, interaction, db, report_id, finalize, *, outcome=None, details=""):
        key = (interaction.guild.id, report_id)
        if key in self.pending:
            await interaction.followup.send("This report already has a closure in progress.", ephemeral=True)
            return
        report = db.get_report_by_id(report_id)
        if not report or report.get("status") in CLOSED_STATUSES:
            await interaction.followup.send("This report is already closed or unavailable.", ephemeral=True)
            return
        if report.get("closure_pending"):
            await interaction.followup.send("This report already has a closure in progress.", ephemeral=True)
            return
        ticket_id = report.get("ticket_channel_id")
        pending = PendingClosure(int(ticket_id)) if ticket_id else PendingClosure(None)
        self.pending[key] = pending
        notice = None
        view = None
        messages = []
        try:
            db.set_closure_pending(report_id, True)
            report = dict(report, closure_pending=True)
            messages = await self._action_messages(interaction.guild, report)
            closure = None
            if ticket_id and outcome:
                closure = {"outcome": outcome, "details": details, "staff_id": interaction.user.id}
            await self._update_report_messages(db, report, messages, closure=closure)
            if ticket_id:
                # Fetch on a cache miss; failure must never bypass the grace period.
                channel = interaction.guild.get_channel(int(ticket_id))
                if channel is None:
                    channel = await interaction.guild.fetch_channel(int(ticket_id))
                view = CancelClosureView(pending)
                minutes = self.delay / 60
                duration = f"{minutes:g} minute{'s' if minutes != 1 else ''}"
                notice = await channel.send(
                    f"Report **#{report_id}** and this ticket will close in **{duration}**. "
                    "Anyone can click **Cancel** or send a message here to keep both open.",
                    view=view,
                )
                await interaction.followup.send(f"Closure scheduled in the ticket for {duration} from now.", ephemeral=True)
                try:
                    await asyncio.wait_for(pending.cancelled.wait(), timeout=self.delay)
                except asyncio.TimeoutError:
                    pass
                pending.waiting = False
                if pending.cancelled.is_set():
                    await notice.edit(content="Closure cancelled. The ticket and report remain open.", view=None)
                    return
                current = db.get_report_by_id(report_id)
                if (not current or current.get("status") in CLOSED_STATUSES
                        or current.get("ticket_channel_id") != ticket_id):
                    await notice.edit(content="This closure is no longer applicable.", view=None)
                    return
                await notice.edit(content="The grace period has ended. Closing the ticket and report…", view=None)
            pending.waiting = False
            await finalize(interaction)
        except discord.HTTPException:
            log.exception("Unable to complete closure for report %s", report_id)
            await interaction.followup.send(
                "Could not complete the closure. Please check the ticket and try again.", ephemeral=True
            )
        finally:
            pending.waiting = False
            if view:
                view.stop()
            try:
                current = db.get_report_by_id(report_id)
                if current:
                    await self._update_report_messages(db, dict(current, closure_pending=False), messages, refresh=True)
                db.set_closure_pending(report_id, False)
            finally:
                self.pending.pop(key, None)
