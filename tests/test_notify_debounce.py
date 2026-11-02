import os
import tempfile
import time
import unittest
from unittest import mock

from qqcache.serve import Bridge


class NotifyDebounceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.bridge = Bridge(os.path.join(tmp, "b.sqlite3"), token="t")
        self.bridge.NOTIFY_DEBOUNCE = 0.3
        self.calls = []
        self.bridge.on_notification = mock.Mock(side_effect=lambda *a, **k: self.calls.append((a, k)))

    def test_first_arrival_admitted_burst_coalesced_into_one_trailing(self):
        b = self.bridge
        self.assertTrue(b._notify_admit("群A"))
        self.assertFalse(b._notify_admit("群A"))
        self.assertFalse(b._notify_admit("群A"))
        self.assertEqual(self.calls, [])
        time.sleep(0.5)
        self.assertEqual(self.calls, [(("群A",), {"trailing": True})])

    def test_window_expiry_readmits(self):
        b = self.bridge
        self.assertTrue(b._notify_admit("群A"))
        time.sleep(0.4)
        self.assertTrue(b._notify_admit("群A"))

    def test_sessions_are_independent(self):
        b = self.bridge
        self.assertTrue(b._notify_admit("群A"))
        self.assertTrue(b._notify_admit("群B"))
        self.assertFalse(b._notify_admit("群A"))

    def test_trailing_run_bypasses_admit(self):
        b = self.bridge
        b.on_notification = mock.Mock()
        b._notify_last["群A"] = time.monotonic()
        b._notify_trailing("群A")
        b.on_notification.assert_called_once_with("群A", trailing=True)
        self.assertNotIn("群A", b._notify_pending)


if __name__ == "__main__":
    unittest.main()
