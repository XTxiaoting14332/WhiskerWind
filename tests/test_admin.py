import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from qqcache import ws11
from qqcache.logbuf import LogBuffer
from qqcache.ob11 import ApiError, Poster, WsHub, make_api_handler
from qqcache.transports import TransportManager


class LogBufferTest(unittest.TestCase):
    def test_snapshot_seq_and_tail(self):
        buf = LogBuffer(capacity=10)
        for i in range(5):
            buf.push("INFO", f"m{i}")
        self.assertEqual(buf.latest_seq, 5)
        self.assertEqual([r["message"] for r in buf.snapshot(tail=2)], ["m3", "m4"])
        self.assertEqual([r["message"] for r in buf.snapshot(after=3)], ["m3", "m4"])
        self.assertEqual([r["seq"] for r in buf.snapshot()], [1, 2, 3, 4, 5])

    def test_level_filter(self):
        buf = LogBuffer()
        buf.push("DEBUG", "d", levelno=10)
        buf.push("ERROR", "e", levelno=40)
        self.assertEqual([r["message"] for r in buf.snapshot(level="warning")], ["e"])

    def test_capacity_and_subscribe(self):
        buf = LogBuffer(capacity=3)
        q = buf.subscribe()
        for i in range(5):
            buf.push("INFO", f"m{i}")
        self.assertEqual([r["seq"] for r in buf.snapshot()], [3, 4, 5])
        self.assertEqual(q.get(timeout=1)["message"], "m0")
        while not q.empty():
            q.get()
        buf.unsubscribe(q)
        buf.push("INFO", "x")
        self.assertTrue(q.empty())


