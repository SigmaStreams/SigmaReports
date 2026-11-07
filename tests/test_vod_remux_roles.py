import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.modals import (
    _can_report_vod_remux,
    _new_vod_state,
    _VODLanguageQuestionView,
    _VODReviewView,
)


def interaction(role_ids):
    return SimpleNamespace(
        user=SimpleNamespace(roles=[SimpleNamespace(id=role_id) for role_id in role_ids]),
        response=SimpleNamespace(edit_message=AsyncMock()),
    )


class RemuxRoleTests(unittest.TestCase):
    def test_either_approved_role_grants_access(self):
        for roles in ([1526787579641462844], [1559995916717654048],
                      [1526787579641462844, 1559995916717654048],
                      [123, 1559995916717654048]):
            with self.subTest(roles=roles):
                self.assertTrue(_can_report_vod_remux(interaction(roles)))

    def test_other_roles_do_not_grant_access(self):
        for roles in ([], [123], [123, 456], [1526787579641462843], [1559995916717654049]):
            with self.subTest(roles=roles):
                self.assertFalse(_can_report_vod_remux(interaction(roles)))

    def test_user_without_member_roles_has_no_access(self):
        self.assertFalse(_can_report_vod_remux(SimpleNamespace(user=SimpleNamespace())))


class RemuxSelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_and_review_selectors_require_approved_role(self):
        # A stale legacy config must not authorize another role.
        cfg = SimpleNamespace(ss_vod_remux_role_id=123)
        standard = ["HD", "4K", "Both"]
        remux = ["Remux", "4K Remux", "Both Remux"]
        for role_ids, allowed in (([1526787579641462844], True),
                                  ([1559995916717654048], True), ([123], False), ([], False)):
            for editing in (False, True):
                with self.subTest(roles=role_ids, editing=editing):
                    state = _new_vod_state()
                    result = interaction(role_ids)
                    if editing:
                        view = _VODReviewView(None, cfg, 1, state)
                        await view.handle_edit(result, "4k")
                    else:
                        view = _VODLanguageQuestionView(None, cfg, 1, state)
                        await view.handle_selection(result, "Standard")
                    selector = result.response.edit_message.call_args.kwargs["view"]
                    values = [option.value for child in selector.children
                              if hasattr(child, "options") for option in child.options]
                    self.assertEqual(values, standard + remux if allowed else standard)
