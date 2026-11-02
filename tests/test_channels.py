"""Satori / automatic 通道：增删改、令牌规则、旧配置迁移。"""

import json
import os
import socket
import tempfile
import time
import unittest

from qqcache.ob11 import ApiError


def _bridge(tmp=None):
    from qqcache.serve import Bridge

    tmp = tmp or tempfile.mkdtemp()
    return Bridge(os.path.join(tmp, "b.sqlite3"), token="tok")


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.b = _bridge()
        self.m = self.b.transports

    def tearDown(self):
        for t in self.m.list():
            if t["source"] == "runtime":
                self.m.remove(t["id"])

    def _item(self, tid):
        return next(t for t in self.m.list() if t["id"] == tid)

    def test_public_listener_gets_token_and_list_hides_it(self):
        tid = self.m.add_spec({"type": "satori", "host": "0.0.0.0", "port": 0})
        item = self._item(tid)
        self.assertEqual(item["protocol"], "satori")
        self.assertTrue(item["token_set"])
        self.assertNotIn("token", item)
        self.assertTrue(self.b.dispatch("get_transport_token", {"id": tid})["token"])

    def test_explicit_empty_token_on_public_host_rejected(self):
        with self.assertRaises(ApiError):
            self.m.add_spec({"type": "automatic", "host": "0.0.0.0", "port": 0, "token": ""})
        self.assertEqual(self.m.list(), [])

    def test_loopback_listener_may_skip_token(self):
        tid = self.m.add_spec({"type": "automatic", "host": "127.0.0.1", "port": 0})
        self.assertFalse(self._item(tid)["token_set"])

    def test_two_satori_channels_are_separate_adapters(self):
        a = self.m.add_spec({"type": "satori", "host": "127.0.0.1", "port": 0})
        b = self.m.add_spec({"type": "satori", "host": "127.0.0.1", "port": 0, "platform": "qq2"})
        self.assertIsNotNone(self.b.protocols.get(f"satori:{a}"))
        self.assertIsNotNone(self.b.protocols.get(f"satori:{b}"))
        self.assertEqual(self._item(b)["platform"], "qq2")
        self.m.remove(a)
        self.assertIsNone(self.b.protocols.get(f"satori:{a}"))

    def test_update_keeps_id_and_regenerates_token(self):
        tid = self.m.add_spec({"type": "satori", "host": "127.0.0.1", "port": 0, "token": "old"})
        self.b.dispatch("update_transport", {"id": tid, "platform": "x", "regenerate_token": True})
        self.assertEqual(self._item(tid)["platform"], "x")
        self.assertNotEqual(self.m.token(tid), "old")
        self.assertIsNotNone(self.b.protocols.get(f"satori:{tid}"))

    def test_failed_update_rolls_back(self):
        tid = self.m.add_spec({"type": "automatic", "host": "127.0.0.1", "port": 0})
        port = self._item(tid)["port"]
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        try:
            with self.assertRaises(ApiError):
                self.m.update(tid, {"port": busy.getsockname()[1]})
        finally:
            busy.close()
        self.assertEqual(self._item(tid)["port"], port)
        self.assertIsNotNone(self.b.protocols.get(f"automatic:{tid}"))

    def test_unknown_field_and_bad_url_rejected(self):
        tid = self.m.add_spec({"type": "http_post", "url": "http://127.0.0.1:1/ev"})
        with self.assertRaises(ApiError):
            self.m.update(tid, {"platform": "x"})
        with self.assertRaises(ApiError):
            self.m.add_spec({"type": "automatic_reverse", "url": "http://x"})

    def test_automatic_reverse_channel(self):
        tid = self.m.add_spec({"type": "automatic_reverse", "url": "ws://127.0.0.1:1/x"})
        time.sleep(0.05)
        item = self._item(tid)
        self.assertEqual(item["protocol"], "automatic")
        self.assertEqual(item["status"], "connecting")
        self.m.remove(tid)

    def test_startup_channel_not_editable(self):
        tid = self.m.add_ws_reverse("ws://127.0.0.1:1/x", source="startup")
        with self.assertRaises(ApiError):
            self.m.update(tid, {"url": "ws://127.0.0.1:2/x"})
        self.m._stop(self.m._items[tid])


class MigrationTest(unittest.TestCase):
    def test_legacy_protocols_json_becomes_channels(self):
        tmp = tempfile.mkdtemp()
        legacy = os.path.join(tmp, "b.sqlite3.protocols.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({
                "satori": {"enabled": True, "host": "127.0.0.1", "port": 0, "token": "s",
                           "platform": "qq", "webhooks": [{"url": "http://127.0.0.1:1/h"}]},
                "automatic": {"enabled": True, "host": "0.0.0.0", "port": 0, "token": "",
                              "reverse_urls": ["ws://127.0.0.1:1/a"]},
            }, fh)
        b = _bridge(tmp)
        b.transports.restore()
        items = b.transports.list()
        self.assertEqual(sorted(t["type"] for t in items),
                         ["automatic", "automatic_reverse", "satori"])
        sat = next(t for t in items if t["type"] == "satori")
        self.assertEqual(sat["webhooks"], 1)
        auto = next(t for t in items if t["type"] == "automatic")
        self.assertTrue(auto["token_set"])          # 对外监听、无令牌：迁移时补生成
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy + ".migrated"))
        with open(os.path.join(tmp, "b.sqlite3.transports.json"), encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)), 3)
        for t in items:
            b.transports.remove(t["id"])


if __name__ == "__main__":
    unittest.main()
