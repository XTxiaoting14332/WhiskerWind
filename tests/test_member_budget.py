import unittest
from unittest import mock

from qqcache import peer, serve


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class MemberBudgetTest(unittest.TestCase):
    def _bridge(self):
        b = serve.Bridge.__new__(serve.Bridge)
        b.member_attempted = {}
        b.ids = mock.Mock()
        b.ids.member_is_ambiguous.return_value = False
        b.cache = None
        b._msg_uins = {}
        b._apply_member_uin = mock.Mock()
        b._check_member_ambiguous = mock.Mock()
        return b

    def test_list_path_at_most_once_per_batch_and_budgeted(self):
        clock = Clock()
        b = self._bridge()
        calls = []

        def avatar_fails(app, node):
            clock.t += 1.0
            raise peer.PeerError("头像在视口外")

        def via_list(app, name):
            calls.append(name)
            clock.t += 3.0
            raise peer.PeerError("资料卡没有出现")

        def back(*a):
            clock.t += 2.0

        pending = {"A": ["m1"], "B": ["m2"], "C": ["m3"]}
        with mock.patch.object(serve.time, "monotonic", clock), \
             mock.patch.object(serve, "_main_window_id"), \
             mock.patch.object(serve.peer, "harvest_member_cards", return_value={}), \
             mock.patch.object(serve.backfill, "find_item_node", return_value=object()), \
             mock.patch.object(serve.peer, "member_uin", side_effect=avatar_fails), \
             mock.patch.object(serve.peer, "member_uin_via_list", side_effect=via_list), \
             mock.patch.object(serve, "send_open", side_effect=back):
            b._resolve_members(None, "1", pending)
        self.assertEqual(calls, ["A"])          # 私聊路径只走一次
        self.assertLessEqual(clock.t, b.MEMBER_BUDGET)

    def test_list_path_skipped_when_budget_low(self):
        clock = Clock()
        b = self._bridge()

        def slow_avatar(app, node):
            clock.t += 6.0
            raise peer.PeerError("成员资料卡没有出现")

        with mock.patch.object(serve.time, "monotonic", clock), \
             mock.patch.object(serve, "_main_window_id"), \
             mock.patch.object(serve.peer, "harvest_member_cards", return_value={}), \
             mock.patch.object(serve.backfill, "find_item_node", return_value=object()), \
             mock.patch.object(serve.peer, "member_uin", side_effect=slow_avatar), \
             mock.patch.object(serve.peer, "member_uin_via_list") as via_list, \
             mock.patch.object(serve, "send_open"):
            b._resolve_members(None, "1", {"A": ["m1"]})
        via_list.assert_not_called()           # 只剩 9s < 10s，不上私聊路径


if __name__ == "__main__":
    unittest.main()
