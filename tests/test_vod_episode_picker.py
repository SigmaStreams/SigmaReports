import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.modals import (_new_vod_state, _VODEpisodePickerView, _VODDetailsModal,
                        _start_vod_episode_picker, _VOD4KQuestionView, _VODReviewView)
from bot.tvdb import list_tvdb_seasons, list_tvdb_season_episodes


def interaction():
    return SimpleNamespace(response=SimpleNamespace(
        defer=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock())


class TVDBChoicesTests(unittest.TestCase):
    @patch('bot.tvdb._tvdb_request')
    def test_seasons_only_official_sorted_deduplicated(self, request):
        request.side_effect = [{'data': {'token': 'token'}}, {'data': {'seasons': [
            {'id': 3, 'number': 2, 'type': {'type': 'official'}},
            {'id': 4, 'number': 1, 'type': {'type': 'dvd'}},
            {'id': 5, 'number': 0, 'type': {'type': 'official'}},
            {'id': 3, 'number': 2, 'type': {'type': 'official'}},
        ]}}]
        self.assertEqual(list_tvdb_seasons('key', '123'), [{'id': '5', 'number': 0}, {'id': '3', 'number': 2}])

    @patch('bot.tvdb._tvdb_request')
    def test_episodes_complete_season_sorted(self, request):
        request.side_effect = [{'data': {'token': 'token'}}, {'data': {'episodes': [
            {'id': n, 'number': n, 'name': f'Episode {n}'} for n in range(60, 0, -1)
        ]}}]
        episodes = list_tvdb_season_episodes('key', '5')
        self.assertEqual(len(episodes), 60)
        self.assertEqual(episodes[0]['number'], 1)
        self.assertIn('/seasons/5/extended', request.call_args.args[0])


class PickerFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = _new_vod_state()
        self.state.update(content_type='tv', source_id='123', title='Show')
        self.cfg = SimpleNamespace(tvdb_key='key')

    async def test_pagination_and_component_limits(self):
        choices = [{'number': n, 'episode_title': 'Long title ' * 30, 'episode_tvdb_id': str(n)} for n in range(1, 61)]
        self.state['season_number'] = 2
        seen = []
        for page in range(3):
            view = _VODEpisodePickerView(None, self.cfg, 1, self.state, choices, 'episode', page)
            for row in view.to_components():
                for component in row['components']:
                    options = component.get('options', [])
                    self.assertLessEqual(len(options), 25)
                    self.assertLessEqual(len(component.get('placeholder', '')), 100)
                    for option in options:
                        self.assertLessEqual(len(option['label']), 100)
                        if option['value'] != 'all':
                            seen.append(int(option['value']))
        self.assertEqual(seen, list(range(1, 61)))

    async def test_season_then_episode_then_details(self):
        view = _VODEpisodePickerView(None, self.cfg, 1, self.state, [{'number': 0, 'id': '9'}], 'season')
        first = interaction()
        with patch('bot.modals.list_tvdb_season_episodes', return_value=[
            {'number': 1, 'episode_title': 'Special', 'episode_tvdb_id': '10'}
        ]):
            await view.handle_selection(first, '0')
        first.response.defer.assert_awaited_once()
        episodes = first.edit_original_response.call_args.kwargs['view']
        second = interaction()
        await episodes.handle_selection(second, '1')
        modal = second.response.send_modal.call_args.args[0]
        self.assertEqual(len(modal.children), 2)
        self.assertEqual(modal.state['season_number'], 0)
        self.assertEqual(modal.state['episode_number'], 1)
        self.assertEqual(modal.state['episode_title'], 'Special')

    async def test_whole_show_and_review_edit(self):
        self.state.update(season_number=2, episode_number=3, episode_title='Old', _edit_vod_field='episodes')
        view = _VODEpisodePickerView(None, self.cfg, 1, self.state, [], 'season')
        result = interaction()
        await view.whole.callback(result)
        review = result.response.edit_message.call_args.kwargs['view']
        self.assertIsInstance(review, _VODReviewView)
        self.assertIsNone(review.state['season_number'])
        self.assertIsNone(review.state['episode_number'])
        self.assertEqual(review.state['episode_title'], '')

    async def test_whole_season_and_manual_fallback(self):
        self.state.update(season_number=2, episode_number=3, episode_title='Old')
        view = _VODEpisodePickerView(None, self.cfg, 1, self.state, [], 'episode')
        result = interaction()
        await view.whole.callback(result)
        modal = result.response.send_modal.call_args.args[0]
        self.assertEqual(modal.state['season_number'], 2)
        self.assertIsNone(modal.state['episode_number'])
        result = interaction()
        await view.manual.callback(result)
        self.assertTrue(result.response.send_modal.call_args.args[0].manual_numbers)

    async def test_outage_keeps_manual_entry_available(self):
        result = interaction()
        with patch('bot.modals.list_tvdb_seasons', side_effect=RuntimeError('offline')):
            await _start_vod_episode_picker(result, None, self.cfg, 1, self.state)
        view = result.edit_original_response.call_args.kwargs['view']
        self.assertEqual(view.choices, [])
        self.assertFalse(view.manual.disabled)

    async def test_version_routes_tv_to_picker_and_movie_to_details(self):
        for content_type in ('tv', 'movie'):
            self.state['content_type'] = content_type
            view = _VOD4KQuestionView(None, self.cfg, 1, self.state, include_remux=False)
            result = interaction()
            with patch('bot.modals.list_tvdb_seasons', return_value=[]):
                await view.handle_selection(result, 'HD')
            if content_type == 'tv':
                self.assertIsInstance(result.edit_original_response.call_args.kwargs['view'], _VODEpisodePickerView)
            else:
                self.assertIsInstance(result.response.send_modal.call_args.args[0], _VODDetailsModal)

    async def test_single_season_auto_selects_and_keeps_specials_accessible(self):
        for specials in ([], [{"number": 0, "id": "8"}]):
            seasons = specials + [{"number": 1, "id": "9"}]
            self.state.update(episode_number=5, episode_title="Old", episode_tvdb_id="old",
                              _edit_vod_field="episodes")
            result = interaction()
            with patch('bot.modals.list_tvdb_seasons', return_value=seasons), patch(
                'bot.modals.list_tvdb_season_episodes', return_value=[
                    {"number": 1, "episode_title": "Pilot", "episode_tvdb_id": "10"}
                ]
            ) as load:
                await _start_vod_episode_picker(result, None, self.cfg, 1, self.state)
            view = result.edit_original_response.call_args.kwargs['view']
            self.assertEqual(view.kind, 'episode')
            self.assertEqual(view.state['season_number'], 1)
            self.assertIsNone(view.state['episode_number'])
            self.assertEqual(view.state['episode_title'], '')
            self.assertEqual(view.state['_edit_vod_field'], 'episodes')
            load.assert_called_once_with('key', '9')
            back = interaction()
            await view.back.callback(back)
            season_view = back.response.edit_message.call_args.kwargs['view']
            self.assertEqual(season_view.kind, 'season')
            self.assertEqual(season_view.choices, seasons)

    async def test_other_season_lists_still_prompt_for_season(self):
        for seasons in ([], [{"number": 0, "id": "8"}], [{"number": 2, "id": "9"}],
                        [{"number": 1, "id": "9"}, {"number": 2, "id": "10"}]):
            result = interaction()
            with patch('bot.modals.list_tvdb_seasons', return_value=seasons), patch(
                'bot.modals.list_tvdb_season_episodes'
            ) as load:
                await _start_vod_episode_picker(result, None, self.cfg, 1, self.state)
            self.assertEqual(result.edit_original_response.call_args.kwargs['view'].kind, 'season')
            load.assert_not_called()

    async def test_single_season_episode_outage_allows_manual_entry(self):
        result = interaction()
        with patch('bot.modals.list_tvdb_seasons', return_value=[{"number": 1, "id": "9"}]), patch(
            'bot.modals.list_tvdb_season_episodes', side_effect=RuntimeError('offline')
        ):
            await _start_vod_episode_picker(result, None, self.cfg, 1, self.state)
        view = result.edit_original_response.call_args.kwargs['view']
        self.assertEqual(view.kind, 'episode')
        self.assertEqual(view.state['season_number'], 1)
        self.assertFalse(view.manual.disabled)

    async def test_whole_scope_button_is_separate_from_paginated_choices(self):
        for kind in ('season', 'episode'):
            choices = [{"number": n, "id": str(n)} for n in range(1, 52)]
            for page in range(3):
                view = _VODEpisodePickerView(None, self.cfg, 1, self.state, choices, kind, page)
                self.assertEqual(view.whole.label, 'Whole show' if kind == 'season' else 'Whole season')
                selects = [child for child in view.children if hasattr(child, 'options')]
                self.assertEqual(len(selects), 1)
                self.assertEqual(len(selects[0].options), 25 if page < 2 else 1)
                self.assertNotIn('all', [option.value for option in selects[0].options])
            empty = _VODEpisodePickerView(None, self.cfg, 1, self.state, [], kind)
            self.assertFalse(any(hasattr(child, 'options') for child in empty.children))
            self.assertFalse(empty.whole.disabled)

    async def test_multiple_episodes_opens_details_and_persists_scope(self):
        from bot.modals import _build_vod_payload
        from bot.utils import _vod_title_display, report_subject
        self.state.update(season_number=2, episode_number=3, episode_title='Old',
                          episode_tvdb_id='old', _edit_vod_field='episodes')
        view = _VODEpisodePickerView(None, self.cfg, 1, self.state, [], 'episode')
        result = interaction()
        await view.multiple.callback(result)
        modal = result.response.send_modal.call_args.args[0]
        self.assertEqual(len(modal.children), 2)
        self.assertEqual(modal.state['season_number'], 2)
        self.assertIsNone(modal.state['episode_number'])
        self.assertEqual(modal.state['episode_title'], '')
        self.assertLessEqual(len(modal.issue.placeholder), 100)
        await modal.on_submit(interaction())
        payload = _build_vod_payload(modal.state)
        self.assertEqual(payload['episode_scope'], 'multiple')
        self.assertIn('S02 (multiple episodes)', _vod_title_display(payload))
        self.assertIn('S02 (multiple episodes)', report_subject('vod', payload))
        self.assertNotIn('_edit_vod_field', modal.state)

    async def test_changing_selection_clears_multiple_scope(self):
        from bot.modals import _apply_vod_selected_item
        self.state.update(season_number=2, episode_scope='multiple')
        choices = [{'number': 3, 'episode_title': 'Third', 'episode_tvdb_id': '3'}]
        for value in ('all', '3'):
            view = _VODEpisodePickerView(None, self.cfg, 1, self.state, choices, 'episode')
            await view.handle_selection(interaction(), value)
            self.assertEqual(view.state['episode_scope'], '')
        _apply_vod_selected_item(self.state, {'title': 'Other', 'content_type': 'tv'})
        self.assertNotIn('episode_scope', self.state)

    async def test_multiple_button_only_appears_after_season_selection(self):
        for kind in ('season', 'episode'):
            view = _VODEpisodePickerView(None, self.cfg, 1, self.state, [], kind)
            self.assertEqual(view.multiple in view.children, kind == 'episode')
