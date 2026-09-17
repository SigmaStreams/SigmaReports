import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot.closure import TicketClosureManager


class TicketClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.manager = TicketClosureManager(self.bot, delay=0.01)
        self.notice = SimpleNamespace(edit=AsyncMock())
        self.channel = SimpleNamespace(send=AsyncMock(return_value=self.notice))
        self.interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_channel=Mock(return_value=self.channel)),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.db = Mock()
        self.db.get_report_by_id.return_value = {"status": "Ticket Open", "ticket_channel_id": 42}
        self.finalize = AsyncMock()

    async def run_closure(self):
        await self.manager.run(self.interaction, self.db, 7, self.finalize)

    async def start_closure(self):
        task = asyncio.create_task(self.run_closure())
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
