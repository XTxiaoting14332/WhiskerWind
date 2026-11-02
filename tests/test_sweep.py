import unittest
from unittest import mock

from qqcache import sweep
from qqcache.sweep import Row, Sweeper, parse_row


def nodes(*items):
    return [(role, name, cls) for role, name, cls in items]


class ParseRowTest(unittest.TestCase):
    def test_group_row_with_badge(self):
        row = parse_row(nodes(
            ("panel", "", "recent-contact-item"),
            ("section", "", "item__info"),
            ("static", "files", ""),
            ("static", "12:10", ""),
            ("static", "【示例】Alice：", ""),
            ("static", "#ww", ""),
            ("section", "1条未读", ""),
            ("static", "1", ""),
        ))
        self.assertEqual(row, Row("files", "12:10", "【示例】Alice：#ww", 1, False))

    def test_muted_private_row(self):
        row = parse_row(nodes(
            ("static", "天家鸡玩网费吧", ""), ("static", "12:14", ""), ("static", "你好", ""),
            ("section", "免打扰", "q-svg-icon q-icon no-disturb-icon"),
        ))
        self.assertEqual((row.name, row.preview, row.unread, row.muted),
                         ("天家鸡玩网费吧", "你好", 0, True))

    def test_badge_digits_not_taken_as_preview(self):
        row = parse_row(nodes(("static", "Moyuu AI", ""), ("static", "12:14", ""),
                              ("static", "hi", ""), ("section", "99条未读", ""),
                              ("static", "99+", "")))
        self.assertEqual((row.preview, row.unread), ("hi", 99))

    def test_emoji_name_split_into_nodes(self):
        row = parse_row(nodes(("static", "💙", ""), ("static", "bakaHub", ""),
                              ("static", "12:18", ""), ("static", "mød：", ""),
                              ("static", "[动画表情]", ""), ("section", "2条未读", "")))
        self.assertEqual((row.name, row.time, row.preview), ("💙bakaHub", "12:18", "mød：[动画表情]"))
        row = parse_row(nodes(("static", "旧群", ""), ("static", "2023/03/29", "")))
        self.assertEqual((row.name, row.time), ("旧群", "2023/03/29"))
        row = parse_row(nodes(("static", "低血压治疗基地三号", ""), ("static", "昨天23:15", ""),
                              ("static", "-：", ""), ("static", "[视频]", "")))
        self.assertEqual((row.name, row.time, row.preview), ("低血压治疗基地三号", "昨天23:15", "-：[视频]"))

    def test_empty(self):
        self.assertIsNone(parse_row(nodes(("section", "", ""))))


class SweeperTest(unittest.TestCase):
    def test_picks_only_notifying_unmuted_unread_not_current(self):
        rows = [Row("files", "12:08", "#ww", 1), Row("Moyuu AI", "12:14", "x", 99),
                Row("muted", "12:14", "x", 3, muted=True), Row("Bob", "12:09", "y", 2),
                Row("read", "12:00", "z", 0)]
        got = Sweeper().pick(rows, {"files", "muted", "Bob", "read"}, current="Bob")
        self.assertEqual([r.name for r in got], ["files"])

    def test_same_signature_not_retriggered_new_message_is(self):
        s = Sweeper()
        r = Row("files", "12:08", "#ww", 1)
        self.assertEqual(len(s.pick([r], {"files"})), 1)
        self.assertEqual(s.pick([r], {"files"}), [])          # 打开失败，未读还挂着
        r2 = Row("files", "12:09", "又一条", 2)
        self.assertEqual(len(s.pick([r2], {"files"})), 1)

    def test_forget_allows_retrigger_of_same_row(self):
        s = Sweeper()
        r = Row("files", "12:08", "#ww", 1)
        s.pick([r], {"files"})
        s.forget("files")
        self.assertEqual(len(s.pick([r], {"files"})), 1)

    def test_sweep_swallows_read_errors(self):
        with mock.patch.object(sweep, "read_rows", side_effect=RuntimeError("atk")):
            self.assertIsNone(sweep.sweep(None, Sweeper(), {"files"}))


