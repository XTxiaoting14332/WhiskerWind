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
