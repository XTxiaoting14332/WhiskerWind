import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from qqcache import automatic, core, ws11
from qqcache.messages import Message
from qqcache.ob11 import ApiError, IdMap
from qqcache.store import Cache


def _msg(**kw):
    base = dict(msg_id="7703189013652762603", kind="text", sender="【示例】Alice",
                is_self=False, content="#ww", title="", quoted_sender=None,
                quoted_content=None, timestamp="0")
    base.update(kw)
    return Message(**base)


class FakeBridge:
    def __init__(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "b.sqlite3")
        self.cache = Cache(db)
        self.ids = IdMap(db)
        self.nt_data = os.path.join(tmp, "nt")
        os.makedirs(os.path.join(self.nt_data, "Pic"))
        self.self_id = 1234567890
        self.self_name = "夜风"
        self.self_face_url = ""
        self.resolve_first = True
        self.sent = []
        self.fail_with = None
        self.cache.record("files", "100000001", kind="group")

    def _login_state_cached(self, ttl=2.0, lock_timeout=5.0):
        return "logged_in"

    def api_send(self, uin, message, auto_escape=False):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((uin, message))
        ob = self.ids.record_message(str(uin), "unknown", f"m{len(self.sent)}", "out",
                                     str(self.self_id), self.self_name, None, "ok", "ok")
        return {"message_id": ob}

    def act_resolve_member(self, p):
        return {"user_id": 2345678901, "was_user_id": int(p["user_id"]), "nickname": "A"}