class BridgeSweepTest(unittest.TestCase):
    def _bridge(self):
        from qqcache import serve
        b = serve.Bridge.__new__(serve.Bridge)
        b.sweeper = Sweeper()
        b.notified_names = lambda: {"files"}
        b._sweep_pending = True
        b._last_sweep = None
        b._app = lambda: object()
        b.on_notification = mock.Mock()
        return b, serve

    def test_missed_row_goes_through_on_notification(self):
        b, serve = self._bridge()
        rows = [Row("files", "12:08", "#ww", 1)]
        with mock.patch.object(serve, "current_conversation", return_value="Bob"), \
             mock.patch.object(sweep, "read_rows", return_value=rows), \
             mock.patch.object(serve.threading, "Thread") as T:
            b._sweep_unread("test")
        T.assert_called_once()
        self.assertEqual(T.call_args.kwargs["args"], ("files",))

    def test_full_sweep_ignores_last_sweep_time(self):
        b, serve = self._bridge()
        b._last_sweep = 1000.0
        with mock.patch.object(serve, "current_conversation", return_value="Bob"), \
             mock.patch.object(sweep, "read_rows", return_value=[]) as rr:
            self.assertTrue(b._sweep_unread("test"))
            self.assertEqual(rr.call_args.kwargs["since"], 940.0)
            self.assertTrue(b._sweep_unread("test", full=True))
            self.assertIsNone(rr.call_args.kwargs["since"])

    def test_unreadable_list_keeps_sweep_pending(self):
        b, serve = self._bridge()
        b._sweep_full = True
        b._sweep_reason = "处理失败后"
        b.gui_lock = serve.TrackedRLock()
        b._defocus = lambda: False
        b._schedule_defocus = mock.Mock()
        with mock.patch.object(serve, "current_conversation", return_value="Bob"), \
             mock.patch.object(sweep, "read_rows", side_effect=RuntimeError("atk")):
            b._defocus_when_idle()
        self.assertTrue(b._sweep_pending)
        self.assertTrue(b._sweep_full)
        b._schedule_defocus.assert_called_once_with(delay=30.0)

    def test_give_up_schedules_bounded_recovery_sweeps(self):
        b, serve = self._bridge()
        b._recover_tries = {}
        b.sweeper._acted["files"] = ("12:05", "#ww", 1)
        with mock.patch.object(serve.threading, "Timer") as T:
            for _ in range(len(b.RECOVER_DELAYS) + 1):
                b._give_up("files")
        delays = [c.args[0] for c in T.call_args_list]
        self.assertEqual(delays, list(b.RECOVER_DELAYS))
        self.assertEqual(T.call_args.kwargs.get("args") or T.call_args.args[2],
                         ("处理失败后", True))
        self.assertNotIn("files", b.sweeper._acted)
        self.assertNotIn("files", b._recover_tries)   # 用尽后清零，新消息重新计数

    def test_relogin_requests_sweep(self):
        b, serve = self._bridge()
        b._sweep_pending = False
        b.request_sweep = mock.Mock()
        b.publish = mock.Mock()
        with mock.patch.object(serve, "_login_state", return_value="logged_in"):
            serve.heartbeat_tick(b, 5, "login_required")
            b.request_sweep.assert_called_once()
            serve.heartbeat_tick(b, 5, None)     # 启动首拍不算重新登录
            b.request_sweep.assert_called_once()


