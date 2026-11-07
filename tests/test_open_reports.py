import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from bot.cogs.reports import Reports
from bot.db import ReportDB


class OpenReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = ReportDB(":memory:")
        self.addCleanup(self.db.conn.close)
        self.cog = Reports(None, self.db, SimpleNamespace(staff_channel_id=22, staff_role_id=7))
        member = Mock(spec=discord.Member)
        member.id = 55
        member.roles = [SimpleNamespace(id=7)]
        self.interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1), user=member,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
        )

    def add_report(self, report_type="TV", title="News", status="Open", guild_id=1):
        rid = self.db.create_report(
            report_type, 11, guild_id, 22, {"channel_name": title, "title": title},
        )
        self.db.update_status(rid, status)
        self.db.set_staff_message_id(rid, 1000 + rid)
        return rid

    async def list_reports(self):
        await self.cog.list_open_reports.callback(self.cog, self.interaction)
        return self.interaction.response.send_message.call_args.kwargs

    def assert_valid_pages(self, pages):
        for page in pages:
            self.assertLessEqual(len(page), 6000)
            self.assertLessEqual(len(page.fields), 25)
            for field in page.fields:
                self.assertGreater(len(field.value), 0)
                self.assertLessEqual(len(field.value), 1024)

    async def test_large_mixed_list_preserves_every_report_within_discord_limits(self):
        ids = []
        for report_type, count in (("TV", 80), ("VOD", 35), ("CUSTOM", 4)):
            for number in range(count):
                status = ("Open", "Claimed", "Ticket Open")[number % 3]
                ids.append(self.add_report(report_type, "Long title 電影 " * 20, status))
        result = await self.list_reports()
        self.assertTrue(result["ephemeral"])
        view = result["view"]
        self.assertGreater(len(view.pages), 1)
        self.assertIs(result["embed"], view.pages[0])
        self.assert_valid_pages(view.pages)
        text = "\n".join(field.value for page in view.pages for field in page.fields)
        for rid in ids:
            self.assertEqual(text.count(f"**#{rid}**"), 1)
            self.assertIn(f"[staff](https://discord.com/channels/1/22/{1000 + rid})", text)
        names = {field.name for page in view.pages for field in page.fields}
        self.assertEqual(names, {"TV (80)", "VOD (35)", "Other (4)"})
        for number, page in enumerate(view.pages, 1):
            self.assertEqual(page.footer.text, f"Page {number} of {len(view.pages)}")

    async def test_oversized_title_is_shortened_without_losing_id_or_staff_link(self):
        rid = self.add_report("VOD", "Movie " * 2000)
        result = await self.list_reports()
        self.assertIsNone(result["view"])
        self.assert_valid_pages([result["embed"]])
        row = result["embed"].fields[0].value
        self.assertIn(f"**#{rid}**", row)
        self.assertIn("…", row)
        self.assertTrue(row.endswith(f"[staff](https://discord.com/channels/1/22/{1000 + rid})"))

    async def test_exact_field_limit_and_following_report(self):
        rid = self.add_report(title="")
        report = self.db.get_report_by_id(rid)
        report["payload"]["channel_name"] = "x"
        overhead = len(self.cog._format_open_report_row(1, report)) - 1
        report["payload"]["channel_name"] = "x" * (1024 - overhead)
        next_id = self.add_report()
        pages = self.cog._build_open_report_pages(1, [report, self.db.get_report_by_id(next_id)])
        self.assert_valid_pages(pages)
        self.assertEqual(len(pages[0].fields[0].value), 1024)
        self.assertIn(f"**#{next_id}**", pages[0].fields[1].value)

    async def test_small_list_filters_closed_reports_and_other_servers(self):
        rid = self.add_report("vod", "Movie", "Claimed")
        closed = self.add_report(status="Resolved")
        other_guild = self.add_report(guild_id=2)
        result = await self.list_reports()
        self.assertIsNone(result["view"])
        self.assert_valid_pages([result["embed"]])
        text = result["embed"].fields[0].value
        self.assertEqual(result["embed"].fields[0].name, "VOD (1)")
        self.assertIn(f"**#{rid}**", text)
        self.assertNotIn(f"**#{closed}**", text)
        self.assertNotIn(f"**#{other_guild}**", text)

    async def test_pagination_buttons_show_all_pages_and_stop_at_boundaries(self):
        for _ in range(20):
            self.add_report(title="Long channel " * 100)
        result = await self.list_reports()
        view = result["view"]
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next_page.disabled)
        for page in range(1, len(view.pages)):
            await view.next_page.callback(self.interaction)
            self.assertEqual(view.page, page)
            self.assertIs(self.interaction.response.edit_message.call_args.kwargs["embed"], view.pages[page])
        self.assertTrue(view.next_page.disabled)
        await view.next_page.callback(self.interaction)
        self.assertEqual(view.page, len(view.pages) - 1)
        for _ in range(len(view.pages)):
            await view.previous.callback(self.interaction)
        self.assertEqual(view.page, 0)
        self.assertTrue(view.previous.disabled)

    async def test_pagination_only_accepts_original_requester(self):
        for _ in range(8):
            self.add_report(title="Long channel " * 100)
        view = (await self.list_reports())["view"]
        self.assertTrue(await view.interaction_check(self.interaction))
        self.interaction.user.id = 66
        self.assertFalse(await view.interaction_check(self.interaction))
        self.assertEqual(view.page, 0)

    async def test_empty_list(self):
        await self.list_reports()
        sent = self.interaction.response.send_message.call_args
        self.assertIn("No reports", sent.args[0])
        self.assertTrue(sent.kwargs["ephemeral"])

    async def test_non_staff_is_denied(self):
        self.interaction.user.roles = []
        await self.list_reports()
        self.assertIn("Not allowed", self.interaction.response.send_message.call_args.args[0])

    async def test_server_is_required(self):
        self.interaction.guild = None
        await self.list_reports()
        self.assertIn("must be used in a server", self.interaction.response.send_message.call_args.args[0])
