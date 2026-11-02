import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from qqcache.messages import Message
from qqcache.ob11 import (
    ApiError,
    IdMap,
    Poster,
    build_message_event,
    cq_escape,
    cq_segment,
    cq_unescape,
    make_api_handler,
    message_to_cq,
    normalize_segments,
    ops_from_segments,
    parse_cq,
)
from http.server import ThreadingHTTPServer
import threading


def msg(msg_id="7700001", kind="text", sender="小明", content="你好", is_self=False,
        title=None, quoted_sender=None, quoted_content=None, file_name=None,
        file_size=None):
    return Message(
        msg_id=msg_id, kind=kind, sender=sender, title=title, content=content,
        timestamp="14:42", is_self=is_self, quoted_sender=quoted_sender,
        quoted_content=quoted_content, file_name=file_name, file_size=file_size,
    )


class CqCodecTest(unittest.TestCase):
    def test_escape_roundtrip(self):
        text = "a&b[c],d"
        self.assertEqual(cq_unescape(cq_escape(text)), text)
        self.assertEqual(cq_escape(text, in_param=True), "a&amp;b&#91;c&#93;&#44;d")

    def test_parse_plain(self):
        self.assertEqual(parse_cq("hello"), [("text", {"text": "hello"})])

    def test_parse_at(self):
        segs = parse_cq("看[CQ:at,qq=12345]这里")
        self.assertEqual(segs[0], ("text", {"text": "看"}))
        self.assertEqual(segs[1], ("at", {"qq": "12345"}))
        self.assertEqual(segs[2], ("text", {"text": "这里"}))

    def test_parse_multi_params(self):
        segs = parse_cq("[CQ:image,file=a.png,url=file:///tmp/a.png]")
        self.assertEqual(
            segs,
            [("image", {"file": "a.png", "url": "file:///tmp/a.png"})],
        )

    def test_parse_escaped_value(self):
        segs = parse_cq("[CQ:share,title=a&#44;b,url=http://x]")
        self.assertEqual(segs[0][1]["title"], "a,b")

    def test_segment_render(self):
        self.assertEqual(cq_segment("at", qq=123), "[CQ:at,qq=123]")

    def test_normalize_array(self):
        segs = normalize_segments([
            {"type": "text", "data": {"text": "hi"}},
            {"type": "at", "data": {"qq": 123}},
        ])
        self.assertEqual(segs, [("text", {"text": "hi"}), ("at", {"qq": "123"})])


class OpsFromSegmentsTest(unittest.TestCase):
    def test_text_at_image(self):
        ops, reply = ops_from_segments(
            [
                ("text", {"text": "hi"}),
                ("at", {"qq": "123"}),
                ("image", {"file": "/tmp/x.png"}),
            ],
            lambda f: f,
        )
        self.assertEqual(
            ops, [("text", "hi"), ("at", "123"), ("image", "/tmp/x.png")]
        )
        self.assertIsNone(reply)

    def test_at_all(self):
        ops, _ = ops_from_segments([("at", {"qq": "all"})], lambda f: f)
        self.assertEqual(ops, [("at", "all")])

    def test_reply_id(self):
        _, reply = ops_from_segments([("reply", {"id": "42"})], lambda f: f)
        self.assertEqual(reply, "42")

    def test_face_degrades_to_text(self):
        ops, _ = ops_from_segments(
            [("text", {"text": "a"}), ("face", {"id": "1"})], lambda f: f
        )
        self.assertEqual(ops, [("text", "a"), ("text", "[表情]")])

    def test_structural_segment_rejected(self):
        with self.assertRaises(ApiError):
            ops_from_segments([("forward", {"id": "x"})], lambda f: f)