class MappingTest(unittest.TestCase):
    def test_user_and_conversation_refs(self):
        self.assertEqual(automatic.user_ref("2345678901"), {"id": "2345678901", "resolved": True})
        self.assertEqual(automatic.user_ref("900000000003", "N"),
                         {"id": "syn:900000000003", "resolved": False, "name": "N"})
        self.assertEqual(automatic.bridge_uid("syn:900000000003"), "900000000003")
        self.assertEqual(automatic.parse_conv("private:1"), ("1", "c2c"))
        with self.assertRaises(automatic.AutomaticError):
            automatic.parse_conv("100000001")

    def test_error_mapping_retryable_means_no_side_effect(self):
        e = automatic.from_api_error(ApiError(1400, "目标 1 身份验证失败，拒绝发送"))
        self.assertEqual((e.code, e.retryable), ("target_unverified", True))
        e = automatic.from_api_error(ApiError(1400, "组装后发送按钮仍未启用"))
        self.assertEqual((e.code, e.retryable), ("gui_failed", False))
        self.assertEqual(automatic.from_api_error(ApiError(1404, "x")).code, "not_found")

    def test_render_message_live_and_backlog(self):
        b = FakeBridge()
        s = automatic.AutomaticServer(b)
        media = os.path.join(b.nt_data, "Pic", "a.png")
        ev = core.MessageCreated("100000001", "group", _msg(), "900000000003", "admin", 5,
                                 f"hi[CQ:image,file=a.png,url=file://{media}]", time=1000,
                                 reply_msg_id="777")
        body = s.render(ev)
        m = body["message"]
        self.assertEqual(body["type"], "message.created")
        self.assertEqual(m["conversation"], {"id": "group:100000001", "kind": "group", "name": "files"})
        self.assertEqual(m["sender"], {"id": "syn:900000000003", "resolved": False,
                                       "name": "【示例】Alice"})
        self.assertEqual(m["segments"], [{"type": "text", "text": "hi"},
                                         {"type": "image", "url": "/automatic/v1/media/Pic/a.png"}])
        self.assertEqual((m["quote"], m["delivery"], m["role"]), ({"id": "777"}, "live", "admin"))
        ev.backlog = True
        self.assertEqual(s.render(ev)["message"]["delivery"], "backlog")

    def test_file_and_system_notification(self):
        s = automatic.AutomaticServer(FakeBridge())
        ev = core.MessageCreated("1", "c2c", _msg(kind="file", file_name="a.zip", file_size="3MB"),
                                 "1", "member", 1, "[文件] a.zip")
        self.assertEqual(s.render(ev)["message"]["segments"],
                         [{"type": "file", "name": "a.zip", "size": "3MB"}])
        n = s.render(core.SystemNotification("群通知", "WS 申请加入 群"))
        self.assertEqual((n["type"], n["title"]), ("notification.system", "群通知"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.b = FakeBridge()
        self.s = automatic.AutomaticServer(self.b, host="127.0.0.1", port=0, token="tok").start()
        self.addCleanup(self.s.stop)

    def _ws(self, hello=None):
        conn = ws11.connect(f"ws://127.0.0.1:{self.s.port}/automatic/v1")
        it = conn.messages()
        conn.send_text(json.dumps(hello or {"type": "hello", "protocol": "automatic/1", "token": "tok"}))
        return conn, it

    def test_hello_ready_call_and_resume(self):
        conn, it = self._ws()
        ready = json.loads(next(it))
        self.assertEqual((ready["type"], ready["resumed"], ready["login"]), ("ready", False, "online"))
        self.assertIn("message.send", ready["capabilities"])
        self.assertTrue(ready["options"]["wait_resolve"])

        conn.send_text(json.dumps({"type": "call", "id": "c1", "action": "message.send", "params": {
            "conversation": "group:100000001", "segments": [{"type": "text", "text": "hi"}]}}))
        res = json.loads(next(it))
        self.assertEqual((res["id"], res["ok"]), ("c1", True))
        self.assertEqual(res["data"]["message"]["conversation"]["id"], "group:100000001")
        self.assertEqual(self.b.sent[0][0], "100000001")

        ev = core.MessageCreated("100000001", "group", _msg(), "2345678901", "member", 9, "x")
        self.s.handle(ev)
        got = json.loads(next(it))
        self.assertEqual((got["type"], got["seq"]), ("event", 1))
        session = ready["session"]
        conn.close()

        self.s.handle(ev)   # seq 2，断线期间
        conn, it = self._ws({"type": "hello", "protocol": "automatic/1", "token": "tok",
                             "resume": {"session": session, "seq": 1}})
        self.assertTrue(json.loads(next(it))["resumed"])
        self.assertEqual(json.loads(next(it))["seq"], 2)
        conn.close()

    def test_bad_token_and_protocol(self):
        conn, it = self._ws({"type": "hello", "protocol": "automatic/1", "token": "bad"})
        self.assertEqual(json.loads(next(it))["error"]["code"], "unauthorized")
        conn, it = self._ws({"type": "hello", "protocol": "onebot/11", "token": "tok"})
        self.assertEqual(json.loads(next(it))["error"]["code"], "unsupported")

    def _http(self, action, params, token="tok"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.s.port}/automatic/v1/{action}",
            data=json.dumps(params).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_http_errors(self):
        self.assertEqual(self._http("self.get", {}, token="bad")[0], 401)
        st, out = self._http("message.send", {"conversation": "group:1",
                                              "segments": [{"type": "voice"}]})
        self.assertEqual((st, out["error"]["code"]), (400, "unsupported_segment"))
        st, out = self._http("message.send", {"conversation": "group:1", "segments": [
            {"type": "mention", "user_id": "syn:900000000003"}]})
        self.assertEqual(out["error"]["code"], "bad_params")
        self.b.fail_with = ApiError(1400, "目标 1 身份验证失败，拒绝发送")
        st, out = self._http("message.send", {"conversation": "group:1",
                                              "segments": [{"type": "text", "text": "x"}]})
        self.assertEqual((st, out["error"]["code"], out["error"]["retryable"]),
                         (409, "target_unverified", True))
        self.assertEqual(self._http("nope", {})[1]["error"]["code"], "unsupported")

    def test_member_resolve_takes_syn_id(self):
        st, out = self._http("member.resolve", {"conversation": "group:100000001",
                                                "user_id": "syn:900000000003"})
        self.assertEqual((st, out["data"]["user"]), (200, {"id": "2345678901", "resolved": True,
                                                           "name": "A"}))


if __name__ == "__main__":
    unittest.main()
