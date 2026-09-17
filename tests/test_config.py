import os
import unittest
from unittest.mock import patch

from bot.config import load_config


class TicketCloseConfigTests(unittest.TestCase):
    def config(self, value=None):
        env = {
            "DISCORD_TOKEN": "test-token", "STAFF_CHANNEL_ID": "1",
            "REPORTS_CHANNEL_IDS": "2", "STAFF_ROLE_ID": "3",
            "SS_VOD_REMUX_ROLE_ID": "4", "PUBLIC_UPDATES": "false",
        }
        if value is not None:
            env["TICKET_CLOSE_DELAY_MINUTES"] = value
        with patch.dict(os.environ, env, clear=True):
            return load_config()

    def test_default_is_ten_minutes(self):
        self.assertEqual(self.config().ticket_close_delay_minutes, 10)

    def test_custom_minutes(self):
        self.assertEqual(self.config("5").ticket_close_delay_minutes, 5)

    def test_invalid_minutes_fail_with_clear_error(self):
        for value in ("0", "-1", "abc", "1.5", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "TICKET_CLOSE_DELAY_MINUTES"):
                    self.config(value)
