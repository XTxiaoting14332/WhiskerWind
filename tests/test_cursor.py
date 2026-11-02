import os
import tempfile
import unittest

from qqcache.cursor import Cursor


class Fake:
    def __init__(self, msg_id):
        self.msg_id = msg_id


def msgs(*ids):
    return [Fake(i) for i in ids]


class CursorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor = Cursor(os.path.join(self.tmp.name, "c.sqlite3"))

    def tearDown(self):
        self.cursor.close()
        self.tmp.cleanup()

    def test_first_run_marks_everything_seen(self):
        r = self.cursor.filter_new("g1", msgs("1", "2", "3"))
        self.assertTrue(r.first_run)
        self.assertEqual(len(r.new), 3)
        self.assertFalse(r.gap)

    def test_only_new_messages_are_emitted(self):
        first = msgs("1", "2", "3")
        self.cursor.commit("g1", first)
        r = self.cursor.filter_new("g1", msgs("2", "3", "4"))
        self.assertFalse(r.first_run)
        self.assertEqual([m.msg_id for m in r.new], ["4"])
        self.assertFalse(r.gap)

    def test_gap_when_oldest_visible_is_unknown(self):
        self.cursor.commit("g1", msgs("1", "2", "3"))
        r = self.cursor.filter_new("g1", msgs("5", "6", "7"))
        self.assertTrue(r.gap)
        self.assertEqual(len(r.new), 3)

    def test_no_gap_when_window_still_overlaps(self):
        self.cursor.commit("g1", msgs("1", "2", "3"))
        r = self.cursor.filter_new("g1", msgs("3", "4"))
        self.assertFalse(r.gap)
        self.assertEqual([m.msg_id for m in r.new], ["4"])

    def test_conversations_are_isolated(self):
        self.cursor.commit("g1", msgs("1"))
        r = self.cursor.filter_new("g2", msgs("1"))
        self.assertTrue(r.first_run)

    def test_ids_are_not_assumed_monotonic(self):
        self.cursor.commit("g1", msgs("7770001"))
        r = self.cursor.filter_new("g1", msgs("7770001", "7700002"))
        self.assertEqual([m.msg_id for m in r.new], ["7700002"])

    def test_reset(self):
        self.cursor.commit("g1", msgs("1"))
        self.cursor.reset("g1")
        r = self.cursor.filter_new("g1", msgs("1"))
        self.assertTrue(r.first_run)


if __name__ == "__main__":
    unittest.main()
