import os
import tempfile
import threading
import time
import types
import unittest

from qqcache.serve import Bridge, TrackedRLock, gui_watchdog


class TrackedRLockTest(unittest.TestCase):
    def test_reentrant_and_tracks_outermost(self):
        lk = TrackedRLock()
        self.assertIsNone(lk.held())
        with lk:
            first = lk.held()
            with lk:
                self.assertEqual(lk.held()[0], first[0])
            self.assertIsNotNone(lk.held())
            self.assertTrue(lk._is_owned())
        self.assertIsNone(lk.held())

    def test_timeout_acquire(self):
        lk = TrackedRLock()
        got = threading.Event()
        done = threading.Event()

        def hold():
            with lk:
                got.set()
                done.wait(2)

        t = threading.Thread(target=hold)
        t.start()
        got.wait(2)
        self.assertFalse(lk.acquire(timeout=0.1))
        done.set()
        t.join()


class WatchdogTest(unittest.TestCase):
    def test_trips_on_long_hold_and_not_on_short(self):
        bridge = types.SimpleNamespace(gui_lock=TrackedRLock())
        fired = []
        stop = threading.Event()
        release = threading.Event()

        def hold():
            with bridge.gui_lock:
                release.wait(3)

        t = threading.Thread(target=hold)
        t.start()
        w = threading.Thread(target=gui_watchdog,
                             args=(bridge, stop, 0.3, 0.05, lambda: fired.append(1)))
        w.start()
        w.join(2)
        release.set()
        t.join()
        self.assertEqual(fired, [1])

        fired.clear()
        stop2 = threading.Event()
        w = threading.Thread(target=gui_watchdog,
                             args=(bridge, stop2, 0.3, 0.05, lambda: fired.append(1)))
        w.start()
        with bridge.gui_lock:
            time.sleep(0.1)
        time.sleep(0.4)
        stop2.set()
        w.join(2)
        self.assertEqual(fired, [])


class BoundedStatusTest(unittest.TestCase):
    def test_login_state_returns_last_known_when_gui_busy(self):
        b = Bridge(os.path.join(tempfile.mkdtemp(), "b.sqlite3"), token="t")
        b._login_probe = (time.monotonic() - 100, "logged_in")
        hold, done = threading.Event(), threading.Event()

        def busy():
            with b.gui_lock:
                hold.set()
                done.wait(3)

        t = threading.Thread(target=busy)
        t.start()
        hold.wait(2)
        t0 = time.monotonic()
        self.assertEqual(b._login_state_cached(lock_timeout=0.2), "logged_in")
        self.assertLess(time.monotonic() - t0, 1.0)
        done.set()
        t.join()
