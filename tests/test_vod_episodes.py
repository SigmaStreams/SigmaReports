import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.modals import (_parse_vod_episode_numbers, _VODDetailsModal,
                        _new_vod_state, _build_vod_payload, _apply_vod_selected_item)
from bot.tvdb import lookup_tvdb_episode
from bot.utils import _vod_title_display, report_subject


class EpisodeTests(unittest.TestCase):
    def test_numbers_and_specials(self):
        self.assertEqual(_parse_vod_episode_numbers('0', '1'), (0, 1))
        self.assertEqual(_parse_vod_episode_numbers('2', ''), (2, None))
        self.assertEqual(_parse_vod_episode_numbers('', ''), (None, None))
        for season, episode in [('', '1'), ('-1', '1'), ('1', '0'), ('1', '2-4'), ('two', '')]:
            with self.assertRaises(ValueError):
                _parse_vod_episode_numbers(season, episode)

    def test_payload_display_and_title_change(self):
        state = _new_vod_state()
        state.update(title='Example', content_type='tv', season_number=0, episode_number=1)
        payload = _build_vod_payload(state)
        self.assertEqual(payload['season_number'], 0)
        self.assertIn('S00E01', _vod_title_display(payload))
        self.assertIn('S00E01', report_subject('vod', payload))
        _apply_vod_selected_item(state, {'title': 'Movie', 'content_type': 'movie'})
        self.assertNotIn('season_number', _build_vod_payload(state))
        self.assertEqual(_vod_title_display(state), 'Movie')

    @patch('bot.tvdb._tvdb_request')
    def test_tvdb_exact_match(self, request):
        request.side_effect = [{'data': {'token': 'token'}}, {'data': {'episodes': [
            {'id': 7, 'seasonNumber': 0, 'number': 1, 'name': 'Special'}]}}]
        result = lookup_tvdb_episode('key', '123', 0, 1)
        self.assertEqual(result, {'episode_tvdb_id': '7', 'episode_title': 'Special'})
        self.assertIn('season=0&episodeNumber=1', request.call_args.args[0])

    @patch('bot.tvdb._tvdb_request')
    def test_tvdb_does_not_attach_wrong_episode(self, request):
        request.side_effect = [{'data': {'token': 'token'}}, {'data': {'episodes': [
            {'id': 7, 'seasonNumber': 2, 'number': 3, 'name': 'Wrong'}]}}]
        self.assertIsNone(lookup_tvdb_episode('key', '123', 1, 3))


class EpisodeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_modal_placeholders_fit_discord_limit(self):
        for content_type in ("tv", "movie"):
            with self.subTest(content_type=content_type):
                state = _new_vod_state()
                state["content_type"] = content_type
                modal = _VODDetailsModal(None, None, 1, state, None)
                for row in modal.to_dict()["components"]:
                    for component in row["components"]:
                        self.assertLessEqual(len(component.get("placeholder", "")), 100)

    async def test_tv_fields_and_outage_fallback(self):
        state = _new_vod_state()
        state.update(content_type='tv', season_number=2, episode_number=3, title='Show')
        modal = _VODDetailsModal(None, SimpleNamespace(tvdb_key='key'), 1, state, None)
        self.assertEqual(len(modal.children), 4)
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                      edit_original_response=AsyncMock())
        with patch('bot.modals.lookup_tvdb_episode', side_effect=RuntimeError('offline')):
            await modal.on_submit(interaction)
        self.assertEqual(modal.state['season_number'], 2)
        self.assertEqual(modal.state['episode_number'], 3)
        interaction.edit_original_response.assert_awaited_once()

    async def test_movie_fields(self):
        state = _new_vod_state()
        state['content_type'] = 'movie'
        modal = _VODDetailsModal(None, None, 1, state, None)
        self.assertEqual(len(modal.children), 2)
