import os
import tempfile
import unittest

from qqcache.ob11 import ApiError
from qqcache.protocols import ProtocolManager


class _Bridge:
    class protocols:
        @staticmethod
        def register(s):
            pass

        @staticmethod
        def unregister(n):
            pass


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.m = ProtocolManager(_Bridge(), path=os.path.join(tempfile.mkdtemp(), "p.json"))

    def test_custom_token_saved_and_stripped(self):
        self.m.update("satori", {"token": "  mine-123  "})
        self.assertEqual(self.m.token("satori"), "mine-123")
        item = [p for p in self.m.list() if p["name"] == "satori"][0]
        self.assertTrue(item["token_set"])
        self.assertNotIn("token", item)   # 列表里绝不带令牌明文

    def test_whitespace_rejected_and_config_untouched(self):
        self.m.update("automatic", {"token": "good"})
        with self.assertRaises(ApiError):
            self.m.update("automatic", {"token": "a b"})
        self.assertEqual(self.m.token("automatic"), "good")

    def test_titles_have_no_version_suffix(self):
        titles = {p["name"]: p["title"] for p in self.m.list()}
        self.assertEqual(titles["automatic"], "automatic")


class TokenRequiredTest(unittest.TestCase):
    """监听非回环地址的协议必须有令牌（不真正起服务）。"""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "p.json")
        self.m = ProtocolManager(_Bridge(), path=self.path)
        self.m._start = lambda name: None

    def test_enable_on_public_host_generates_token(self):
        self.m.update("satori", {"enabled": True})
        self.assertTrue(self.m.token("satori"))

    def test_explicit_empty_token_on_public_host_rejected(self):
        with self.assertRaises(ApiError):
            self.m.update("automatic", {"enabled": True, "token": ""})
        self.assertFalse(self.m.config["automatic"]["enabled"])

    def test_loopback_allows_no_token(self):
        self.m.update("satori", {"enabled": True, "host": "127.0.0.1", "token": ""})
        self.assertEqual(self.m.token("satori"), "")

    def test_saved_tokenless_config_gets_token_on_start(self):
        import json
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"satori": {"enabled": True, "host": "0.0.0.0", "token": ""}}, fh)
        m = ProtocolManager(_Bridge(), path=self.path)
        m._start = lambda name: None
        m.start_enabled()
        self.assertTrue(m.token("satori"))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["satori"]["token"], m.token("satori"))
