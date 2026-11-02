"""Protocol-neutral events and the hub that fans them out to protocol adapters.

The bridge reads QQ through its GUI and produces *core events*; every outward
protocol (OneBot v11, Satori, …) is an adapter that renders those events in its
own wire format. Adapters never talk to each other and never reach into the
GUI: inbound actions go through the bridge's own methods (``api_send`` etc.).

A core event carries the parsed :class:`~qqcache.messages.Message` itself — that
object is already protocol-free (what the GUI showed: sender, content, kind,
QQ's native msg_id) — plus what the bridge worked out around it (conversation
uin, the sender's uin and whether it is a real QQ number, media on disk, the
quoted message).
"""

import queue
import threading
import time
from dataclasses import dataclass, field

from loguru import logger

from .ob11 import SYN_BASE


@dataclass
class MessageCreated:
    conv_uin: str
    kind: str                     # "group" | "c2c"
    msg: object                   # messages.Message
    sender_uin: str               # real uin, or a synthetic id >= SYN_BASE
    role: str                     # "owner" | "admin" | "member"
    ob_id: int                    # OneBot int id (storage key in ob11_messages)
    cq: str                       # canonical stored rendering (CQ string)
    conv_name: str = ""
    media_path: str | None = None
    reply_ob_id: int | None = None
    reply_msg_id: str | None = None
    # True: 不是刚到达的消息（漏读后回填 / 积压补读），bot 不该当新命令处理
    backlog: bool = False
    time: float = field(default_factory=time.time)

    @property
    def sender_resolved(self):
        """False when sender_uin is a synthetic stand-in, not a QQ number."""
        return not (str(self.sender_uin).isdigit() and int(self.sender_uin) >= SYN_BASE)


@dataclass
class MemberResolved:
    group_uin: str
    name: str
    user_id: str                  # the real uin
    was_user_id: str              # the synthetic id events carried before
    time: float = field(default_factory=time.time)


@dataclass
class SystemNotification:
    """A QQ system toast (加群申请, 好友申请, 退群…): read-only, title + text
    as QQ showed them. Not actionable through the GUI."""
    title: str
    text: str
    time: float = field(default_factory=time.time)


@dataclass
class Heartbeat:
    interval: float               # seconds
    state: str                    # login state, see login.login_state
    time: float = field(default_factory=time.time)


@dataclass
class LoginChanged:
    prev: str | None
    state: str
    time: float = field(default_factory=time.time)


@dataclass
class Connected:
    """The service came up (OneBot lifecycle/connect)."""
    interval: float
    time: float = field(default_factory=time.time)


class ProtocolHub:
    """Fan core events out to every registered adapter.

    One adapter failing must not starve the others (or the GUI thread that
    published the event), so each call is isolated.
    """

    def __init__(self):
        self._adapters = {}
        self._lock = threading.Lock()

    def register(self, adapter):
        with self._lock:
            self._adapters[adapter.name] = adapter
        return adapter

    def unregister(self, name):
        with self._lock:
            return self._adapters.pop(name, None)

    def get(self, name):
        with self._lock:
            return self._adapters.get(name)

    def adapters(self):
        with self._lock:
            return list(self._adapters.values())

    def publish(self, event):
        for adapter in self.adapters():
            try:
                adapter.handle(event)
            except Exception as exc:  # noqa: BLE001 - isolate adapters
                logger.warning(f"协议 {adapter.name} 处理 {type(event).__name__} 失败: {exc}")


class QueuedWsClient:
    """Events go through a per-connection queue + writer thread: a slow or
    dead client must never block the GUI thread that published the event."""

    def __init__(self, conn):
        self.conn = conn
        self.q = queue.Queue(maxsize=2000)
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    def send(self, text):
        try:
            self.q.put_nowait(text)
        except queue.Full:
            logger.warning("ws 客户端积压过多，断开")
            self.conn.close()

    def _writer(self):
        while True:
            text = self.q.get()
            if text is None:
                self.conn.close()
                return
            try:
                self.conn.send_text(text)
            except OSError:
                self.conn.close()
                return

    def close(self, timeout=2.0):
        """Flush what is queued (e.g. a final error frame), then close."""
        try:
            self.q.put_nowait(None)
        except queue.Full:
            self.conn.close()
            return
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        self.conn.close()
