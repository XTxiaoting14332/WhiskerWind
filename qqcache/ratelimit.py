"""Outbound send guard: rate limits + duplicate-content block.

A bot gone wrong (a loop, someone spamming a command) would otherwise make
the GUI type as fast as it can, which is the most direct way to get the
account flagged. Over the limit we *refuse* (retryable, nothing was sent)
instead of queueing and sending late.

A slot is reserved before the GUI is touched, so concurrent callers queued on
the gui lock are counted too; ``release`` gives it back when the send failed
without side effects (so a retry is not mistaken for a duplicate).
"""

import hashlib
import json
import threading
import time
from collections import deque

from .ob11 import ApiError

RATE_LIMITED = 1429

# (条数, 秒)：同一会话短窗/长窗 + 全局。GUI 发一条本身要 2–5s，这些上限
# 正常使用碰不到，只拦失控的循环
DEFAULT_LIMITS = {
    "conv_burst": (3, 10.0),
    "conv_minute": (12, 60.0),
    "global_minute": (20, 60.0),
}
DEFAULT_DUP_WINDOW = 30.0


# 这些失败发生在敲字之前，保证没有副作用（与 automatic 的 retryable 判定一致）
SIDE_EFFECT_FREE = ("身份验证失败", "发送目标被切换", "拒绝打开", "没有找到会话",
                    "残留内容", "当前状态", "不在无障碍树")


def side_effect_free(message):
    return any(k in message for k in SIDE_EFFECT_FREE)


class RateLimited(ApiError):
    def __init__(self, message, retry_after):
        super().__init__(RATE_LIMITED, message)
        self.retry_after = retry_after


def content_key(message):
    """Stable digest of a message (CQ string or segment list)."""
    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(" ".join(message.split()).encode("utf-8")).hexdigest()


class SendGuard:
    def __init__(self, limits=None, dup_window=DEFAULT_DUP_WINDOW, clock=time.monotonic):
        self.limits = dict(DEFAULT_LIMITS, **(limits or {}))
        self.dup_window = dup_window
        self.clock = clock
        self._lock = threading.Lock()
        self._sends = deque()   # (t, conv, key)，最旧在左

    def _prune(self, now):
        horizon = max([w for _, w in self.limits.values()] + [self.dup_window])
        while self._sends and now - self._sends[0][0] > horizon:
            self._sends.popleft()

    def reserve(self, conv, message):
        """Claim a send slot or raise RateLimited. Returns a ticket for release()."""
        conv = str(conv)
        key = content_key(message)
        with self._lock:
            now = self.clock()
            self._prune(now)
            for t, c, k in reversed(self._sends):
                if now - t > self.dup_window:
                    break
                if c == conv and k == key:
                    wait = self.dup_window - (now - t)
                    raise RateLimited(
                        f"{int(self.dup_window)}s 内已向 {conv} 发过相同内容，拒绝重复发送", wait)
            for name, (count, window) in self.limits.items():
                hits = [t for t, c, _ in self._sends
                        if now - t <= window and (name.startswith("global") or c == conv)]
                if len(hits) >= count:
                    wait = window - (now - hits[0])
                    scope = "全局" if name.startswith("global") else f"会话 {conv}"
                    raise RateLimited(
                        f"{scope}发送过快（{int(window)}s 内最多 {count} 条），"
                        f"{wait:.0f}s 后再试", wait)
            ticket = (now, conv, key)
            self._sends.append(ticket)
            return ticket

    def release(self, ticket):
        with self._lock:
            try:
                self._sends.remove(ticket)
            except ValueError:
                pass