class ReadRowsTest(unittest.TestCase):
    """列表停在置顶/普通交界处：交界可见就零滚动；整屏置顶往下翻，整屏
    普通往上翻；碰到比上次巡检更早的普通会话就停。pages 是按滚动位置排列
    的可见窗口。"""

    def _run(self, pages, start=0, since=None, regular=12):
        state = {"page": start, "moves": []}

        def invoke(_lst, action):
            delta = 1 if action == "scrollForward" else -1
            nxt = state["page"] + delta
            if 0 <= nxt < len(pages):
                state["page"] = nxt
                state["moves"].append(delta)
            return True

        with mock.patch("qqcache.send.conversation_list", return_value="lst"), \
             mock.patch.object(sweep, "_visible", side_effect=lambda lst: pages[state["page"]]), \
             mock.patch.object(sweep.atspi, "invoke", side_effect=invoke), \
             mock.patch.object(sweep, "_first_row_name",
                               side_effect=lambda app: pages[state["page"]][0].name), \
             mock.patch.object(sweep.time, "sleep"):
            rows = sweep.read_rows(None, since=since, regular=regular)
        return [r.name for r in rows if not r.pinned], state

    P = [Row(f"p{i}", "2023/03/29", pinned=True) for i in range(8)]

    def test_boundary_visible_no_scrolling(self):
        page = self.P[-2:] + [Row("files", "12:38", "#ww", 1), Row("Bob", "12:21")]
        names, state = self._run([page], regular=2)
        self.assertEqual((names, state["moves"]), (["files", "Bob"], []))

    def test_all_pinned_pages_down_to_boundary(self):
        pages = [self.P[:4], self.P[4:], self.P[-1:] + [Row("files", "12:38", "#ww", 1)]]
        names, state = self._run(pages, regular=1)
        self.assertEqual((names, state["moves"]), (["files"], [1, 1]))

    def test_all_regular_pages_back_up_to_boundary(self):
        pages = [self.P[-1:] + [Row("files", "12:38", "", 1)], [Row("old", "09:00")]]
        names, state = self._run(pages, start=1, regular=1)
        self.assertEqual((names, state["moves"]), (["files"], [-1]))

    def test_extra_paging_returns_to_boundary(self):
        pages = [self.P[-1:] + [Row("a", "12:40")], [Row("b", "12:39"), Row("c", "12:38")]]
        names, state = self._run(pages, regular=3)
        self.assertEqual((names, state["moves"], state["page"]), (["a", "b", "c"], [1, -1], 0))
        # 平时（有 since）最多多翻一步
        pages = [self.P[-1:] + [Row("a", "12:40")], [Row("b", "12:40")], [Row("c", "12:40")]]
        names, state = self._run(pages, since=sweep.row_time("12:30"))
        self.assertEqual((names, state["moves"]), (["a", "b"], [1, -1]))

    def test_stops_at_rows_older_than_last_sweep(self):
        page = self.P[-1:] + [Row("a", "12:40", unread=1), Row("files", "12:38", unread=1)]
        names, state = self._run([page, [Row("x", "12:00")]], since=sweep.row_time("12:39"))
        self.assertEqual((names, state["moves"]), (["a"], []))

    def test_row_time(self):
        import datetime
        now = datetime.datetime(2026, 11, 2, 0, 5).timestamp()
        self.assertEqual(datetime.datetime.fromtimestamp(sweep.row_time("昨天23:59", now)),
                         datetime.datetime(2026, 11, 1, 23, 59))
        self.assertEqual(sweep.row_time("星期三", now), 0.0)


class NotifiedSummariesTest(unittest.TestCase):
    def test_mention_only_conversations_excluded(self):
        import os
        import tempfile
        from types import SimpleNamespace
        from qqcache.notify import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "n.sqlite3"))
        def add(summary, mention):
            st.add(SimpleNamespace(received_at=__import__("time").time(), app="QQ",
                                   summary=summary, body="", urgency=1, mention=mention,
                                   is_group=True, sender_card=None, sender_name=None,
                                   content="", truncated=False, replaces_id=0))
        add("files", False)
        add("某技术群", True)
        add("files", True)
        self.assertEqual(st.notified_summaries(), {"files"})


class WiringTest(unittest.TestCase):
    def test_serve_wires_notified_names_and_notify_does_not(self):
        import ast
        import inspect
        from qqcache import __main__ as m
        def assigns(fn):
            tree = ast.parse(inspect.getsource(fn))
            return any(isinstance(n, ast.Attribute) and n.attr == "notified_names"
                       for n in ast.walk(tree))
        self.assertTrue(assigns(m.cmd_serve))
        self.assertFalse(assigns(m.cmd_notify))


if __name__ == "__main__":
    unittest.main()