class MessageToCqTest(unittest.TestCase):
    def test_text(self):
        self.assertEqual(message_to_cq(msg(content="a&b")), "a&amp;b")

    def test_image_with_media(self):
        out = message_to_cq(msg(kind="image"), media_path="/data/nt/pic/x.png")
        self.assertEqual(
            out, "[CQ:image,file=x.png,url=file:///data/nt/pic/x.png]"
        )

    def test_reply_prefix(self):
        m = msg(kind="reply", quoted_sender="A", quoted_content="hello")
        out = message_to_cq(m, reply_ob_id=7)
        self.assertTrue(out.startswith("[CQ:reply,id=7]"))

    def test_system_suppressed(self):
        self.assertEqual(message_to_cq(msg(kind="system", content="撤回")), "")

    def test_file(self):
        m = msg(kind="file", content="report.pdf", file_name="report.pdf", file_size="1MB")
        self.assertIn("[文件] report.pdf", cq_unescape(message_to_cq(m)))


class IdMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ids = IdMap(os.path.join(self.tmp, "t.sqlite3"))

    def tearDown(self):
        self.ids.close()

    def test_record_dedupes_by_msg_id(self):
        a = self.ids.record_message("1", "c2c", "770", "in", "2", "n", None, "m", "r")
        b = self.ids.record_message("1", "c2c", "770", "in", "2", "n", None, "m", "r")
        self.assertEqual(a, b)

    def test_member_id_stable_and_upgrade(self):
        syn = self.ids.member_user_id("100", "小明")
        self.assertGreaterEqual(syn, 900_000_000_000)
        self.assertEqual(self.ids.member_user_id("100", "小明"), syn)
        real = self.ids.member_user_id("100", "小明", real_uin="12345")
        self.assertEqual(real, 12345)

    def test_member_ambiguous_second_uin(self):
        # 同名解出第二个号 → 名字判歧义 → 退回合成号，不再错归
        u1 = self.ids.member_user_id("100", "重名", real_uin="11111")
        self.assertEqual(u1, 11111)
        self.ids.member_user_id("100", "重名", real_uin="22222")
        self.assertTrue(self.ids.member_is_ambiguous("100", "重名"))
        got = self.ids.member_user_id("100", "重名")
        self.assertGreaterEqual(got, 900_000_000_000)
        self.assertEqual(set(self.ids.member_uins_for_name("100", "重名")),
                         {"11111", "22222"})

    def test_member_ambiguous_flag(self):
        # 成员列表 count>1 打的标记：只解出一个号也判歧义
        syn = self.ids.member_user_id("100", "同名甲")
        self.ids.member_user_id("100", "同名甲", real_uin="33333")
        self.ids.mark_member_ambiguous("100", "同名甲")
        self.assertTrue(self.ids.member_is_ambiguous("100", "同名甲"))
        got = self.ids.member_user_id("100", "同名甲")
        self.assertGreaterEqual(got, 900_000_000_000)
        self.assertEqual(got, syn)  # 名字槽位还是原来那个合成号

    def test_member_ambiguous_flag_legacy_uin_slot(self):
        # 老数据 user_id 就是真号——打标记时槽位必须换成合成号
        self.ids.db.execute(
            "INSERT INTO ob11_members (group_uin,name,user_id,real_uin,first_seen,last_seen)"
            " VALUES ('100','老同名',44444,'44444',0,0)")
        self.ids.db.commit()
        self.ids.mark_member_ambiguous("100", "老同名")
        got = self.ids.member_user_id("100", "老同名")
        self.assertGreaterEqual(got, 900_000_000_000)

    def test_member_rename_shares_uin(self):
        # 改名：新旧名索引同指一个 uin，两个名都无歧义地返回它
        self.ids.member_user_id("100", "旧名", real_uin="55555")
        self.ids.member_user_id("100", "新名", real_uin="55555")
        self.assertEqual(self.ids.member_user_id("100", "旧名"), 55555)
        self.assertEqual(self.ids.member_user_id("100", "新名"), 55555)
        self.assertFalse(self.ids.member_is_ambiguous("100", "旧名"))

    def test_member_binding_ttl(self):
        # 缓存窗口：窗口内已验绑定直接给真号；过期回合成号（进 pending 重验）
        self.ids.member_user_id("100", "小窗", real_uin="66666")
        self.assertEqual(
            self.ids.member_user_id("100", "小窗", max_age=3600), 66666)
        # 验证时间戳改旧 → 窗口判过期 → 合成号；绑定本体还在
        self.ids.db.execute(
            "UPDATE ob11_member_names SET verified_at = ?"
            " WHERE group_uin = '100' AND name = '小窗'",
            (time.time() - 7200,))
        self.ids.db.commit()
        got = self.ids.member_user_id("100", "小窗", max_age=3600)
        self.assertGreaterEqual(got, 900_000_000_000)
        # 非归因路径（窗口关）照旧认最后已知绑定
        self.assertEqual(self.ids.member_user_id("100", "小窗"), 66666)
        # 重验刷新 verified_at → 窗口内又是真号
        self.ids.member_user_id("100", "小窗", real_uin="66666")
        self.assertEqual(
            self.ids.member_user_id("100", "小窗", max_age=3600), 66666)

    def test_member_binding_ttl_legacy_row(self):
        # 旧格式（真号占 user_id 槽、无索引无时间戳）：窗口下全判过期，
        # 且槽位换成合成号——过期真号不能从槽位漏出去
        self.ids.db.execute(
            "INSERT INTO ob11_members (group_uin,name,user_id,real_uin,first_seen,last_seen)"
            " VALUES ('100','旧绑',77777,'77777',0,0)")
        self.ids.db.commit()
        got = self.ids.member_user_id("100", "旧绑", max_age=3600)
        self.assertGreaterEqual(got, 900_000_000_000)
        # 非归因路径仍认旧绑定
        self.assertEqual(self.ids.member_user_id("100", "旧绑"), 77777)

    def test_guess_reply(self):
        self.ids.record_message("g", "group", "m1", "in", "u1", "小明", None, "hello", "hello world")
        self.ids.record_message("g", "group", "m2", "in", "u2", "小红", None, "x", "bye")
        hit = self.ids.guess_reply_id("g", "小明", "hello")
        self.assertIsNotNone(hit)

    def test_get_msg(self):
        ob_id = self.ids.record_message("1", "c2c", "m", "in", "2", "n", None, "hi", "hi")
        row = self.ids.get(ob_id)
        self.assertEqual(row["msg_id"], "m")


class BuildEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ids = IdMap(os.path.join(self.tmp, "t.sqlite3"))

    def tearDown(self):
        self.ids.close()

    def test_private_event(self):
        m = msg()
        ob_id = self.ids.record_message("2345678901", "c2c", m.msg_id, "in", "2345678901",
                                        m.sender, None, "你好", "你好")
        ev = build_message_event(m, "2345678901", "c2c", 111, self.ids, "你好", ob_id)
        self.assertEqual(ev["message_type"], "private")
        self.assertEqual(ev["user_id"], 2345678901)
        self.assertEqual(ev["self_id"], 111)
        self.assertEqual(ev["post_type"], "message")
        self.assertNotIn("group_id", ev)

    def test_group_event_synthetic_sender(self):
        m = msg(sender="路人甲", title="LV5 管理员")
        ob_id = self.ids.record_message("777", "group", m.msg_id, "in", "x", "路人甲",
                                        "admin", "你好", "你好")
        ev = build_message_event(m, "777", "group", 111, self.ids, "你好", ob_id)
        self.assertEqual(ev["message_type"], "group")
        self.assertEqual(ev["group_id"], 777)
        self.assertEqual(ev["sender"]["role"], "admin")
        self.assertGreaterEqual(ev["user_id"], 900_000_000_000)

    def test_self_event(self):
        m = msg(is_self=True)
        ob_id = self.ids.record_message("777", "group", m.msg_id, "out", "111", "me",
                                        None, "你好", "你好")
        ev = build_message_event(m, "777", "group", 111, self.ids, "你好", ob_id)
        self.assertEqual(ev["post_type"], "message_sent")
        self.assertEqual(ev["user_id"], 111)
        self.assertEqual(ev["target_id"], 777)

    def test_group_event_user_id_passthrough(self):
        # 逐条归因的 user_id 直通事件——歧义名也能带真号，不按名重查
        m = msg(sender="同名者")
        self.ids.member_user_id("777", "同名者", real_uin="11111")
        self.ids.member_user_id("777", "同名者", real_uin="22222")
        ob_id = self.ids.record_message("777", "group", m.msg_id, "in", "22222",
                                        "同名者", None, "你好", "你好")
        ev = build_message_event(m, "777", "group", 111, self.ids, "你好", ob_id,
                                 user_id="22222")
        self.assertEqual(ev["user_id"], 22222)
        self.assertEqual(ev["sender"]["user_id"], 22222)


