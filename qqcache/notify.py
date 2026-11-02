"""Own org.freedesktop.Notifications and turn QQ's toasts into events.

QQ never tells us which conversation a notification belongs to by id: the
payload carries only a display name. For groups the body is
``显示名：正文``; for private chats it is the bare text. Mentions get an
extra ``[有人@我] `` prefix, which is the only reliable "I was pinged" signal
anywhere in the client.

Body text is truncated at 128 characters by QQ itself, so anything that long is
flagged and should be re-read from the accessibility tree instead.

Every Notify call is persisted synchronously: QQ actively withdraws old
notifications with CloseNotification, so the daemon's in-memory history is not
a source of truth.
"""

import json
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from loguru import logger

MENTION_PREFIX = "[有人@我]"
# 群通知的发送者前缀就是显示名（有群名片时显示名片，否则昵称），老 QQ 的
# 「【名片】昵称」复合形态在 NT QQ 已经不存在。显示名本身可以含【】，所以
# 整个冒号前缀是一个不可分割的名字。
SENDER_PREFIX = re.compile(r"^(?P<sender>[^：\n]{1,40})：(?P<content>.*)$", re.S)
TRUNCATION_LIMIT = 128

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
# 自检探针的保留 summary：这种 Notify 只落库验证「D-Bus->存储」一跳，
# 不进 worker——否则 worker 会拿假会话名把整个归因链又跑一遍
SELFTEST_SUMMARY = "qqcache·selftest"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL NOT NULL,
    app         TEXT NOT NULL,
    summary     TEXT NOT NULL,
    body        TEXT NOT NULL,
    urgency     INTEGER NOT NULL DEFAULT 1,
    mention     INTEGER NOT NULL DEFAULT 0,
    is_group    INTEGER NOT NULL DEFAULT 0,
    sender_card TEXT,
    sender_name TEXT,
    content     TEXT NOT NULL,
    truncated   INTEGER NOT NULL DEFAULT 0,
    replaces_id INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_notifications_time ON notifications(received_at DESC);
