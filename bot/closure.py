"""Cancellable ticket closure grace periods (restart safely leaves reports open)."""
import asyncio
import logging

import discord

log = logging.getLogger(__name__)
CLOSURE_DELAY = 10 * 60


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

    async def on_message(self, message):
        # Ignore our own countdown/confirmation messages, but accept other activity.
        if self.bot.user and message.author.id == self.bot.user.id:
            return
        for pending in tuple(self.pending.values()):
            if pending.waiting and message.channel.id == pending.channel_id:
                pending.cancelled.set()
                pending.waiting = False

    async def run(self, interaction, db, report_id, finalize):
        key = (interaction.guild.id, report_id)
        if key in self.pending:
            await interaction.followup.send("This report already has a closure in progress.", ephemeral=True)
            return
        report = db.get_report_by_id(report_id)
        if not report or report.get("status") in {"Resolved", "Not Resolved"}:
            await interaction.followup.send("This report is already closed or unavailable.", ephemeral=True)
            return
        ticket_id = report.get("ticket_channel_id")
        pending = PendingClosure(int(ticket_id)) if ticket_id else PendingClosure(None)
        self.pending[key] = pending
        notice = None
        view = None
        try:
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
                if (not current or current.get("status") in {"Resolved", "Not Resolved"}
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
            self.pending.pop(key, None)