class HttpApiTest(unittest.TestCase):
    def _server(self, dispatch, token=None):
        handler = make_api_handler(dispatch, access_token=token)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _post(self, server, path, payload, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_dispatch_ok_and_echo(self):
        seen = {}

        def dispatch(action, params):
            seen["action"] = action
            return {"pong": params.get("x")}

        server = self._server(dispatch)
        resp = self._post(server, "/get_version_info", {"x": 1, "echo": "tag"})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["data"], {"pong": 1})
        self.assertEqual(resp["echo"], "tag")
        self.assertEqual(seen["action"], "get_version_info")
        server.shutdown()

    def test_async_suffix_stripped(self):
        def dispatch(action, params):
            return {"a": action}

        server = self._server(dispatch)
        resp = self._post(server, "/send_msg_async", {})
        self.assertEqual(resp["data"]["a"], "send_msg")
        server.shutdown()

    def test_api_error(self):
        def dispatch(action, params):
            raise ApiError(1404, "nope")

        server = self._server(dispatch)
        resp = self._post(server, "/x", {})
        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["retcode"], 1404)
        server.shutdown()

    def test_auth(self):
        server = self._server(lambda a, p: {}, token="secret")
        with self.assertRaises(urllib.error.HTTPError):
            self._post(server, "/x", {})
        resp = self._post(server, "/x", {}, headers={"Authorization": "Bearer secret"})
        self.assertEqual(resp["status"], "ok")
        resp2 = self._post(server, "/x?access_token=secret", {})
        self.assertEqual(resp2["status"], "ok")
        server.shutdown()