"""


@dataclass
class Notification:
    app: str
    summary: str
    body: str
    urgency: int = 1
    mention: bool = False
    is_group: bool = False
    sender_card: str | None = None
    sender_name: str | None = None
    content: str = ""
    truncated: bool = False
    replaces_id: int = 0
    received_at: float = field(default_factory=time.time)
    row_id: int | None = None

    @property
    def sender_display(self):
        """通知里看到的发送者显示名；与 sender_name 同义，保留作 API 字段。"""
        return self.sender_name

    def as_dict(self):
        return {
            "id": self.row_id,
            "received_at": self.received_at,
            "app": self.app,
            "summary": self.summary,
            "urgency": self.urgency,
            "mention": self.mention,
            "is_group": self.is_group,
            "sender_card": self.sender_card,
            "sender_name": self.sender_name,
            "sender_display": self.sender_display,
            "content": self.content,
            "truncated": self.truncated,
        }


def parse(app, summary, body, urgency=1, replaces_id=0, kind=None):
    """Split a raw D-Bus payload into conversation, sender and content.

    The body alone is ambiguous: a private message may legitimately read
    ``注意：明天开会``, which looks exactly like a group's ``显示名：正文``
    prefix. So the conversation kind from the name->uin cache decides when
    available; without it we only trust a prefix that starts with 【.
    """
    mention = body.startswith(MENTION_PREFIX)
    if mention:
        body = body[len(MENTION_PREFIX) :].lstrip()

    match = SENDER_PREFIX.match(body)
    if kind == "c2c":
        is_group = False
    elif kind == "group":
        is_group = True
    else:
        # 没有 kind 提示时只能猜：带【】的显示名前缀是群消息的强信号（私聊
        # 正文恰好以「【…】…：」开头的概率低得多），其它形态保守判私聊。
        is_group = bool(match and match.group("sender").startswith("【"))

    if is_group and match:
        nick, content = match.group("sender"), match.group("content")
    else:
        nick = None
        content = body
    card = None

    return Notification(
        app=app,
        summary=summary,
        body=body,
        urgency=urgency,
        mention=mention,
        is_group=is_group,
        sender_card=card,
        sender_name=nick,
        content=content,
        truncated=len(content) >= TRUNCATION_LIMIT,
        replaces_id=replaces_id,
    )


def parse_hints(hints):
    urgency = hints.get("urgency", 1)
    if isinstance(urgency, (bytes, bytearray)):
        urgency = urgency[0] if urgency else 1
    return int(urgency)


class Store:
    def __init__(self, path):
        # GIO 的 D-Bus 派发线程和工作线程都会写这张表
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # processed_at 后补：缺列才 ALTER，老库平滑迁移
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(notifications)")}
        if "processed_at" not in cols:
            self.db.execute("ALTER TABLE notifications ADD COLUMN processed_at REAL")
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def add(self, event):
        cursor = self.db.execute(
            "INSERT INTO notifications (received_at, app, summary, body, urgency, mention,"
            " is_group, sender_card, sender_name, content, truncated, replaces_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.received_at,
                event.app,
                event.summary,
                event.body,
                event.urgency,
                int(event.mention),
                int(event.is_group),
                event.sender_card,
                event.sender_name,
                event.content,
                int(event.truncated),
                event.replaces_id,
            ),
        )
        self.db.commit()
        event.row_id = cursor.lastrowid
        return event

    def recent(self, limit=20, since=None):
        if since is None:
            rows = self.db.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM notifications WHERE id > ? ORDER BY id ASC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    def mark_processed(self, notification_id):
        self.db.execute(
            "UPDATE notifications SET processed_at = ? WHERE id = ?",
            (time.time(), notification_id),
        )
        self.db.commit()

    def notified_summaries(self, days=30.0):
        """近期为普通消息发过通知的会话名——未读巡检只补这些。「接收但不
        提醒」的群攒 99+ 未读也从不通知；「只提醒@」的群只有 @ 才通知，
        平时的未读不代表漏了通知（mention=1 的不算数）。"""
        rows = self.db.execute(
            "SELECT DISTINCT summary FROM notifications"
            " WHERE received_at > ? AND mention = 0",
            (time.time() - days * 86400,),
        ).fetchall()
        return {r[0] for r in rows if r[0]}

    def unprocessed(self, window=600.0, limit=50):
        """落库但归因没跑完的通知——进程崩溃只蒸发内存队列，行还在。"""
        rows = self.db.execute(
            "SELECT id, summary, received_at FROM notifications"
            " WHERE processed_at IS NULL AND received_at > ?"
            " ORDER BY id ASC LIMIT ?",
            (time.time() - window, limit),
        ).fetchall()
        return [dict(r) for r in rows]


INTROSPECTION = """
<node>
  <interface name="org.freedesktop.Notifications">
    <method name="Notify">
      <arg type="s" name="app_name" direction="in"/>
      <arg type="u" name="replaces_id" direction="in"/>
      <arg type="s" name="app_icon" direction="in"/>
      <arg type="s" name="summary" direction="in"/>
      <arg type="s" name="body" direction="in"/>
      <arg type="as" name="actions" direction="in"/>
      <arg type="a{sv}" name="hints" direction="in"/>
      <arg type="i" name="expire_timeout" direction="in"/>
      <arg type="u" name="id" direction="out"/>
    </method>
    <method name="CloseNotification">
      <arg type="u" name="id" direction="in"/>
    </method>
    <method name="GetCapabilities">
      <arg type="as" name="capabilities" direction="out"/>
    </method>
    <method name="GetServerInformation">
      <arg type="s" name="name" direction="out"/>
      <arg type="s" name="vendor" direction="out"/>
      <arg type="s" name="version" direction="out"/>
      <arg type="s" name="spec_version" direction="out"/>
    </method>
    <signal name="NotificationClosed">
      <arg type="u" name="id"/>
      <arg type="u" name="reason"/>
    </signal>
    <signal name="ActionInvoked">
      <arg type="u" name="id"/>
      <arg type="s" name="action_key"/>
    </signal>
  </interface>
  <interface name="org.qqcache.Notify">
    <method name="Invoke">
      <arg type="u" name="id" direction="in"/>
      <arg type="s" name="action" direction="in"/>
    </method>
  </interface>
