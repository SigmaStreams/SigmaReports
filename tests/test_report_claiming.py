import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from bot.db import ReportDB
from bot.views import ReportActionView


class ReportClaimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = ReportDB(':memory:')
        self.addCleanup(self.db.conn.close)
        self.rid = self.db.create_report('TV', 11, 1, 22, {'channel_name': 'News', 'issue': 'No audio'})
        self.db.set_staff_message_id(self.rid, 33)
        self.view = ReportActionView(self.db, 22, 0, False, 0)
        self.responses = Mock(spec=discord.TextChannel)
        self.responses.send = AsyncMock()
        self.reporter = SimpleNamespace(id=11, mention='<@11>')
        self.interaction = SimpleNamespace(
            guild=SimpleNamespace(get_channel=lambda cid: self.responses if cid == 44 else None),
            channel=SimpleNamespace(id=22, mention='<#22>'),
            user=SimpleNamespace(id=55),
            message=SimpleNamespace(id=33, edit=AsyncMock()),
            client=SimpleNamespace(cfg=SimpleNamespace(responses_channel_id=44), get_user=lambda uid: self.reporter),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_claim_persists_and_notifies_once_without_closing(self):
        await self.view.claimed.callback(self.interaction)
        report = self.db.get_report_by_id(self.rid)
        self.assertEqual(report['status'], 'Claimed')
        self.assertEqual(report['claimed_by_user_id'], 55)
        self.assertTrue(report['claimed_at'])
        self.assertIsNone(report['ticket_channel_id'])
        self.assertIn(report, self.db.list_active_reports(1, {'Resolved', 'Not Resolved'}))
        edit = self.interaction.message.edit.call_args.kwargs
        fields = {field.name: field.value for field in edit['embed'].fields}
        self.assertEqual(fields['Status'], 'Claimed')
        self.assertIn('<@55>', fields['Claimed by'])
        self.assertTrue(edit['view'].claimed.disabled)
        self.assertFalse(edit['view'].open_ticket.disabled)
        self.assertFalse(self.view.claimed.disabled)  # Persistent fallback remains reusable.
        sent = self.responses.send.call_args.kwargs
        self.assertEqual(sent['content'], f'<@11>\nThe issue described in your report [#{self.rid} - News] has been confirmed. Please keep an eye on this channel and your DMs for updates.')
        self.assertEqual(sent['allowed_mentions'].to_dict()['users'], [11])
        await self.view.claimed.callback(self.interaction)
        self.responses.send.assert_awaited_once()

    async def test_ticket_and_closed_reports_cannot_be_claimed(self):
        for status, ticket in [('Ticket Open', 77), ('Resolved', None), ('Not Resolved', None), ('Open', 77)]:
            self.db.update_status(self.rid, status)
            self.db.set_ticket_channel_id(self.rid, ticket)
            await self.view.claimed.callback(self.interaction)
            self.assertIsNone(self.db.get_report_by_id(self.rid)['claimed_by_user_id'])
        self.responses.send.assert_not_awaited()

    async def test_refreshed_ticket_view_disables_claim(self):
        self.db.set_ticket_channel_id(self.rid, 77)
        self.view.apply_report_state(self.db.get_report_by_id(self.rid))
        self.assertTrue(self.view.claimed.disabled)
        self.assertTrue(self.view.open_ticket.disabled)
        self.assertFalse(self.view.resolved.disabled)

    async def test_ticket_preserves_existing_claimant_and_bulk_close_includes_claimed(self):
        self.assertTrue(self.db.claim_report(self.rid, 55))
        self.db.mark_claimed(self.rid, 66, 'later')
        self.assertEqual(self.db.get_report_by_id(self.rid)['claimed_by_user_id'], 55)
        self.assertEqual(self.db.close_open_reports(1), 1)
        self.assertEqual(self.db.get_report_by_id(self.rid)['status'], 'Resolved')

    async def test_missing_responses_channel_leaves_report_open(self):
        self.interaction.client.cfg.responses_channel_id = 0
        await self.view.claimed.callback(self.interaction)
        self.assertEqual(self.db.get_report_by_id(self.rid)['status'], 'Open')

    async def test_notification_failure_is_reported_to_staff(self):
        self.responses.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing permissions')
        await self.view.claimed.callback(self.interaction)
        self.assertIn('notification could not be sent', self.interaction.followup.send.call_args.args[0])

    async def test_non_staff_cannot_claim(self):
        self.view.staff_role_id = 99
        await self.view.claimed.callback(self.interaction)
        self.assertEqual(self.db.get_report_by_id(self.rid)['status'], 'Open')
        self.responses.send.assert_not_awaited()
