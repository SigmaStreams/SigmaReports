import unittest
from unittest.mock import patch

from bot.tmdb import search_tmdb_movies
from bot.tvdb import search_tvdb_series
from bot.utils import _vod_title_display, report_subject


class VODTitleMetadataTests(unittest.TestCase):
    @patch("bot.tmdb._tmdb_get")
    def test_tmdb_keeps_original_title_and_adds_english_title(self, get):
        get.return_value = {
            "results": [
                {
                    "id": 1,
                    "title": "The Intouchables",
                    "original_title": "Intouchables",
                    "release_date": "2011-11-02",
                }
            ]
        }

        result = search_tmdb_movies("token", "Intouchables")

        self.assertEqual(result[0]["title"], "Intouchables")
        self.assertEqual(result[0]["english_title"], "The Intouchables")
        self.assertIn("language=en-US", get.call_args.args[0])

    @patch("bot.tvdb._tvdb_request")
    def test_tvdb_reads_english_title_from_search_translations(self, request):
        request.side_effect = [
            {"data": {"token": "token"}},
            {
                "data": [
                    {
                        "tvdb_id": "123",
                        "name": "La casa de papel",
                        "translations": {"eng": "Money Heist"},
                        "year": "2017",
                    }
                ]
            },
        ]

        result = search_tvdb_series("key", "La casa de papel")

        self.assertEqual(result[0]["title"], "La casa de papel")
        self.assertEqual(result[0]["english_title"], "Money Heist")

    def test_display_is_backward_compatible_and_adds_translation(self):
        self.assertEqual(_vod_title_display({"title": "Arrival"}), "Arrival")
        payload = {"title": "La casa de papel", "english_title": "Money Heist"}
        self.assertEqual(
            _vod_title_display(payload),
            "La casa de papel\n*English: Money Heist*",
        )
        self.assertEqual(
            report_subject("vod", payload),
            "La casa de papel / Money Heist",
        )


if __name__ == "__main__":
    unittest.main()