</node>
"""


class Service:
    """Minimal notification server; persists every Notify before returning."""

    def __init__(self, store, app_filter=None, on_event=None, verbose=True, resolver=None,
                 attributor=None, invoke_all=False, gate=None):
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        self.Gio = Gio
        self.GLib = GLib
        self.store = store
        self.app_filter = app_filter
        self.on_event = on_event
        self.verbose = verbose
        self.resolver = resolver
        self.attributor = attributor
        # gate(summary) -> bool：通知处理前是否允许让 QQ 切会话。serve 侧用它
        # 做「用户活跃时不抢窗口」的闸——gate 拒绝则跳过这条通知。
        self.gate = gate
        # invoke_all: queue an ActionInvoked for every notification, not just
        # cache misses — the serve layer uses the route to open the chat and
        # read the full message, so known conversations need it too.
        self.invoke_all = invoke_all
        self.loop = GLib.MainLoop()
        self.next_id = 1
        self.owner = None
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self.registration = None
        self._pending = queue.Queue()
        self._worker = None
        # summary -> (dbus_id, action)：QQ 还没撤回的最新一条通知。QQ 会很快
        # CloseNotification（新消息顶替、手机端已读同步），撤回后再发
        # ActionInvoked 它不理——只有活着的通知值得 invoke。
        self._live = {}
        self._live_lock = threading.Lock()

    def _emit_action(self, notification_id, action):
        """ActionInvoked from our held bus name — this is what makes QQ open
        the conversation behind a notification, so the peer's uin can be read
        out of the GUI afterwards."""
        self.connection.emit_signal(
            None, OBJECT_PATH, "org.freedesktop.Notifications", "ActionInvoked",
            self.GLib.Variant("(us)", (notification_id, action)),
        )

    def invoke_live(self, summary):
        """对 summary 最新一条未撤回的通知发 ActionInvoked（让 QQ 自己切到
        该会话）。返回是否真的发了——通知已被 QQ 撤回时返回 False，调用方
        改走主动打开。调用方须持 GUI 锁：切会话不能插进别的发送操作中间。"""
        with self._live_lock:
            live = self._live.pop(summary, None)
        if live is None or getattr(self, "connection", None) is None:
            return False
        self._emit_action(*live)
        return True

    def _work(self):
        """Serialize attribution jobs off the D-Bus thread.

        ActionInvoked 不在这里发：worker 排到时 QQ 往往已经把通知撤回了，
        而且切会话必须在 GUI 锁里做——attributor 拿到锁后调 invoke_live。

        job = (dbus_id, row_id, action, summary)：dbus_id 是 Notify 返回给
        QQ 的编号，row_id 是落库行号（mark_processed 必须用它）。两者曾经
        混用：标记打到了别的行，处理过的通知每次重启都被回放。
        """
        while True:
            job = self._pending.get()
            if job is None:
                return
            _dbus_id, row_id, _action, summary = job
            try:
                if self.gate is not None and not self.gate(summary):
                    if self.verbose:
                        logger.info(f"通知 {summary!r}: gate 拒绝，跳过")
                    continue
                self.attributor(summary)
            except Exception as exc:
                if self.verbose:
                    logger.warning(f"归因失败 {summary!r}: {exc}")
            finally:
                # 异常也标记：回放只为「进程崩溃蒸发了内存队列」兜底，
                # 处理过但失败的通知重放只会再失败一次、反复堵队列
                if row_id is not None:
                    try:
                        self.store.mark_processed(row_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"标记通知已处理失败 {row_id}: {exc}")

    def _replay_unprocessed(self, window=600.0):
        """崩溃恢复：进程死只丢内存队列，落库的通知重新排回 worker。
        每个 summary 只回放最新一条（同会话旧通知由这次打开全覆盖），
        poll_emit 的 cursor 去重兜底重复 emit。"""
        try:
            rows = self.store.unprocessed(window)
        except Exception as exc:
            logger.warning(f"崩溃恢复查询失败: {exc}")
            return
        latest = {}
        for r in rows:
            latest[r["summary"]] = r["id"]
        # 同会话旧通知由最新一条的回放全覆盖——直接标已处理，
        # 不然它们永远 NULL，窗口期内每次启动都白白回放
        covered = [r["id"] for r in rows if r["id"] not in latest.values()]
        for nid in covered:
            self.store.mark_processed(nid)
        for summary, nid in latest.items():
            logger.info(f"崩溃恢复: 回放通知 {summary!r} (id={nid})")
            self._pending.put((None, nid, "default", summary))

    def _on_bus_acquired(self, connection, name):
        self.connection = connection
        self.registration = connection.register_object(
            OBJECT_PATH, self.node.interfaces[0], self._on_call, None, None
        )
        connection.register_object(
            OBJECT_PATH, self.node.interfaces[1], self._on_call, None, None
        )
        if self.verbose:
            logger.success(f"已接管 {name} ({OBJECT_PATH})")
        # bus 拿到才算服务就绪（之前回放会让 _emit_action 撞上 connection=None）
        if self.attributor:
            self._replay_unprocessed()

    def _on_name_lost(self, connection, name):
        if self.verbose:
            logger.warning(f"失去总线名 {name}，可能已有其它通知守护进程")
        self.loop.quit()

    def _on_call(self, connection, sender, path, interface, method, params, invocation):
        Gio, GLib = self.Gio, self.GLib
        if method == "Notify":
            app_name, replaces_id, app_icon, summary, body, actions, hints, expire = params.unpack()
            if self.verbose:
                logger.debug(
                    f"raw actions={actions!r} hints={sorted(hints)!r} icon={app_icon!r}"
                )
            kind = self.resolver(summary) if self.resolver else None
            event = parse(app_name, summary, body, parse_hints(hints), replaces_id, kind=kind)
            self.store.add(event)
            notification_id = self.next_id
            self.next_id += 1
            # 通知只排队给 attributor，由 serve 决定何时、如何打开会话；这里不发
            # ActionInvoked，避免和正在进行的发送抢窗口。invoke_all 时已知会话也排队
            if (self.attributor and (self.invoke_all or kind is None)
                    and summary != SELFTEST_SUMMARY):
                action = "default" if "default" in actions else (actions[0] if actions else "default")
                with self._live_lock:
                    self._live[summary] = (notification_id, action)
                self._pending.put((notification_id, event.row_id, action, summary))
            if self.on_event:
                self.on_event(event)
            elif self.verbose:
                flag = " @我" if event.mention else ""
                cut = " (截断)" if event.truncated else ""
                logger.info(
                    f"[{event.summary}] {event.sender_display or '-'}: {event.content}{flag}{cut}"
                )
            invocation.return_value(GLib.Variant("(u)", (notification_id,)))
        elif method == "CloseNotification":
            (notification_id,) = params.unpack()
            with self._live_lock:
                for k in [k for k, v in self._live.items() if v[0] == notification_id]:
                    del self._live[k]
            connection.emit_signal(
                None, OBJECT_PATH, interface, "NotificationClosed",
                GLib.Variant("(uu)", (notification_id, 3)),
            )
            invocation.return_value(None)
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["body", "actions", "persistence"],)))
        elif method == "GetServerInformation":
            invocation.return_value(GLib.Variant("(ssss)", ("qqcache", "qqcache", "0.1", "1.2")))
        elif method == "Invoke":
            notification_id, action = params.unpack()
            self._emit_action(notification_id, action)
            invocation.return_value(None)
        else:
            invocation.return_value(None)
        return True

    def run(self):
        Gio = self.Gio
        self.owner = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            None,
            self._on_name_lost,
        )
        if self.attributor:
            self._worker = threading.Thread(target=self._work, daemon=True)
            self._worker.start()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            if self._worker is not None:
                self._pending.put(None)
            if self.registration is not None:
                self.connection.unregister_object(self.registration)
            if self.owner is not None:
                Gio.bus_unown_name(self.owner)


MONITOR_RULE = "interface='org.freedesktop.Notifications',member='Notify'"


def _unescape(text):
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            i += 1
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(text[i], text[i]))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def iter_monitor_events(stream):
    """Yield (app, replaces_id, summary, body) from dbus-monitor output.

    dbus-python's BecomeMonitor returns success but never routes the monitored
    messages to a filter, so monitor mode shells out to dbus-monitor instead.
    Only the scalar head of each Notify call is read; the hints array (which
    carries a full avatar bitmap) is skipped.
    """
    scalars = []
    in_block = False
    for raw in stream:
        line = raw.rstrip("\n")
        if line.startswith("method call") and "member=Notify" in line:
            in_block = True
            scalars = []
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped.startswith("array [") or stripped.startswith("dict entry"):
            in_block = False
            if len(scalars) >= 5:
                yield scalars[0], scalars[1], scalars[3], scalars[4]
            continue
        if line.startswith("   string "):
            body = line[len("   string ") :]
            if body.startswith('"') and body.endswith('"'):
                body = body[1:-1]
            scalars.append(_unescape(body))
        elif line.startswith("   uint32 "):
            try:
                scalars.append(int(line.split()[-1]))
            except ValueError:
                scalars.append(0)
        elif line.startswith("method call") or line.startswith("signal "):
            in_block = False


def monitor(store, verbose=True, on_event=None, rule=MONITOR_RULE, resolver=None):
    """Eavesdrop on Notify calls without owning the bus name.

    Runs alongside an existing daemon (swaync, dunst, ...), which is what makes
    testing outside the container possible.
    """
    import shutil
    import subprocess

    if not shutil.which("dbus-monitor"):
        raise RuntimeError("需要 dbus-monitor")

    process = subprocess.Popen(
        ["dbus-monitor", "--session", rule],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if verbose:
        logger.info(f"被动监听 {rule}")
    try:
        for app_name, replaces_id, summary, body in iter_monitor_events(process.stdout):
            kind = resolver(summary) if resolver else None
            event = parse(app_name, summary, body, 1, replaces_id, kind=kind)
            store.add(event)
            if on_event:
                on_event(event)
            elif verbose:
                flag = " @我" if event.mention else ""
                cut = " (截断)" if event.truncated else ""
                logger.info(f"[{event.summary}] {event.sender_display or '-'}: {event.content}{flag}{cut}")
    finally:
        process.terminate()
