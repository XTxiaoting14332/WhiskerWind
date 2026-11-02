import os
import tempfile
import unittest

from qqcache.messages import Message
from qqcache.ob11 import SYN_BASE
from qqcache.serve import Bridge


class DeliverEmitOrderTest(unittest.TestCase):
    """发帖与成员解析的顺序契约。

    默认 resolve_first（最大兼容）：触发先行、解析完才发帖——下游裸读
    event.user_id 拿到的总是尽力真号，合成号只是解析不动的兜底。
    低延迟模式（resolve_first=False）则事件先走、解析殿后，未解成员
    带稳定合成号发帖，qqcache_member_resolved 通知事后升级。
    """

    def _bridge(self):
        tmp = tempfile.mkdtemp()
        bridge = Bridge(os.path.join(tmp, "b.sqlite3"), token="t")
        bridge.nt_data = None
        return bridge

    def _msg(self, i, sender="成员甲", **kw):
        return Message(msg_id=str(i), kind="text", sender=sender, title=None,
                       content=f"c{i}", timestamp="14:42", is_self=False, **kw)

    def test_resolve_first_is_default(self):
        self.assertTrue(self._bridge().resolve_first)

    def test_resolve_first_posts_resolved_uin(self):
        bridge = self._bridge()
        order = []
        bridge.emit = lambda ev: order.append(("event", ev))
        bridge._nw_triggers = lambda emit, uin: order.append(("trigger", emit[0].msg_id))
        def resolve(app, group_uin, pending):
            order.append(("resolve", list(pending)))
            bridge.ids.member_user_id(group_uin, "成员甲", real_uin="2345678901")
        bridge._resolve_members = resolve
        posted = bridge._deliver_emit(None, "100000001", "group", [self._msg(1)])
        self.assertEqual(posted, 1)
        # 回复先行（不依赖真号），解析其后，事件最后——带的是真号
        self.assertEqual([s for s, _ in order], ["trigger", "resolve", "event"])
        self.assertEqual(order[2][1]["sender"]["user_id"], 2345678901)

    def test_resolution_exception_does_not_eat_events(self):
        # resolve-first 下解析抛错必须回落合成号发帖，不能吞掉整批
        bridge = self._bridge()
        posted = []
        bridge.emit = posted.append
        def boom(app, group_uin, pending):
            raise RuntimeError("gui blew up")
        bridge._resolve_members = boom
        n = bridge._deliver_emit(None, "100000001", "group", [self._msg(1)])
        self.assertEqual(n, 1)
        self.assertEqual(len(posted), 1)
        self.assertGreaterEqual(posted[0]["sender"]["user_id"], SYN_BASE)

    def test_emit_first_posts_before_resolution(self):
        bridge = self._bridge()
        bridge.resolve_first = False
        order = []
        bridge.emit = lambda ev: order.append(("event", ev))
        bridge._resolve_members = (
            lambda app, group_uin, pending: order.append(("resolve", dict(pending)))
        )
        posted = bridge._deliver_emit(None, "100000001", "group", [self._msg(1)])
        self.assertEqual(posted, 1)
        self.assertEqual([step for step, _ in order], ["event", "resolve"])
        self.assertEqual(list(order[1][1]), ["成员甲"])
        self.assertGreaterEqual(order[0][1]["sender"]["user_id"], SYN_BASE)

    def test_nothing_pending_no_resolution(self):
        bridge = self._bridge()
        calls = []
        bridge.emit = lambda ev: None
        bridge._resolve_members = lambda *a: calls.append(a)
        msg = self._msg(1)
        msg.is_self = True  # 自己的消息不进 pending
        self.assertEqual(bridge._deliver_emit(None, "1", "group", [msg]), 1)
        self.assertEqual(calls, [])

    def test_resolved_member_posts_instantly(self):
        # 已解成员没有 pending——两种模式下都立即发帖
        bridge = self._bridge()
        bridge.ids.member_user_id("100000001", "成员甲", real_uin="2345678901")
        events = []
        bridge.emit = events.append
        calls = []
        bridge._resolve_members = lambda *a: calls.append(a)
        bridge._deliver_emit(None, "100000001", "group", [self._msg(1)])
        self.assertEqual(events[0]["sender"]["user_id"], 2345678901)
        self.assertEqual(calls, [])

    def test_resolve_first_toggle_persists(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "b.sqlite3")
        b1 = Bridge(path, token="t")
        b1.dispatch("set_resolve_first", {"enabled": False})
        self.assertFalse(b1.resolve_first)
        b2 = Bridge(path, token="t")  # 重启后从偏好文件读回
        self.assertFalse(b2.resolve_first)
        self.assertFalse(b2.dispatch("get_resolve_first", {})["enabled"])


if __name__ == "__main__":
    unittest.main()