class _Fixture:
    """Manager wired to fakes; returns (mgr, hub, poster, tmpdir)."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.hub = WsHub(lambda a, p: {})
        self.poster = Poster([])
        self.mgr = TransportManager(
            lambda a, p: {}, self.hub, self.poster,
            specs_path=os.path.join(self.tmp, "specs.json"),
        )


class TransportManagerTest(unittest.TestCase):
    def test_http_add_list_remove(self):
        fx = _Fixture()
        tid = fx.mgr.add_http("127.0.0.1", 0)
        items = fx.mgr.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "http")
        self.assertEqual(items[0]["status"], "listening")
        port = int(items[0]["address"].split(":")[1])
        self.assertGreater(port, 0)
        # 活着：真实请求能通
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/get_version_info", data=b"{}", timeout=5
        ) as resp:
            self.assertEqual(json.loads(resp.read())["status"], "ok")
        fx.mgr.remove(tid)
        self.assertEqual(fx.mgr.list(), [])
        with self.assertRaises(OSError):
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/x", data=b"{}", timeout=2
            )

    def test_startup_not_removable(self):
        fx = _Fixture()
        tid = fx.mgr.add_http("127.0.0.1", 0, source="startup")
        with self.assertRaises(ApiError):
            fx.mgr.remove(tid)
        self.assertEqual(len(fx.mgr.list()), 1)

    def test_remove_missing(self):
        fx = _Fixture()
        with self.assertRaises(ApiError):
            fx.mgr.remove("t99")

    def test_post_url_and_dup(self):
        fx = _Fixture()
        fx.mgr.add_post_url("http://a/ev")
        self.assertIn("http://a/ev", fx.poster.urls)
        with self.assertRaises(ApiError):
            fx.mgr.add_post_url("http://a/ev")
        tid = fx.mgr.list()[0]["id"]
        fx.mgr.remove(tid)
        self.assertNotIn("http://a/ev", fx.poster.urls)

    def test_ws_reverse_remove_to_dead_end(self):
        fx = _Fixture()
        tid = fx.mgr.add_ws_reverse("ws://127.0.0.1:1/none")
        time.sleep(0.05)
        self.assertEqual(fx.mgr.list()[0]["status"], "connecting")
        fx.mgr.remove(tid)  # 不应 hang
        self.assertEqual(fx.mgr.list(), [])

    def test_persistence_roundtrip(self):
        fx = _Fixture()
        fx.mgr.add_post_url("http://a/ev")
        fx.mgr.add_ws_reverse("ws://127.0.0.1:1/none", token="tk")
        specs = json.load(open(fx.mgr.specs_path))
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[1]["token"], "tk")

        fx2 = _Fixture()
        fx2.mgr.specs_path = fx.mgr.specs_path
        fx2.mgr.restore()
        items = {i["type"]: i for i in fx2.mgr.list()}
        self.assertIn("http_post", items)
        self.assertIn("ws_reverse", items)
        fx2.mgr.remove(items["http_post"]["id"])
        fx2.mgr.remove(items["ws_reverse"]["id"])


class AdminAuthTest(unittest.TestCase):
    """管理 action 在 --access-token 未配置时必须拒绝。"""

    def _bridge(self, token):
        from qqcache.serve import Bridge

        tmp = tempfile.mkdtemp()
        return Bridge(os.path.join(tmp, "b.sqlite3"), token=token)

    def test_admin_denied_without_token(self):
        bridge = self._bridge(None)
        for action in ("get_logs", "list_transports", "add_transport",
                       "del_transport"):
            with self.assertRaises(ApiError) as ctx:
                bridge.dispatch(action, {})
            self.assertEqual(ctx.exception.retcode, 1403)

    def test_admin_ok_with_token(self):
        bridge = self._bridge("tok")
        out = bridge.dispatch("list_transports", {})
        self.assertIn("transports", out)
        out = bridge.dispatch("add_transport", {"type": "http", "host": "127.0.0.1",
                                                "port": 0})
        tid = out["id"]
        self.assertEqual(len(bridge.dispatch("list_transports", {})["transports"]), 1)
        bridge.dispatch("del_transport", {"id": tid})
        self.assertEqual(bridge.dispatch("list_transports", {})["transports"], [])

    def test_get_logs_action(self):
        bridge = self._bridge("tok")
        bridge.logbuf.push("INFO", "hello-log")
        out = bridge.dispatch("get_logs", {"tail": 10})
        self.assertEqual(out["logs"][-1]["message"], "hello-log")
        self.assertEqual(out["latest_seq"], 1)


class SelectEmitTest(unittest.TestCase):
    def _result(self, msgs, first_run):
        from qqcache.cursor import Result
        return Result(msgs, False, first_run)

    def _msg(self, i, kind="text"):
        from qqcache.messages import Message
        return Message(msg_id=str(i), kind=kind, sender="s", title=None,
                       content=f"c{i}", timestamp="14:42", is_self=False)

    def test_normal_run_emits_all_new(self):
        from qqcache.serve import _select_emit
        msgs = [self._msg(1), self._msg(2)]
        self.assertEqual(len(_select_emit(self._result(msgs, False))), 2)

    def test_first_run_emits_tail_only(self):
        # first_run 不分路径一律只 emit 最新非 system 消息：
        # 通知路径它是通知所指，poll 路径它兜住崩溃丢失的通知
        from qqcache.serve import _select_emit
        msgs = [self._msg(1), self._msg(2), self._msg(3)]
        out = _select_emit(self._result(msgs, True))
        self.assertEqual([m.msg_id for m in out], ["3"])

    def test_first_run_skips_system_tail(self):
        from qqcache.serve import _select_emit
        msgs = [self._msg(1), self._msg(2, kind="system")]
        out = _select_emit(self._result(msgs, True))
        self.assertEqual([m.msg_id for m in out], ["1"])


class RectOverlapTest(unittest.TestCase):
    def test_overlap(self):
        from qqcache.send import _rect_overlap
        a = (100, 100, 50, 50)
        self.assertTrue(_rect_overlap(a, (120, 120, 50, 50)))
        self.assertFalse(_rect_overlap(a, (200, 100, 50, 50)))   # 右侧
        self.assertFalse(_rect_overlap(a, (100, 200, 50, 50)))   # 下方
        self.assertTrue(_rect_overlap(a, a))                      # 自身


class StatusAndHeartbeatTest(unittest.TestCase):
    def test_status_text_format(self):
        from qqcache.serve import status_text

        text = status_text(90061)  # 1天1小时1分钟1秒
        self.assertIn("WhiskerWind 状态", text)
        self.assertRegex(text, r"版本: \d+\.\d+\.\d+")
        self.assertRegex(text, r"平台: linux-\w+")
        self.assertIn("运行时长: 1天 1小时 1分钟", text)

    def test_heartbeat_emits_login_state_transition(self):
        import types
        from qqcache.serve import heartbeat_tick

        emitted = []
        from qqcache import proto_onebot
        bridge = types.SimpleNamespace(
            self_id=1,
            publish=lambda ev: emitted.extend(proto_onebot.render(ev, 1)),
            gui_lock=__import__("threading").Lock(),
            _app=lambda: None,
        )
        state = heartbeat_tick(bridge, 30, None)
        self.assertEqual(state, "not_running")
        self.assertEqual(emitted[0]["meta_event_type"], "heartbeat")
        self.assertEqual(emitted[0]["status"]["login_state"], "not_running")
        # 首次 tick 也发 transition（prev=None -> 当前态），之后同态不再发
        self.assertEqual(emitted[1]["meta_event_type"], "login_state")
        heartbeat_tick(bridge, 30, state)
        self.assertEqual(len(emitted), 3)  # 只有一个新 heartbeat


class LogStreamTest(unittest.TestCase):
    def _server(self, logbuf, token=None):
        handler = make_api_handler(
            lambda a, p: {}, access_token=token, log_buffer=logbuf
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_stream_replays_then_live(self):
        buf = LogBuffer()
        buf.push("INFO", "old")
        server = self._server(buf, token="tok")
        port = server.server_address[1]
        conn = ws11.connect(
            f"ws://127.0.0.1:{port}/logs/stream?access_token=tok&tail=10"
        )
        first = json.loads(next(conn.messages()))
        self.assertEqual(first["type"], "log")
        self.assertEqual(first["data"]["message"], "old")
        buf.push("WARNING", "new")
        second = json.loads(next(conn.messages()))
        self.assertEqual(second["data"]["message"], "new")
        conn.close()
        server.shutdown()

    def test_stream_requires_token(self):
        server = self._server(LogBuffer(), token="tok")
        port = server.server_address[1]
        with self.assertRaises(Exception):
            ws11.connect(f"ws://127.0.0.1:{port}/logs/stream")
        server.shutdown()

    def test_stream_rejected_without_token_configured(self):
        server = self._server(LogBuffer(), token=None)
        port = server.server_address[1]
        with self.assertRaises(Exception):
            ws11.connect(f"ws://127.0.0.1:{port}/logs/stream")
        server.shutdown()


if __name__ == "__main__":
    unittest.main()


class DurationToDhmTest(unittest.TestCase):
    def test_zero_means_unban(self):
        from qqcache.admin import duration_to_dhm
        self.assertIsNone(duration_to_dhm(0))
        self.assertIsNone(duration_to_dhm(-5))

    def test_seconds_round_to_minute(self):
        from qqcache.admin import duration_to_dhm
        # <30s 也至少给 1 分钟（QQ 无秒粒度也无 0）
        self.assertEqual(duration_to_dhm(1), (0, 0, 1))
        self.assertEqual(duration_to_dhm(29), (0, 0, 1))
        self.assertEqual(duration_to_dhm(30), (0, 0, 1))  # 30s 四舍五入→1分
        self.assertEqual(duration_to_dhm(60), (0, 0, 1))
        self.assertEqual(duration_to_dhm(89), (0, 0, 1))  # 29s 零头舍去
        self.assertEqual(duration_to_dhm(90), (0, 0, 2))  # 30s 零头进位

    def test_decomposition(self):
        from qqcache.admin import duration_to_dhm
        self.assertEqual(duration_to_dhm(3600), (0, 1, 0))
        self.assertEqual(duration_to_dhm(90000), (1, 1, 0))
        self.assertEqual(duration_to_dhm(30 * 1440 * 60), (30, 0, 0))

    def test_over_30_days_rejected(self):
        from qqcache.admin import duration_to_dhm
        from qqcache.send import SendError
        with self.assertRaises(SendError):
            duration_to_dhm(31 * 1440 * 60)


class RecentMsgIdsTest(unittest.TestCase):
    def setUp(self):
        from qqcache.ob11 import IdMap
        fd, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.ids = IdMap(self.path)

    def tearDown(self):
        self.ids.close()
        os.unlink(self.path)

    def test_newest_first_and_filters(self):
        self.ids.record_message("100", "group", "m1", "in", "555", "a", "member", "[]", "x")
        self.ids.record_message("100", "group", "m2", "in", "666", "b", "member", "[]", "x")
        self.ids.record_message("100", "group", "m3", "in", "555", "a", "member", "[]", "x")
        self.ids.record_message("100", "group", None, "in", "555", "a", "member", "[]", "x")
        self.assertEqual(
            self.ids.recent_msg_ids_by_sender("100", "555"), ["m3", "m1"])
        self.assertEqual(
            self.ids.recent_msg_ids_by_sender("100", "666"), ["m2"])
        self.assertEqual(
            self.ids.recent_msg_ids_by_sender("100", "999"), [])
