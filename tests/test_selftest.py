import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from qqcache import selftest
from qqcache.notify import SELFTEST_SUMMARY, Store, parse


class _Variant:
    def __init__(self, *a):
        pass


class _VariantType:
    @staticmethod
    def new(t):
        return t


class _Gio:
    class DBusCallFlags:
        NONE = 0


class _GLib:
    Variant = _Variant
    VariantType = _VariantType


class _Unpacked:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class _FakeConn:
    """Mirrors the real bus round-trip: NameHasOwner answers, Notify writes
    the probe row like Service._on_call would."""

    def __init__(self, db_path, owned=True, nid=7):
        self.db_path = db_path
        self.owned = owned
        self.nid = nid
        self.notified = []

    def call_sync(self, bus, path, iface, method, params, reply_type,
                  flags, timeout, cancellable):
        if method == "NameHasOwner":
            return _Unpacked((self.owned,))
        if method == "Notify":
            self.notified.append(params)
            with Store(self.db_path) as st:
                st.add(parse("qqcache-selftest", SELFTEST_SUMMARY, "自检探针"))
            return _Unpacked((self.nid,))
        raise AssertionError(f"unexpected call {method}")


class _Bridge:
    """Bridge 替身：只暴露自检用到的面。"""

    def __init__(self, tmp, resolved=("100000001", "group"), posted=2):
        self.db_path = os.path.join(tmp, "b.sqlite3")
        self.gui_lock = threading.RLock()
        self.resolved = resolved
        self.posted = posted
        self.app = object()
        self.poll_calls = []

    def _app(self):
        return self.app

    def _resolve_uin(self, app, name, summary):
        return self.resolved

    def poll_emit(self, app, uin, kind, notified=False):
        self.poll_calls.append((uin, kind, notified))
        return self.posted


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _bridge(self, **kw):
        return _Bridge(self.tmp, **kw)

    def _patches(self, conn=None, conv_open=True, conv_name="files"):
        conn = conn if conn is not None else _FakeConn(
            os.path.join(self.tmp, "b.sqlite3"))
        return [
            patch.object(selftest.login, "login_state", lambda app: "logged_in"),
            patch.object(selftest, "_session_bus", lambda: (conn, _Gio, _GLib)),
            patch.object(selftest, "open_conversation",
                         lambda app, name, timeout=3.0: conv_open),
            patch.object(selftest, "current_conversation",
                         lambda app: conv_name),
        ]

    def test_all_pass(self):
        bridge = self._bridge()
        conn = _FakeConn(bridge.db_path)
        patches = self._patches(conn=conn)
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)

        report = selftest.run(bridge, "files")

        self.assertTrue(report["ok"])
        self.assertIsNone(report["failed_stage"])
        names = [s["name"] for s in report["stages"]]
        self.assertEqual(
            names,
            ["atspi", "login", "notify_bus", "notify", "open", "attribute", "emit"],
        )
        self.assertTrue(all(s["ok"] for s in report["stages"]))
        # emit 段真的走到了 poll_emit
        self.assertEqual(bridge.poll_calls, [("100000001", "group", True)])
        # 探针行已被标已处理，重启回放不会捡它
        with Store(bridge.db_path) as st:
            row = st.db.execute(
                "SELECT processed_at FROM notifications WHERE summary = ?",
                (SELFTEST_SUMMARY,),
            ).fetchone()
        self.assertIsNotNone(row[0])
        # 报告已追加进 jsonl
        self.assertTrue(os.path.exists(bridge.db_path + ".selftest.jsonl"))

    def test_fail_fast_records_breakpoint(self):
        bridge = self._bridge()
        patches = self._patches(conv_open=False)
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)

        report = selftest.run(bridge, "files")

        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_stage"], "open")
        names = [s["name"] for s in report["stages"]]
        self.assertEqual(names, ["atspi", "login", "notify_bus", "notify", "open"])
        self.assertEqual(bridge.poll_calls, [])

    def test_login_gate(self):
        bridge = self._bridge()
        patches = self._patches()
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        with patch.object(selftest.login, "login_state",
                          lambda app: "login_required"):
            report = selftest.run(bridge, "files")
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_stage"], "login")

    def test_bus_not_owned(self):
        bridge = self._bridge()
        conn = _FakeConn(bridge.db_path, owned=False)
        patches = self._patches(conn=conn)
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        report = selftest.run(bridge, "files")
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_stage"], "notify_bus")

    def test_attribute_rejected(self):
        bridge = self._bridge(resolved=(None, None))
        patches = self._patches()
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        report = selftest.run(bridge, "files")
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_stage"], "attribute")

    def test_open_name_mismatch(self):
        bridge = self._bridge()
        patches = self._patches(conv_name="别的人")
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        report = selftest.run(bridge, "files")
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_stage"], "open")
        self.assertIn("不符", report["stages"][-1]["detail"])


class ServiceProbeSkipTest(unittest.TestCase):
    """SELFTEST_SUMMARY 的通知只落库不进 worker——否则探针会再触发
    一整条归因链。"""

    def test_probe_not_enqueued(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "n.sqlite3"))
        try:
            from qqcache.notify import Service

            svc = Service(store, verbose=False, invoke_all=True, attributor=lambda s: None)
            captured = []

            class _Inv:
                def return_value(self, v):
                    captured.append(v)

            svc._on_call(
                None, "sender", "/p", "org.freedesktop.Notifications", "Notify",
                _Unpacked(
                    ("qqcache-selftest", 0, "", SELFTEST_SUMMARY, "自检探针", [], {}, 0)
                ),
                _Inv(),
            )
            self.assertTrue(svc._pending.empty())
            self.assertEqual(store.count(), 1)

            # 普通通知照样进队
            svc._on_call(
                None, "sender", "/p", "org.freedesktop.Notifications", "Notify",
                _Unpacked(("QQ", 0, "", "files", "x：hi", ["default"], {}, 0)),
                _Inv(),
            )
            self.assertFalse(svc._pending.empty())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
