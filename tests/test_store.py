import os
import tempfile
import time
import unittest

from qqcache.store import Cache, display_name, is_uin, sanitize


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(os.path.join(self.tmp.name, "cache.sqlite3"))

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_roundtrip(self):
        self.cache.record("files", "100000001", "group")
        self.assertEqual(self.cache.resolve("files"), ["100000001"])
        self.assertEqual(self.cache.get("files"), "100000001")
        self.assertEqual(self.cache.lookup("100000001")["kind"], "group")

    def test_rename_keeps_old_alias(self):
        self.cache.record("旧群名", "1001311187", "group")
        self.cache.record("新群名", "1001311187", "group")
        self.assertEqual(self.cache.resolve("旧群名"), ["1001311187"])
        self.assertEqual(self.cache.resolve("新群名"), ["1001311187"])
        self.assertEqual(self.cache.lookup("1001311187")["name"], "新群名")
        self.assertEqual(self.cache.lookup("1001311187")["fetch_count"], 2)

    def test_same_name_two_groups_is_ambiguous(self):
        self.cache.record("通知群", "111111111", "group")
        self.cache.record("通知群", "222222222", "group")
        self.assertEqual(len(self.cache.resolve("通知群")), 2)
        self.assertIsNone(self.cache.get("通知群"))
        self.assertTrue(self.cache.ambiguous("通知群"))
        self.assertEqual(self.cache.stats()["ambiguous_names"], 1)

    def test_ttl(self):
        now = time.time()
        self.cache.record("files", "100000001", "group", now=now - 10 * 86400)
        self.assertFalse(self.cache.is_fresh("files", ttl=7 * 86400))
        self.assertTrue(self.cache.is_fresh("files", ttl=30 * 86400))
        self.assertTrue(self.cache.is_fresh("files"))

    def test_unknown_name(self):
        self.assertEqual(self.cache.resolve("没有这个群"), [])
        self.assertFalse(self.cache.is_fresh("没有这个群"))


class SanitizeTest(unittest.TestCase):
    def test_strips_control_chars(self):
        self.assertEqual(sanitize("<$ÿĀ\x11\x10>相亲相爱一家人"), "<$ÿĀ>相亲相爱一家人")
        self.assertEqual(display_name("  files  "), "files")

    def test_uin(self):
        self.assertTrue(is_uin("100000001"))
        self.assertTrue(is_uin(" 2345678901 "))
        self.assertFalse(is_uin("1234"))
        self.assertFalse(is_uin("abcdefghi"))
        self.assertFalse(is_uin(""))


if __name__ == "__main__":
    unittest.main()
