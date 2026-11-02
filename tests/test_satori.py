import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from qqcache import core, satori, ws11
from qqcache.messages import Message
from qqcache.ob11 import IdMap
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
        self.db = os.path.join(tmp, "b.sqlite3")
        self.cache = Cache(self.db)
        self.ids = IdMap(self.db)
        self.nt_data = os.path.join(tmp, "nt_data")
        os.makedirs(os.path.join(self.nt_data, "Pic"))
        self.self_id = 1234567890
        self.self_name = "夜风"
        self.self_face_url = ""
        self.sent = []
        self.cache.record("files（4）", "100000001", kind="group")

    def _login_state_cached(self, ttl=2.0):
        return "logged_in"

    def api_send(self, uin, message, auto_escape=False):
        self.sent.append((uin, message))
        ob = self.ids.record_message(str(uin), "unknown", f"sent{len(self.sent)}", "out",
                                     str(self.self_id), self.self_name, None, "ok", "ok")
        return {"message_id": ob}

    def act_get_group_list(self, p):
        return [{"group_id": 100000001, "group_name": "files"}]


class ElementTest(unittest.TestCase):
    def test_parse_and_unescape(self):
        toks = satori.parse_elements('a &lt;b&gt; <at id="1"/><img src="x&amp;y"/>')
        self.assertEqual(toks[0], ("text", {"text": "a <b> "}))
        self.assertEqual(toks[1], ("at", {"id": "1"}))
        self.assertEqual(toks[3], ("img", {"src": "x&y"}))

    def test_content_to_segments(self):
        s = satori.SatoriServer(FakeBridge())
        [segs] = s.content_to_segments(
            '<at type="all"/> hi <b>bold</b><br/><a href="https://e.x">link</a>', "100000001")
        self.assertEqual(segs[0], {"type": "at", "data": {"qq": "all"}})
        text = "".join(x["data"].get("text", "") for x in segs if x["type"] == "text")
        self.assertEqual(text, " hi bold\nlink (https://e.x)")

    def test_message_split_and_quote(self):
        b = FakeBridge()
        ob = b.ids.record_message("100000001", "group", "777", "in", "1", "A", None, "q", "q")
        s = satori.SatoriServer(b)
        msgs = s.content_to_segments('<quote id="777">被引用</quote>re<message/>two', "100000001")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0][0], {"type": "reply", "data": {"id": str(ob)}})
        self.assertEqual(msgs[0][1], {"type": "text", "data": {"text": "re"}})

    def test_render_group_message(self):
        b = FakeBridge()
        s = satori.SatoriServer(b)
        media = os.path.join(b.nt_data, "Pic", "a.png")
        ev = core.MessageCreated("100000001", "group", _msg(), "2345678901", "member", 5,
                                 f"[CQ:image,file=a.png,url=file://{media}]hi", time=1000)
        body = s.render(ev)
        self.assertEqual(body["type"], "message-created")
        self.assertEqual(body["channel"], {"id": "100000001", "type": 0, "name": "files"})
        self.assertEqual(body["guild"]["id"], "100000001")
        self.assertEqual(body["user"]["id"], "2345678901")
        self.assertEqual(body["message"]["id"], "7703189013652762603")
        self.assertEqual(body["message"]["content"],
                         '<img src="internal:qq/1234567890/media/Pic/a.png"/>hi')
        self.assertEqual(body["login"], {"sn": 1, "platform": "qq", "user": {"id": "1234567890"}})
        self.assertEqual(body["timestamp"], 1000000)

    def test_private_channel_and_unresolved_user(self):
        s = satori.SatoriServer(FakeBridge())
        ev = core.MessageCreated("333333333", "c2c", _msg(sender="Lazy"), "333333333", "member", 6, "x")
        self.assertEqual(s.render(ev)["channel"], {"id": "private:333333333", "type": 1})
        ev = core.MessageCreated("100000001", "group", _msg(), "900000000003", "member", 7, "x")
        self.assertNotIn("avatar", s.render(ev)["user"])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.s = satori.SatoriServer(self.bridge, host="127.0.0.1", port=0, token="tok").start()
        self.addCleanup(self.s.stop)
        self.base = f"http://127.0.0.1:{self.s.port}/v1/"

    def _post(self, method, body, token="tok", headers=None):
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {token}",
             "Satori-Platform": "qq", "Satori-User-ID": "1234567890", **(headers or {})}
        req = urllib.request.Request(self.base + method, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_auth_and_status_codes(self):
        self.assertEqual(self._post("login.get", {}, token="bad")[0], 401)
        status, login = self._post("login.get", {})
        self.assertEqual((status, login["status"], login["adapter"]), (200, 1, "whiskerwind"))
        self.assertEqual(self._post("message.update", {})[0], 404)     # QQ 没有
        self.assertEqual(self._post("guild.member.kick", {})[0], 501)  # QQ 有、未实现
        self.assertEqual(self._post("nope.nope", {})[0], 404)
        self.assertEqual(self._post("login.get", {}, headers={"Satori-User-ID": "1"})[0], 404)

    def test_message_create_goes_through_bridge(self):
        status, out = self._post("message.create",
                                 {"channel_id": "private:2345678901", "content": "a<message/>b"})
        self.assertEqual(status, 200)
        self.assertEqual([u for u, _ in self.bridge.sent], ["2345678901", "2345678901"])
        self.assertEqual(out[0]["id"], "sent1")
        self.assertEqual(out[0]["channel"]["id"], "private:2345678901")

    def _ws(self):
        conn = ws11.connect(f"ws://127.0.0.1:{self.s.port}/v1/events")
        it = conn.messages()
        return conn, it

    def test_ws_identify_ping_and_resume(self):
        conn, it = self._ws()
        conn.send_text(json.dumps({"op": 3, "body": {"token": "tok"}}))
        ready = json.loads(next(it))
        self.assertEqual(ready["op"], 4)
        self.assertEqual(ready["body"]["logins"][0]["user"]["id"], "1234567890")
        conn.send_text(json.dumps({"op": 1, "body": {}}))
        self.assertEqual(json.loads(next(it))["op"], 2)

        ev = core.MessageCreated("100000001", "group", _msg(), "2345678901", "member", 5, "hi")
        self.s.handle(ev)
        got = json.loads(next(it))
        self.assertEqual((got["op"], got["body"]["sn"]), (0, 1))
        conn.close()

        self.s.handle(ev)   # sn 2，断线期间
        time.sleep(0.1)
        conn, it = self._ws()
        conn.send_text(json.dumps({"op": 3, "body": {"token": "tok", "sn": 1}}))
        self.assertEqual(json.loads(next(it))["op"], 4)
        replay = json.loads(next(it))
        self.assertEqual(replay["body"]["sn"], 2)
        conn.close()

    def test_ws_bad_token_closed(self):
        conn, it = self._ws()
        conn.send_text(json.dumps({"op": 3, "body": {"token": "bad"}}))
        self.assertEqual(list(it), [])


if __name__ == "__main__":
    unittest.main()
