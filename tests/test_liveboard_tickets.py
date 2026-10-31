import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from bot.cogs.liveboard import CLOSED_STATUSES, LiveboardCog
from bot.db import ReportDB
from bot.views import ReportActionView


class LiveboardTicketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = ReportDB(':memory:')
        self.addCleanup(self.db.conn.close)
        self.rid = self.db.create_report('TV', 11, 1, 22, {'channel_name': 'News'})
        self.db.set_staff_message_id(self.rid, 33)
        self.db.claim_report(self.rid, 55)
        self.claimed_at = self.db.get_report_by_id(self.rid)['claimed_at']

        self.board_message = SimpleNamespace(edit=AsyncMock())
        board_channel = Mock(spec=discord.TextChannel)
        board_channel.fetch_message = AsyncMock(return_value=self.board_message)
        self.db.set_liveboard(1, 88, 99)
        self.ticket = Mock(spec=discord.TextChannel)
        self.ticket.id, self.ticket.mention = 77, '<#77>'
        self.ticket.send = AsyncMock()
        self.guild = SimpleNamespace(
            id=1, get_channel=lambda cid: board_channel if cid == 88 else None,
            me=Mock(spec=discord.Member), default_role=Mock(spec=discord.Role),
            create_text_channel=AsyncMock(return_value=self.ticket),
        )
        self.guild.me.guild_permissions = SimpleNamespace(manage_channels=True)
        # Avoid starting the periodic task; exercise the real update directly.
        self.liveboard = LiveboardCog.__new__(LiveboardCog)
        self.liveboard.db = self.db
        self.liveboard.cfg = SimpleNamespace(staff_channel_id=22)
        reporter = Mock(spec=discord.User)
        reporter.id, reporter.mention = 11, '<@11>'
        self.bot = SimpleNamespace(
            get_guild=lambda gid: self.guild,
            get_cog=lambda name: self.liveboard if name == 'LiveboardCog' else None,
            fetch_user=AsyncMock(return_value=reporter),
        )
        self.liveboard.bot = self.bot
        self.interaction = SimpleNamespace(
            client=self.bot, guild=self.guild, user=SimpleNamespace(id=66),
            channel=SimpleNamespace(id=22, mention='<#22>'),
            message=SimpleNamespace(id=33),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.view = ReportActionView(self.db, 22, 0, False, 0)

    def board_text(self):
        embed = self.board_message.edit.call_args.kwargs['embed']
        return '\n'.join(field.value for field in embed.fields)

    async def test_opening_ticket_refreshes_claimed_report_immediately(self):
        await self.liveboard.update_liveboard(1)
        self.assertIn('`Claimed`', self.board_text())

        await self.view.open_ticket.callback(self.interaction)

        report = self.db.get_report_by_id(self.rid)
        self.assertEqual(report['status'], 'Ticket Open')
        self.assertEqual(report['ticket_channel_id'], 77)
        self.assertEqual(report['claimed_by_user_id'], 55)
        self.assertEqual(report['claimed_at'], self.claimed_at)
        self.assertEqual(self.board_message.edit.await_count, 2)
        self.assertIn('`Ticket Open`', self.board_text())
        self.assertNotIn('`Claimed`', self.board_text())
        fields = {f.name: f.value for f in self.interaction.response.edit_message.call_args.kwargs['embed'].fields}
        self.assertEqual(fields['Status'], 'Ticket Open')

    async def test_existing_ticket_overrides_stale_open_or_claimed_status(self):
        for report_type in ('TV', 'VOD'):
            for status in ('Open', 'Claimed'):
                with self.subTest(report_type=report_type, status=status):
                    rid = self.db.create_report(report_type, 11, 1, 22, {'title': 'Movie'})
                    self.db.update_status(rid, status)
                    self.db.set_ticket_channel_id(rid, 77)
                    await self.liveboard.update_liveboard(1)
                    self.assertIn(f'**#{rid}** • `Ticket Open`', self.board_text())

    async def test_report_without_ticket_stays_claimed(self):
        await self.liveboard.update_liveboard(1)
        self.assertIn('`Claimed`', self.board_text())
        self.assertNotIn('`Ticket Open`', self.board_text())

    async def test_closed_reports_with_ticket_are_removed(self):
        self.db.set_ticket_channel_id(self.rid, 77)
        for status in CLOSED_STATUSES:
            self.db.update_status(self.rid, status)
            await self.liveboard.update_liveboard(1)
            self.assertNotIn(f'**#{self.rid}**', self.board_text())

    async def test_failed_welcome_message_keeps_ticket_status(self):
        self.ticket.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'Missing permissions')
        with self.assertRaises(discord.Forbidden):
            await self.view.open_ticket.callback(self.interaction)
        self.assertEqual(self.db.get_report_by_id(self.rid)['status'], 'Ticket Open')
        await self.liveboard.update_liveboard(1)
        self.assertIn('`Ticket Open`', self.board_text())

    async def test_failed_channel_creation_keeps_claimed_status(self):
        self.guild.create_text_channel.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'Missing permissions')
        await self.view.open_ticket.callback(self.interaction)
        report = self.db.get_report_by_id(self.rid)
        self.assertEqual(report['status'], 'Claimed')
        self.assertIsNone(report['ticket_channel_id'])

    async def test_refresh_failure_does_not_fail_ticket_creation(self):
        self.liveboard.update_liveboard = AsyncMock(side_effect=RuntimeError('Offline'))
        with self.assertLogs('bot.views', level='ERROR'):
            await self.view.open_ticket.callback(self.interaction)
        self.assertEqual(self.db.get_report_by_id(self.rid)['status'], 'Ticket Open')
        self.interaction.followup.send.assert_awaited_once()
