import unittest

from qqcache import automatic
from qqcache.ratelimit import RateLimited, SendGuard, side_effect_free


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SendGuardTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.g = SendGuard(clock=self.clock)

    def test_duplicate_blocked_then_allowed_after_window(self):
        self.g.reserve("100000001", "hi")
        with self.assertRaises(RateLimited):
            self.g.reserve("100000001", " hi ")   # 空白差异不算不同内容
        self.g.reserve("100000002", "hi")        # 别的会话不受影响
        self.clock.t += 31
        self.g.reserve("100000001", "hi")

    def test_segment_lists_compare_by_content(self):
        self.g.reserve("1", [{"type": "text", "data": {"text": "a"}}])
        with self.assertRaises(RateLimited):
            self.g.reserve("1", [{"data": {"text": "a"}, "type": "text"}])

    def test_conversation_burst_and_global_limits(self):
        for i in range(3):
            self.g.reserve("1", f"m{i}")
        with self.assertRaises(RateLimited) as cm:
            self.g.reserve("1", "m3")
        self.assertGreater(cm.exception.retry_after, 0)
        self.clock.t += 11
        self.g.reserve("1", "m3")
        g = SendGuard(clock=self.clock)
        for i in range(20):
            self.clock.t += 2.5
            g.reserve(str(i), "x")
        with self.assertRaises(RateLimited):
            g.reserve("new", "x")

    def test_release_returns_slot(self):
        t = self.g.reserve("1", "hi")
        self.g.release(t)
        self.g.reserve("1", "hi")

    def test_error_mapping(self):
        e = automatic.from_api_error(RateLimited("过快", 3))
        self.assertEqual((e.code, e.retryable), ("rate_limited", True))
        self.assertTrue(side_effect_free("目标 1 身份验证失败，拒绝发送"))
        self.assertFalse(side_effect_free("组装后发送按钮仍未启用"))


if __name__ == "__main__":
    unittest.main()
