import unittest

from qqcache import core, proto_onebot
from qqcache.messages import Message


def _msg(**kw):
    base = dict(msg_id="7703", kind="text", sender="A", is_self=False,
                content="hi", title="", quoted_sender=None, quoted_content=None,
                timestamp="0")
    base.update(kw)
    return Message(**base)


class HubTest(unittest.TestCase):
    def test_failing_adapter_does_not_starve_others(self):
        got = []

        class Bad:
            name = "bad"

            def handle(self, ev):
                raise RuntimeError("boom")

        class Good:
            name = "good"

            def handle(self, ev):
                got.append(ev)

        hub = core.ProtocolHub()
        hub.register(Bad())
        hub.register(Good())
        hub.publish(core.Heartbeat(interval=5, state="logged_in"))
        self.assertEqual(len(got), 1)

    def test_sender_resolved(self):
        ev = core.MessageCreated("1", "group", _msg(), "900000000001", "member", 1, "hi")
        self.assertFalse(ev.sender_resolved)
        ev.sender_uin = "2345678901"
        self.assertTrue(ev.sender_resolved)


class OneBotRenderTest(unittest.TestCase):
    def test_group_message(self):
        ev = core.MessageCreated("100000001", "group", _msg(), "2345678901", "member", 42, "hi",
                                 time=1000)
        [out] = proto_onebot.render(ev, 1234567890)
        self.assertEqual(out["post_type"], "message")
        self.assertEqual(out["group_id"], 100000001)
        self.assertEqual(out["user_id"], 2345678901)
        self.assertEqual(out["message_id"], 42)
        self.assertEqual(out["time"], 1000)

    def test_member_resolved_and_meta(self):
        [n] = proto_onebot.render(core.MemberResolved("1", "A", "2345678901", "900000000001"), 9)
        self.assertEqual(n["notice_type"], "qqcache_member_resolved")
        self.assertEqual(n["was_user_id"], 900000000001)
        [h] = proto_onebot.render(core.Heartbeat(interval=5, state="logged_in"), 9)
        self.assertEqual(h["interval"], 5000)
        self.assertTrue(h["status"]["online"])
        [c] = proto_onebot.render(core.Connected(interval=5), 9)
        self.assertEqual((c["meta_event_type"], c["sub_type"]), ("lifecycle", "connect"))
