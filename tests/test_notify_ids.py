import os
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch

from qqcache.notify import Service, Store


class _Unpacked:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class _Inv:
    def return_value(self, v):
        pass


class NotifyIdTest(unittest.TestCase):
    """D-Bus 编号（发给 QQ 的 ActionInvoked）与落库行号（mark_processed）
    不能混用：混用时标记打到别的行，处理过的通知每次重启都被回放。"""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(tmp, "n.sqlite3"))
        self.addCleanup(self.store.close)
        # 先塞几行历史，让行号和本进程的 D-Bus 编号（从 1 起）错开
        for _ in range(5):
            self.store.db.execute(
                "INSERT INTO notifications (received_at, app, summary, body, content,"
                " processed_at) VALUES (?, 'QQ', 'old', '', '', ?)",
                (time.time() - 3600, time.time() - 3600),
            )
        self.store.db.commit()
        self.actions = []
        self.handled = []
        self.svc = Service(self.store, verbose=False, invoke_all=True,
                           attributor=self.handled.append)
        self.svc._emit_action = lambda nid, action: self.actions.append((nid, action))

    def _notify(self, summary):
        self.svc._on_call(
            None, "sender", "/p", "org.freedesktop.Notifications", "Notify",
            _Unpacked(("QQ", 0, "", summary, "x：hi", ["default", "查看"], {}, 0)),
            _Inv(),
        )

    def _drain(self):
        self.svc._pending.put(None)
        self.svc._work()

    def test_live_notification_marks_its_own_row(self):
        self._notify("files")
        self._drain()
        self.assertEqual(self.handled, ["files"])
        self.assertEqual(self.store.unprocessed(), [])

    def test_invoke_live_uses_dbus_id_once(self):
        self.svc.connection = object()
        self._notify("files")
        self.assertTrue(self.svc.invoke_live("files"))
        self.assertEqual(self.actions, [(1, "default")])
        self.assertFalse(self.svc.invoke_live("files"))

    def test_withdrawn_notification_not_invoked(self):
        self.svc.connection = object()
        self._notify("files")
        from unittest.mock import Mock
        self.svc._on_call(Mock(), "QQ", "/p", "org.freedesktop.Notifications",
                          "CloseNotification", _Unpacked((1,)), _Inv())
        self.assertFalse(self.svc.invoke_live("files"))
        self.assertEqual(self.actions, [])

    def test_replay_skips_action_and_marks_row(self):
        self.store.db.execute(
            "INSERT INTO notifications (received_at, app, summary, body, content)"
            " VALUES (?, 'QQ', 'files', '', '')", (time.time(),),
        )
        self.store.db.commit()
        self.svc._replay_unprocessed()
        self._drain()
        # 上个进程的编号 QQ 不认识——回放不进 live 表，也就不会 invoke
        self.assertFalse(self.svc.invoke_live("files"))
        self.assertEqual(self.handled, ["files"])
        self.assertEqual(self.store.unprocessed(), [])

    def test_failed_attribution_still_marked(self):
        def boom(summary):
            raise RuntimeError("x")

        self.svc.attributor = boom
        self._notify("files")
        self._drain()
        self.assertEqual(self.store.unprocessed(), [])


class DefocusTest(unittest.TestCase):
    def test_sets_focus_none_and_flushes(self):
        from qqcache.serve import Bridge

        calls = []

        class _Lib:
            def __getattr__(self, name):
                fn = lambda *a: calls.append((name, a[1:])) or 1  # noqa: E731
                fn.argtypes = fn.restype = None
                setattr(self, name, fn)
                return fn

        with patch("ctypes.CDLL", return_value=_Lib()):
            self.assertTrue(Bridge._x_focus_pointer_root())
        names = [c[0] for c in calls]
        self.assertIn(("XSetInputFocus", (0, 0, 0)), calls)
        self.assertLess(names.index("XSetInputFocus"), names.index("XFlush"))
        self.assertIn("XCloseDisplay", names)



class DefocusDeferTest(unittest.TestCase):
    """失焦不能插进正在进行的 GUI 操作（点头像/复制号码）中间。"""

    def test_waits_for_gui_lock(self):
        import threading
        from qqcache.serve import Bridge

        b = Bridge(":memory:")
        b._defocus = unittest.mock.Mock()
        b._schedule_defocus = unittest.mock.Mock()
        held = threading.Event()
        release = threading.Event()

        def hold():
            with b.gui_lock:
                held.set()
                release.wait(2)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(2)
        b._defocus_when_idle()
        b._defocus.assert_not_called()
        b._schedule_defocus.assert_called_once()
        release.set()
        t.join()
        b._defocus_when_idle()
        b._defocus.assert_called_once()

if __name__ == "__main__":
    unittest.main()