class WsTest(unittest.TestCase):
    def _server(self, dispatch, token=None):
        from qqcache.ob11 import WsHub, make_api_handler

        hub = WsHub(dispatch)
        handler = make_api_handler(dispatch, access_token=token, ws_hub=hub)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, hub

    def test_frame_roundtrip(self):
        import socket
        from qqcache import ws11

        a, b = socket.socketpair()
        ws11.send_frame(a, "你好 ob11", mask=True)
        fin, op, payload, _ = ws11.recv_frame(b)
        self.assertTrue(fin)
        self.assertEqual(op, ws11.OP_TEXT)
        self.assertEqual(payload.decode(), "你好 ob11")
        a.close(); b.close()

    def test_forward_ws_api_and_events(self):
        from qqcache import ws11

        server, hub = self._server(lambda a, p: {"action": a, "echo_in": p.get("x")})
        url = f"ws://127.0.0.1:{server.server_address[1]}/"
        conn = ws11.connect(url)
        conn.send_text(json.dumps(
            {"action": "get_version_info", "params": {"x": 1}, "echo": "e1"}
        ))
        resp = json.loads(next(conn.messages()))
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["data"]["action"], "get_version_info")
        self.assertEqual(resp["echo"], "e1")

        hub.broadcast({"post_type": "meta_event", "t": 1})
        ev = json.loads(next(conn.messages()))
        self.assertEqual(ev["t"], 1)
        conn.close()
        server.shutdown()

    def test_forward_ws_auth(self):
        from qqcache import ws11

        server, _ = self._server(lambda a, p: {}, token="tok")
        url = f"ws://127.0.0.1:{server.server_address[1]}/"
        with self.assertRaises(Exception):
            ws11.connect(url)
        conn = ws11.connect(url + "?access_token=tok")
        conn.close()
        server.shutdown()

    def test_reverse_ws_loop(self):
        """Fake framework: accepts the reverse conn, receives an event,
        sends an api request, gets a response."""
        import socket as _socket
        import threading
        from qqcache import ws11
        from qqcache.ob11 import WsHub, reverse_ws_loop

        listener = _socket.socket()
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        got = {}

        def framework_side():
            sock, _ = listener.accept()
            path, headers, key, rest = ws11.server_handshake(sock)
            got["self_id_header"] = headers.get("x-self-id")
            sock.sendall(ws11.accept_response(key))
            conn = ws11.WsConn(sock, buffered=rest)
            # 先收一个事件，再发一个 api 请求，验证回包
            ev = json.loads(next(conn.messages()))
            got["event"] = ev
            conn.send_text(json.dumps(
                {"action": "act", "params": {}, "echo": "r1"}
            ))
            got["resp"] = json.loads(next(conn.messages()))
            conn.close()

        threading.Thread(target=framework_side, daemon=True).start()
        hub = WsHub(lambda a, p: {"did": a})
        stop = threading.Event()
        t = threading.Thread(
            target=reverse_ws_loop,
            args=(f"ws://127.0.0.1:{port}/onebot/v11/ws", hub, 4242, None, stop),
            daemon=True,
        )
        t.start()
        deadline = time.time() + 10
        while not hub.conns and time.time() < deadline:
            time.sleep(0.05)
        hub.broadcast({"post_type": "meta_event", "hello": "ws"})
        deadline = time.time() + 10
        while "resp" not in got and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        self.assertEqual(got["self_id_header"], "4242")
        self.assertEqual(got["event"]["hello"], "ws")
        self.assertEqual(got["resp"]["echo"], "r1")
        self.assertEqual(got["resp"]["data"], {"did": "act"})
        listener.close()


class LoginTest(unittest.TestCase):
    def test_detect_self_uin(self):
        from qqcache.login import detect_self_uin

        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "Partitions", "qqnt_1234567890"))
        os.makedirs(os.path.join(root, "Partitions", "qqnt_9210"))  # 4位=系统分区
        self.assertEqual(detect_self_uin(root), "1234567890")

    def test_detect_self_uin_missing(self):
        from qqcache.login import detect_self_uin

        self.assertIsNone(detect_self_uin(tempfile.mkdtemp()))

    def test_safe_media_path(self):
        from qqcache.ob11 import _safe_media_path

        root = tempfile.mkdtemp()
        f = os.path.join(root, "x.png")
        open(f, "wb").write(b"x")
        self.assertEqual(_safe_media_path(root, f), os.path.realpath(f))
        self.assertIsNone(_safe_media_path(root, "/etc/passwd"))
        self.assertIsNone(_safe_media_path(root, os.path.join(root, "..", "x")))
        self.assertIsNone(_safe_media_path(None, f))


class PosterTest(unittest.TestCase):
    def test_posts_event(self):
        received = []

        def dispatch(action, params):
            received.append(params)
            return {}

        handler = make_api_handler(dispatch)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/events"

        poster = Poster([url], self_id=42)
        poster.post({"post_type": "message", "message": "hi"})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["message"], "hi")
        server.shutdown()



class SegmentsToCqTest(unittest.TestCase):
    def test_roundtrip_and_base64_dropped(self):
        from qqcache.ob11 import normalize_segments, segments_to_cq
        segs = normalize_segments([{"type": "text", "data": {"text": "a[b],c"}},
                                   {"type": "at", "data": {"qq": "1"}},
                                   {"type": "image", "data": {"file": "base64://AAAA"}}])
        cq = segments_to_cq(segs)
        self.assertEqual(cq, "a&#91;b&#93;,c[CQ:at,qq=1][CQ:image,file=base64://]")
        self.assertEqual(normalize_segments(cq)[0], ("text", {"text": "a[b],c"}))

if __name__ == "__main__":
    unittest.main()
