import asyncio
from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from bot.closure import TicketClosureManager
from bot.db import ReportDB
from bot.views import ReportActionView, TicketResolveView


class TicketClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99), cfg=SimpleNamespace(
            staff_channel_id=22, support_channel_id=0, public_updates=False,
            staff_role_id=0, tickets_category_id=0,
        ))
        self.manager = TicketClosureManager(self.bot, delay=0.01)
        self.notice = SimpleNamespace(edit=AsyncMock())
        staff_embed = discord.Embed(title="Report #7")
        staff_embed.add_field(name="Status", value="Ticket Open", inline=False)
        staff_embed.add_field(name="Issue", value="No audio", inline=False)
        staff_embed.set_thumbnail(url="https://example.com/poster.png")
        self.staff_message = SimpleNamespace(edit=AsyncMock(), embeds=[staff_embed])
        self.ticket_message = SimpleNamespace(
            author=SimpleNamespace(id=99), edit=AsyncMock(),
            embeds=[discord.Embed(title="Ticket for Report #7", description="Original ticket summary")],
            components=[SimpleNamespace(children=[SimpleNamespace(custom_id="ticket:resolve")])],
        )
        for message in (self.staff_message, self.ticket_message):
            message.fetch = AsyncMock(return_value=message)

            async def edit(*, embeds=None, view=None, target=message):
                if embeds is not None:
                    target.embeds = embeds

            message.edit.side_effect = edit

        async def history(**kwargs):
            yield self.ticket_message

        self.channel = SimpleNamespace(send=AsyncMock(return_value=self.notice), history=Mock(side_effect=history))
        self.staff_channel = SimpleNamespace(fetch_message=AsyncMock(return_value=self.staff_message))
        self.interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_channel=Mock(
                side_effect=lambda cid: self.staff_channel if cid == 22 else self.channel)),
            user=SimpleNamespace(id=55),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.db = Mock()
        self.db.get_report_by_id.return_value = {
            "id": 7, "guild_id": 1, "staff_message_id": 33,
            "status": "Ticket Open", "ticket_channel_id": 42,
        }
        self.db.set_closure_pending.side_effect = lambda rid, pending: self.db.get_report_by_id.return_value.update(closure_pending=pending)
        self.finalize = AsyncMock(side_effect=lambda interaction: self.db.get_report_by_id.return_value.update(status="Resolved"))

    async def run_closure(self, *, outcome="Resolved", details="Staff explanation"):
        await self.manager.run(self.interaction, self.db, 7, self.finalize, outcome=outcome, details=details)

    async def start_closure(self, **kwargs):
        self.channel.send.reset_mock()
        task = asyncio.create_task(self.run_closure(**kwargs))
        while not self.channel.send.called:
            await asyncio.sleep(0)
        return task

    async def test_quiet_ticket_closes_once(self):
        await self.run_closure()
        self.finalize.assert_awaited_once_with(self.interaction)
        self.assertFalse(self.manager.pending)

    async def test_message_cancels_and_allows_new_attempt(self):
        task = await self.start_closure()
        await self.manager.on_message(SimpleNamespace(author=SimpleNamespace(id=5), channel=SimpleNamespace(id=42)))
        await task
        self.finalize.assert_not_awaited()
        await self.run_closure()
        self.finalize.assert_awaited_once()

    async def test_anyone_can_cancel(self):
        task = await self.start_closure()
        view = self.channel.send.call_args.kwargs["view"]
        click = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await view.children[0].callback(click)
        await task
        self.finalize.assert_not_awaited()

    async def test_own_messages_and_other_channels_do_not_cancel(self):
        task = await self.start_closure()
        for author, channel in [(99, 42), (5, 43)]:
            await self.manager.on_message(SimpleNamespace(author=SimpleNamespace(id=author), channel=SimpleNamespace(id=channel)))
        await task
        self.finalize.assert_awaited_once()

    async def test_duplicate_request_does_not_replace_timer(self):
        task = await self.start_closure()
        await self.run_closure()
        await task
        self.channel.send.assert_awaited_once()
        self.finalize.assert_awaited_once()

    async def test_report_without_ticket_closes_immediately(self):
        self.db.get_report_by_id.return_value = {"status": "Open", "ticket_channel_id": None}
        await self.run_closure()
        self.channel.send.assert_not_awaited()
        self.finalize.assert_awaited_once()

    async def test_changed_ticket_does_not_close(self):
        task = await self.start_closure()
        self.db.get_report_by_id.return_value = {"status": "Ticket Open", "ticket_channel_id": 43}
        await task
        self.finalize.assert_not_awaited()

    async def test_interrupted_countdown_leaves_report_open(self):
        task = await self.start_closure()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.finalize.assert_not_awaited()
        self.assertFalse(self.manager.pending)
        self.assertFalse(self.db.get_report_by_id(7)["closure_pending"])
        self.assertFalse(self.staff_message.edit.call_args.kwargs["view"].resolved.disabled)
        self.assertFalse(any(field.name.startswith("Pending closure") for field in self.staff_message.embeds[0].fields))

    async def test_failed_notice_does_not_close_report(self):
        import discord
        self.channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions"
        )
        with self.assertLogs("bot.closure", level="ERROR"):
            await self.run_closure()
        self.finalize.assert_not_awaited()
        self.assertFalse(self.manager.pending)

    async def test_both_modals_route_through_grace_period(self):
        from bot.modals import ResolveReportModal, NotResolvedReportModal
        self.db.get_report_by_id.return_value = {
            "guild_id": 1, "status": "Ticket Open", "ticket_channel_id": 42,
        }
        self.interaction.response = SimpleNamespace(defer=AsyncMock())
        manager = SimpleNamespace(run=AsyncMock())
        self.interaction.client = SimpleNamespace(ticket_closures=manager)
        for modal_type in (ResolveReportModal, NotResolvedReportModal):
            modal = modal_type(self.db, 1, 2, False, 3, 4, 7)
            modal.details._value = "Staff explanation"
            await modal.on_submit(self.interaction)
            self.assertEqual(manager.run.call_args.args[2], 7)
            self.assertEqual(manager.run.call_args.args[3], modal._finalize)
            self.assertEqual(manager.run.call_args.kwargs, {
                "outcome": "Resolved" if modal_type is ResolveReportModal else "Not Resolved",
                "details": "Staff explanation",
            })
        self.assertEqual(manager.run.await_count, 2)

    async def test_notice_uses_configured_duration(self):
        for minutes, label in [(1, "1 minute"), (5, "5 minutes"), (10, "10 minutes")]:
            self.manager.delay = minutes * 60
            self.channel.send.reset_mock()
            task = await self.start_closure()
            self.assertIn(f"**{label}**", self.channel.send.call_args.args[0])
            self.assertIn(label, self.interaction.followup.send.call_args.args[0])
            self.manager.pending[(1, 7)].cancelled.set()
            await task

    async def test_all_actions_disabled_during_countdown_and_after_closure(self):
        task = await self.start_closure()
        for message, view_type in [(self.staff_message, ReportActionView), (self.ticket_message, TicketResolveView)]:
            view = message.edit.call_args.kwargs["view"]
            self.assertIsInstance(view, view_type)
            self.assertTrue(all(child.disabled for child in view.children))
        cancel_view = self.channel.send.call_args.kwargs["view"]
        self.assertFalse(cancel_view.children[0].disabled)
        await task
        for message in (self.staff_message, self.ticket_message):
            self.assertTrue(all(child.disabled for child in message.edit.call_args.kwargs["view"].children))
        self.assertFalse(self.db.get_report_by_id(7)["closure_pending"])

    async def test_cancellation_restores_only_available_actions(self):
        task = await self.start_closure()
        self.manager.pending[(1, 7)].cancelled.set()
        await task
        staff = self.staff_message.edit.call_args.kwargs["view"]
        self.assertFalse(staff.resolved.disabled)
        self.assertFalse(staff.not_resolved.disabled)
        self.assertTrue(staff.claimed.disabled)
        self.assertTrue(staff.open_ticket.disabled)
        self.assertTrue(all(not child.disabled for child in self.ticket_message.edit.call_args.kwargs["view"].children))

    async def test_notice_failure_restores_controls(self):
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No permission")
        with self.assertLogs("bot.closure", level="ERROR"):
            await self.run_closure()
        self.assertFalse(self.staff_message.edit.call_args.kwargs["view"].resolved.disabled)
        self.assertFalse(self.db.get_report_by_id(7)["closure_pending"])
        self.assertFalse(any(field.name.startswith("Pending closure") for field in self.staff_message.embeds[0].fields))

    async def test_button_edit_failure_still_prevents_duplicate_closure(self):
        self.staff_message.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "No permission")
        with self.assertLogs("bot.closure", level="ERROR"):
            task = await self.start_closure()
            await self.run_closure()
            await task
        self.finalize.assert_awaited_once()

    async def test_cache_miss_fetches_channels_and_restores_callbacks(self):
        self.interaction.guild.get_channel.side_effect = lambda cid: None
        self.interaction.guild.fetch_channel = AsyncMock(
            side_effect=lambda cid: self.staff_channel if cid == 22 else self.channel)
        task = await self.start_closure()
        self.manager.pending[(1, 7)].cancelled.set()
        await task
        self.interaction.guild.fetch_channel.assert_any_await(22)
        self.interaction.guild.fetch_channel.assert_any_await(42)
        for message in (self.staff_message, self.ticket_message):
            view = message.edit.call_args.kwargs["view"]
            self.assertTrue(view.is_persistent())
            self.assertTrue(all(child.callback is not None for child in view.children))

    async def test_stale_buttons_reject_pending_and_closed_reports(self):
        self.interaction.client = self.bot
        self.interaction.channel = SimpleNamespace(id=22, topic="report_id=7")
        self.interaction.message = SimpleNamespace(id=33)
        self.interaction.response = SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock())
        self.db.get_by_staff_message_id.side_effect = lambda mid: self.db.get_report_by_id(7)
        for status, pending in [("Ticket Open", True), ("Resolved", False)]:
            self.db.get_report_by_id.return_value.update(status=status, closure_pending=pending)
            for view_type in (ReportActionView, TicketResolveView):
                view = view_type(self.db, 22, 0, False, 0)
                for button in view.children:
                    await button.callback(self.interaction)
        self.interaction.response.send_modal.assert_not_awaited()
        self.assertEqual(self.interaction.response.send_message.await_count, 12)

    async def test_reconnect_restores_interrupted_controls_but_keeps_running_timer(self):
        db = ReportDB(":memory:")
        self.addCleanup(db.conn.close)
        rid = db.create_report("TV", 5, 1, 22, {})
        db.set_staff_message_id(rid, 33)
        db.set_ticket_channel_id(rid, 42)
        db.update_status(rid, "Ticket Open")
        db.set_closure_pending(rid, True)
        for message in (self.staff_message, self.ticket_message):
            message.embeds[0].add_field(name="Pending closure", value="Resolved requested by <@55>")
            message.embeds[0].add_field(name="Pending closure details", value="Old attempt")
        self.bot.get_guild = Mock(return_value=self.interaction.guild)
        self.manager.pending[(1, rid)] = object()
        await self.manager.recover_interrupted(db)
        self.assertTrue(db.get_report_by_id(rid)["closure_pending"])
        self.staff_message.edit.assert_not_awaited()
        self.manager.pending.clear()
        await self.manager.recover_interrupted(db)
        self.assertFalse(db.list_pending_closures())
        self.assertFalse(self.staff_message.edit.call_args.kwargs["view"].resolved.disabled)
        self.assertTrue(self.staff_message.edit.call_args.kwargs["view"].open_ticket.disabled)
        for message in (self.staff_message, self.ticket_message):
            self.assertFalse(any(field.name.startswith("Pending closure") for field in message.embeds[0].fields))

    async def test_closure_preview_visible_then_removed_by_both_cancellation_methods(self):
        def snapshots():
            result = []
            for message in (self.staff_message, self.ticket_message):
                data = deepcopy(message.embeds[0].to_dict())
                data.setdefault("fields", [])
                result.append(data)
            return result

        for outcome in ("Resolved", "Not Resolved"):
            for cancel_method in ("button", "message"):
                with self.subTest(outcome=outcome, cancel_method=cancel_method):
                    originals = snapshots()
                    note = "Checked the stream.\nAudio is now working." if outcome == "Resolved" else "Cannot reproduce the audio issue."
                    task = await self.start_closure(outcome=outcome, details=note)
                    for message in (self.staff_message, self.ticket_message):
                        fields = {field.name: field.value for field in message.embeds[0].fields}
                        self.assertIn(f"**{outcome}**", fields["Pending closure"])
                        self.assertIn("<@55>", fields["Pending closure"])
                        self.assertEqual(fields["Pending closure details"], note)
                    self.assertEqual(self.db.get_report_by_id(7)["status"], "Ticket Open")
                    if cancel_method == "button":
                        view = self.channel.send.call_args.kwargs["view"]
                        await view.children[0].callback(SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock())))
                    else:
                        await self.manager.on_message(SimpleNamespace(author=SimpleNamespace(id=5), channel=SimpleNamespace(id=42)))
                    await task
                    self.finalize.assert_not_awaited()
                    self.assertEqual(snapshots(), originals)

    async def test_duplicate_attempt_does_not_replace_preview(self):
        task = await self.start_closure(outcome="Resolved", details="Stream fixed")
        await self.run_closure(outcome="Not Resolved", details="Conflicting attempt")
        fields = {field.name: field.value for field in self.staff_message.embeds[0].fields}
        self.assertIn("**Resolved**", fields["Pending closure"])
        self.assertEqual(fields["Pending closure details"], "Stream fixed")
        self.manager.pending[(1, 7)].cancelled.set()
        await task

    async def test_successful_closure_keeps_final_embed_and_removes_ticket_preview(self):
        async def finalize(interaction):
            self.db.get_report_by_id.return_value.update(status="Resolved")
            embed = discord.Embed(title="Report #7")
            embed.add_field(name="Status", value="Resolved")
            embed.add_field(name="Resolved by", value="<@55>")
            embed.add_field(name="Resolution details", value="Stream fixed")
            self.staff_message.embeds = [embed]

        self.finalize.side_effect = finalize
        await self.run_closure(details="Stream fixed")
        fields = {field.name: field.value for field in self.staff_message.embeds[0].fields}
        self.assertEqual(fields, {"Status": "Resolved", "Resolved by": "<@55>", "Resolution details": "Stream fixed"})
        self.assertFalse(any(field.name.startswith("Pending closure") for field in self.ticket_message.embeds[0].fields))

    async def test_empty_optional_note_has_clear_placeholder(self):
        task = await self.start_closure(details="  ")
        fields = {field.name: field.value for field in self.staff_message.embeds[0].fields}
        self.assertEqual(fields["Pending closure details"], "No details provided.")
        self.manager.pending[(1, 7)].cancelled.set()
        await task
