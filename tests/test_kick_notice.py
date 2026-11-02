import threading
import unittest
from unittest import mock

from qqcache import core, proto_onebot, serve


class FakeBridge:
    def __init__(self):
        self.gui_lock = threading.RLock()
        self._login_probe = None
        self.last_kick = None
        self.published = []

    def _app(self):
        return object()

    def publish(self, ev):
        self.published.append(ev)


class KickNoticeTest(unittest.TestCase):
    def test_kick_is_kept_until_logged_in_again(self):
        b = FakeBridge()
        with mock.patch.object(serve.login, "login_state", return_value="login_required"), \
             mock.patch.object(serve.login, "dismiss_notice",
                               return_value=("下线通知", "你的帐号在另一台设备登录")):
            self.assertEqual(serve._login_state(b), "login_required")
        self.assertEqual(b.last_kick["title"], "下线通知")
        self.assertEqual(b.last_kick["time"], int(b.published[0].time))
        # 之后的心跳没有弹框了：记录保留
        with mock.patch.object(serve.login, "login_state", return_value="login_required"), \
             mock.patch.object(serve.login, "dismiss_notice", return_value=None):
            serve._login_state(b)
        self.assertIsNotNone(b.last_kick)
        with mock.patch.object(serve.login, "login_state", return_value="logged_in"):
            serve._login_state(b)
        self.assertIsNone(b.last_kick)

    def test_onebot_renders_system_notification(self):
        ev = core.SystemNotification("下线通知", "x", time=1000.5)
        out = proto_onebot.render(ev, 1234567890)
        self.assertEqual(out, [{"time": 1000, "self_id": 1234567890, "post_type": "notice",
                                "notice_type": "qqcache_system_notification",
                                "title": "下线通知", "text": "x"}])


if __name__ == "__main__":
    unittest.main()
