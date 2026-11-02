import os
import tempfile
import unittest

from qqcache.media import MediaIndex, correlate


class Msg:
    def __init__(self, msg_id, kind):
        self.msg_id = msg_id
        self.kind = kind


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "nt_data")
        self.db = os.path.join(self.tmp.name, "m.sqlite3")
        self._make("Pic/2026-10/Ori", "a.png", mtime=1000)
        self._make("Pic/2026-10/Ori", "b.jpg", mtime=1001)
        self._make("Pic/2026-10/Thumb", "a_0.png", mtime=1002)
        self._make("Ptt/2026-10/Ori", "v.amr", mtime=1003)

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, rel, name, mtime):
        directory = os.path.join(self.root, rel)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * 16)
        os.utime(path, (mtime, mtime))
        return path

    def test_scan_ignores_thumbnails_and_is_incremental(self):
        with MediaIndex(self.root, self.db) as index:
            fresh = index.scan()
            self.assertEqual({os.path.basename(m.path) for m in fresh}, {"a.png", "b.jpg", "v.amr"})
            self.assertTrue(index.last_scan_was_initial)
            self.assertEqual(index.scan(), [])
            self.assertFalse(index.last_scan_was_initial)

    def test_scan_picks_up_new_files_only(self):
        with MediaIndex(self.root, self.db) as index:
            index.scan()
            self._make("Pic/2026-10/Ori", "c.png", mtime=1004)
            fresh = index.scan()
            self.assertEqual([os.path.basename(m.path) for m in fresh], ["c.png"])

    def test_correlate_pairs_in_order(self):
        media = [
            type("M", (), {"path": "/x/2.png", "category": "image", "mtime": 20, "size": 1})(),
            type("M", (), {"path": "/x/1.png", "category": "image", "mtime": 10, "size": 1})(),
        ]
        messages = [Msg("m1", "image"), Msg("m2", "text"), Msg("m3", "image")]
        links, leftover = correlate(messages, media)
        self.assertEqual(links["m1"].path, "/x/1.png")
        self.assertEqual(links["m3"].path, "/x/2.png")
        self.assertEqual(leftover, 0)

    def test_correlate_reports_leftover(self):
        media = [
            type("M", (), {"path": "/x/1.png", "category": "image", "mtime": 10, "size": 1})(),
            type("M", (), {"path": "/x/2.png", "category": "image", "mtime": 20, "size": 1})(),
        ]
        links, leftover = correlate([Msg("m1", "image")], media)
        self.assertEqual(len(links), 1)
        self.assertEqual(leftover, 1)

    def test_correlate_leaves_unmatched_media_message_empty(self):
        links, leftover = correlate([Msg("m1", "voice")], [])
        self.assertEqual(links, {})
        self.assertEqual(leftover, 0)


if __name__ == "__main__":
    unittest.main()
