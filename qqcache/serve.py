"""Runtime bridge: QQ GUI <-> OneBot v11.

A single GUI lock serialises everything that touches the client — the
notification worker's open+poll cycle and HTTP API sends compete for the same
window. Notifications are only triggers: once ActionInvoked makes QQ open the
conversation, the full contents are read back through the accessibility tree
(including backfill when the viewport skipped), then posted upstream.
"""

import json
import os
import re
import subprocess
import threading
import time

from loguru import logger

from . import atspi, backfill, core, login, peer, sweep
from .cursor import Cursor
from .logbuf import LogBuffer
from .media import MediaIndex, correlate, find_nt_data
from .messages import parse_messages
from .proto_onebot import OneBotAdapter
from .ratelimit import RateLimited, SendGuard, side_effect_free
from .ob11 import (
    SYN_BASE,
    ApiError,
    IdMap,
    Poster,
    WsHub,
    message_to_cq,
    meta_event,  # noqa: F401 - re-export (moved to ob11)
    normalize_segments,
    ops_from_segments,
    resolve_file_ref,
    segments_to_cq,
)
from .send import (
    SendError,
    _main_window_id,
    _names_match,
    activate_qq,
    current_conversation,
    ensure_message_tab,
    on_message_tab,
    open_conversation,
    send,
)
from .store import Cache, is_uin

MEMBER_COUNT_RE = re.compile(r"[（(]\s*(\d+)\s*人[）)]")


_MACHINE = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}


def status_text(uptime_s):
    """#ww 触发器的回复文本。"""
    import platform
    import sys

    from . import __version__

    days, rem = divmod(int(uptime_s), 86400)
    hours, rem = divmod(rem, 3600)
    machine = _MACHINE.get(platform.machine().lower(), platform.machine())
    return (
        "WhiskerWind 状态\n"
        f"版本: {__version__}\n"
        f"平台: {sys.platform}-{machine}\n"
        f"运行时长: {days}天 {hours}小时 {rem // 60}分钟"
    )


class TrackedRLock:
    """RLock that knows who holds it and since when (outermost acquire).

    The GUI lock is the single choke point of the service: a native AT-SPI
    call that never returns while holding it freezes polling, notifications,
    sends and every status request. gui_watchdog() uses ``held()`` to notice
    and restart the process instead of hanging forever.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._owner = None
        self._depth = 0
        self._since = None

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            if self._depth == 0:
                self._owner = threading.get_ident()
                self._since = time.monotonic()
            self._depth += 1
        return ok

    def release(self):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._since = None
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()

    def _is_owned(self):
        return self._owner == threading.get_ident()

    def held(self):
        """(owner thread ident, seconds held) or None."""
        owner, since = self._owner, self._since
        if owner is None or since is None:
            return None
        return owner, time.monotonic() - since


GUI_WEDGE_SECS = 180.0


def gui_watchdog(bridge, stop, limit=GUI_WEDGE_SECS, check=10.0, exit_fn=None):
    """Restart the process when the GUI lock has been held too long.

    Seen in production: a libatspi call blocked in dbind_send_and_allow_reentry
    for many minutes despite Atspi.set_timeout, holding the GUI lock; every
    other thread queued behind it and status requests piled up by the
    hundred. Nothing in-process can cancel a stuck native call, so log the
    holder's stack and exit — the entrypoint respawns serve within ~10s and
    QQ itself is untouched. The longest legitimate holder (backfill plus
    member resolution) stays well under ``limit``.
    """
    import sys
    import traceback

    while not stop.wait(check):
        held = bridge.gui_lock.held()
        if not held or held[1] < limit:
            continue
        owner, secs = held
        frame = sys._current_frames().get(owner)
        stack = "".join(traceback.format_stack(frame)[-12:]) if frame else "(无栈)"
        logger.critical(f"GUI 锁被同一操作占用 {secs:.0f}s，判定卡死，重启 serve。持锁线程栈:\n{stack}")
        (exit_fn or (lambda: os._exit(70)))()
        return


def _member_count(name):
    m = MEMBER_COUNT_RE.search(name or "")
    return int(m.group(1)) if m else 0


class Bridge:
    """All state the service needs; one instance per process."""

    def __init__(self, db_path, self_id=0, self_name="", post_urls=(),
                 token=None, secret=None, verify_identity=True, log_buffer=None,
                 test_trigger=False):
        from .transports import TransportManager

        self.db_path = db_path
        self.cache = Cache(db_path)
        self.cursor = Cursor(db_path)
        self.ids = IdMap(db_path)
        detected = login.detect_self_uin()
        # self_id 可能是字符串 "0"（arg 默认值），先归一成 int 再判断
        self_id = int(self_id or 0)
        if not self_id and detected:
            logger.info(f"从分区目录检测到登录账号: {detected}")
        prof = login.detect_self_profile()
        self.self_id = self_id or int(detected or prof.get("uin") or 0)
        self.self_name = self_name or prof.get("nickname", "")
        self.self_face_url = prof.get("face_url", "")
        if self.self_name:
            logger.info(f"登录账号资料: {self.self_name} ({self.self_id})")
        self.access_token = token
        self.poster = Poster(post_urls, self_id=self_id, token=token, secret=secret)
        self.ws_hub = WsHub(self.dispatch)
        # 协议适配器：核心事件 -> 各协议线格式。OneBot v11 常驻（emit 就是它的出口）
        self.protocols = core.ProtocolHub()
        self.protocols.register(OneBotAdapter(self))
        self.verify_identity = verify_identity
        # 运行时切换的开关持久化在 db 旁——serve 被看门狗拉起后 WebUI
        # 上一次的开关状态仍在；文件不存在时跟随启动参数 --test-trigger。
        self._trigger_pref_path = (
            None if db_path == ":memory:" else db_path + ".test_trigger"
        )
        self.test_trigger = self._init_test_trigger(test_trigger)
        # resolve_first：事件等成员解析完再发帖（user_id 落帖即真号）——
        # 最大兼容性原则：下游只会裸读 event.user_id，不为桥改代码，
        # 所以真号必须在源头就位；合成号只是解析不动时的兜底。
        self._resolve_pref_path = (
            None if db_path == ":memory:" else db_path + ".resolve_first"
        )
        self.resolve_first = self._init_resolve_first()
        self.started_at = time.time()
        # RLock：通知处理持锁期间，#ww 触发器的 api_send 会再次进锁
        self.gui_lock = TrackedRLock()
        # 出站限速 + 重复内容拦截（风控）
        self.send_guard = SendGuard()
        self.last_kick = None   # 最近一次下线通知 {title, text, time}，重新登录后清掉
        self._minimize_at = 0.0      # monotonic deadline for the deferred hide
        self._minimize_timer = None
        self._defocus_timer = None
        # 0 = 永不自动收窗。收窗后唤回不可靠（withdraw 态 windowmap 只拿回
        # 不重绘的空壳），QQ 常驻前台对 GUI 自动化最稳；通知有轮询兜底。
        self._minimize_delay = 0.0
        self._login_probe = None     # (monotonic, payload) 登录态 TTL 缓存
        self.attempted = {}          # summary -> monotonic, unresolved-uin cooldown
        self.member_attempted = {}   # "group:name" -> monotonic, member-card cooldown
        self.open_attempted = {}     # summary -> monotonic, open-failure cooldown
        self._notify_last = {}       # summary -> monotonic, 上次真正处理通知的时刻
        self._notify_pending = set() # summary -> 已排好尾随处理的会话
        self._notify_lock = threading.Lock()
        self._notify_since = 0.0     # 最近一次通知到达时刻——poll 据此让路
        self._notify_text = {}       # summary -> 最近一条通知正文（系统通知透传用）
        # notify.Service.invoke_live：持锁后对活着的通知发 ActionInvoked
        self.invoke_notification = None
        # 未读巡检：持焦/重启/重新登录这些盲区里 QQ 不发通知，事后读会话列表补
        self.sweeper = sweep.Sweeper()
        self.notified_names = None      # () -> set，__main__ 接到通知库上
        self._sweep_pending = True      # 启动后第一次失焦时巡检一次
        self._sweep_reason = "启动/重新登录"
        self._sweep_full = False        # 下次巡检不按上次巡检时刻截断
        self._recover_tries = {}        # summary -> 放弃后已安排的补处理次数
        self._last_sweep = None         # 上次巡检的时刻（epoch），限定往下读多深
        self._card_verified = None   # (uin, monotonic)：最近一次资料卡验号
        self._last_view = None       # (name, uin)：最近一次身份确认通过的会话
        self._msg_uins = {}          # msg_id -> real uin，歧义名按消息归因
        # member_ttl：名↔号绑定的缓存窗口——窗口内已验成员不再重复跑 GUI
        # 解析；过期绑定进 pending 重验（成员变动/同名交接只靠它兜底检测）。
        # 持久化文件 > QQCACHE_MEMBER_TTL > 默认 3600s；<=0 关闭（永久信任）。
        self._member_ttl_pref_path = (
            None if db_path == ":memory:" else db_path + ".member_ttl"
        )
        self.member_bind_ttl = self._init_member_ttl()
        self.kinds = {}              # summary -> is_group hint from the notification
        self.nt_data = find_nt_data()
        self.logbuf = log_buffer or LogBuffer()
        self.transports = TransportManager(
            self.dispatch, self.ws_hub, self.poster,
            media_root=self.nt_data, routes=self.http_routes(),
            access_token=token, log_buffer=self.logbuf, self_id=self.self_id,
            specs_path=None if db_path == ":memory:" else db_path + ".transports.json",
        )
        from .protocols import ProtocolManager

        self.protocol_mgr = ProtocolManager(
            self, path=None if db_path == ":memory:" else db_path + ".protocols.json")

    def publish(self, event):
        """Hand a core event to every protocol adapter (see core.ProtocolHub)."""
        self.protocols.publish(event)

    def emit(self, event):
        """Fan an event out to every transport: POST urls and ws clients."""
        self.poster.post(event)
        self.ws_hub.broadcast(event)

    # -- plumbing -----------------------------------------------------------

    def _app(self):
        from .__main__ import find_qq_pid

        pid = find_qq_pid()
        return atspi.find_app(pid) if pid else None

    def _minimize(self):
        wid = _main_window_id()
        if wid is not None:
            try:
                subprocess.run(["xdotool", "windowminimize", wid],
                               capture_output=True, timeout=2)
            except (subprocess.SubprocessError, OSError):
                pass

    def _schedule_minimize(self):
        """延迟收窗：GUI 弹起后留窗口期供后续操作/菜单交互，到期无活动才收。

        立即 minimize 曾让所有 GUI 自动化（右键菜单/资料卡/子菜单）在
        unmapped 窗口上落空。20s 内连续操作会刷新截止时间；到期时若仍有
        GUI 操作持锁则顺延。
        """
        if self._minimize_delay <= 0:
            return
        self._minimize_at = time.monotonic() + self._minimize_delay
        if self._minimize_timer is None or not self._minimize_timer.is_alive():
            self._minimize_timer = threading.Thread(
                target=self._minimize_loop, daemon=True
            )
            self._minimize_timer.start()

    def _schedule_defocus(self, delay=0.8):
        """操作收尾后点空桌面还回焦点——QQ 持焦时对其它会话的通知静默，
        失焦后通知通道恢复（窗口保持映射，不收窗）。"""
        if self._defocus_timer is not None:
            self._defocus_timer.cancel()
        self._defocus_timer = threading.Timer(delay, self._defocus_when_idle)
        self._defocus_timer.daemon = True
        self._defocus_timer.start()

    def _defocus_when_idle(self):
        """计时器线程上的失焦：GUI 操作还在跑就顺延——失焦插进点头像/复制
        号码中间会让弹层失焦关闭、按键落到别的窗口。"""
        if not self.gui_lock.acquire(blocking=False):
            self._schedule_defocus(delay=1.0)
            return
        try:
            took_focus = self._defocus()
            # QQ 刚才持焦 = 刚过完一段通知盲区；启动/重新登录也是盲区
            if took_focus or self._sweep_pending:
                reason = "持焦后" if took_focus else self._sweep_reason
                full, pending = self._sweep_full, self._sweep_pending
                self._sweep_pending = self._sweep_full = False
                if not self._sweep_unread(reason, full=full) and pending:
                    # 读不到会话列表（界面正在重建）：稍后再巡检
                    self._sweep_pending, self._sweep_full = True, full
                    self._schedule_defocus(delay=30.0)
        finally:
            self.gui_lock.release()

    def _sweep_unread(self, reason, full=False):
        """调用方持 GUI 锁。只读会话列表，漏掉的会话交给 on_notification
        走正常通知流程（去抖、验号都在那边）——另起线程，等这把锁释放。
        返回是否读到了会话列表。"""
        if self.notified_names is None:
            return True
        app = self._app()
        if app is None:
            return False
        try:
            names = self.notified_names()
            current = current_conversation(app)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"未读巡检跳过: {exc}")
            return False
        # 上次巡检之前就有的行不用再看（留 1 分钟余量：行内时间只精确到分钟）。
        # 补处理失败的会话可能早于上次巡检，此时不截断
        since = self._last_sweep - 60 if self._last_sweep and not full else None
        rows = sweep.sweep(app, self.sweeper, names, current=current, since=since)
        if rows is None:
            return False
        self._last_sweep = time.time()
        for row in rows:
            logger.info(f"未读巡检（{reason}）: {row.name!r} 有 {row.unread} 条未读"
                        f"但没收到通知，补处理")
            threading.Thread(target=self.on_notification, args=(row.name,),
                             daemon=True).start()
        return True

    def request_sweep(self, reason="启动/重新登录", full=False):
        """下一次失焦时顺带巡检（重新登录后：离线期间的消息同步进来不弹通知）。"""
        self._sweep_pending = True
        self._sweep_reason = reason
        self._sweep_full = self._sweep_full or full
        self._schedule_defocus()

    # 放弃一条通知后，隔多久用未读巡检再补处理；首次须长于打开冷却（60s）
    RECOVER_DELAYS = (65.0, 180.0, 600.0)

    def _give_up(self, summary):
        """通知处理到此放弃。消息在 QQ 里仍是未读，但 QQ 不会为它再发通知；
        渲染进程崩溃重建这类故障通常持续一两分钟，所以稍后用未读巡检再补，
        有限次数后才等下一条新消息。"""
        self.sweeper.forget(summary)
        n = self._recover_tries.get(summary, 0)
        if n >= len(self.RECOVER_DELAYS):
            self._recover_tries.pop(summary, None)
            logger.warning(f"通知 {summary!r}: 补处理 {n} 次仍失败，等待新消息")
            return
        self._recover_tries[summary] = n + 1
        delay = self.RECOVER_DELAYS[n]
        logger.info(f"通知 {summary!r}: {delay:.0f}s 后巡检未读再补处理")
        timer = threading.Timer(delay, self.request_sweep, args=("处理失败后", True))
        timer.daemon = True
        timer.start()

    def _defocus(self):
        try:
            out = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=3,
            )
            if "QQ" not in (out.stdout or ""):
                return False
            # 纯焦点切换，不移动鼠标、不产生指针事件。
            # XSetInputFocus(None) 单独用会被 openbox 的焦点回退抢回 QQ
            # （实测），所以激活一个角落里的 1x1 焦点接收窗口——WM 认它为
            # 活动窗口，QQ 拿到 FocusOut。接收窗口起不来才退回 X 直设。
            sink = self._focus_sink()
            if sink:
                subprocess.run(["xdotool", "windowactivate", sink],
                               capture_output=True, timeout=3)
            else:
                self._x_focus_pointer_root()
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    FOCUS_SINK = "qqcache-focus-sink"

    def _focus_sink(self):
        """焦点接收窗口的 X id；不存在就拉起一个（xmessage，x11-utils 自带）。"""
        def find():
            out = subprocess.run(
                ["xdotool", "search", "--name", f"^{self.FOCUS_SINK}$"],
                capture_output=True, text=True, timeout=2,
            )
            ids = (out.stdout or "").split()
            return ids[0] if ids else None

        try:
            wid = find()
            if wid:
                return wid
            subprocess.Popen(
                ["xmessage", "-name", self.FOCUS_SINK, "-title", self.FOCUS_SINK,
                 "-geometry", "1x1-0-0", "."],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(20):
                time.sleep(0.1)
                wid = find()
                if wid:
                    return wid
        except (subprocess.SubprocessError, OSError):
            pass
        return None

    @staticmethod
    def _x_focus_pointer_root():
        """把键盘焦点交给 None：没有任何窗口持焦，QQ 收到 FocusOut。

        不用 PointerRoot——指针停在 QQ 窗口上时焦点会跟着指针落回 QQ。
        旧实现漏导入 c_int（NameError 被吞）且不 XFlush，请求从未发出。
        """
        try:
            import ctypes
            from ctypes import c_char_p, c_int, c_ulong, c_void_p

            lib = ctypes.CDLL("libX11.so.6")
            lib.XOpenDisplay.restype = c_void_p
            lib.XOpenDisplay.argtypes = [c_char_p]
            lib.XSetInputFocus.argtypes = [c_void_p, c_ulong, c_int, c_ulong]
            lib.XFlush.argtypes = [c_void_p]
            lib.XCloseDisplay.argtypes = [c_void_p]
            dpy = lib.XOpenDisplay(os.environ.get("DISPLAY", ":99").encode())
            if not dpy:
                return False
            try:
                # focus=None(0), revert_to=RevertToNone(0), time=CurrentTime(0)
                lib.XSetInputFocus(dpy, 0, 0, 0)
                lib.XFlush(dpy)
            finally:
                lib.XCloseDisplay(dpy)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"defocus 失败: {exc}")
            return False

    def _minimize_loop(self):
        while True:
            wait = self._minimize_at - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            if not self.gui_lock.acquire(blocking=False):
                # GUI 操作还在跑——顺延，不抢窗
                self._minimize_at = time.monotonic() + self._minimize_delay
                continue
            try:
                self._minimize()
            finally:
                self.gui_lock.release()
            return

    # -- inbound: notification -> open -> poll -> emit ----------------------

    def on_event(self, event):
        """Service.on_event hook: stash the group hint and raw text for the worker."""
        self.kinds[event.summary] = event.is_group
        self._notify_text[event.summary] = event.body

    NOTIFY_DEBOUNCE = 15.0

    def on_notification(self, summary, attempts=0, trailing=False):
        """Service.attributor hook — runs on the notification worker thread.

        Reached after ActionInvoked has fired, so QQ should already be opening
        the conversation behind the notification. 拿不到锁说明 admin/send 在跑
        ——延迟重试让位，不硬堵在锁上抢（消息在 db 里晚两秒读无妨）。
        """
        self._notify_since = time.monotonic()
        if attempts == 0 and not trailing and not self._notify_admit(summary):
            return
        if not self.gui_lock.acquire(timeout=10):
            if attempts >= 5:
                logger.warning(f"通知 {summary!r} 等锁 {attempts} 次仍超时，放弃")
                self._give_up(summary)
                return
            logger.debug(f"通知 {summary!r} 等锁超时，延迟重试 ({attempts+1})")
            threading.Timer(2.0, self.on_notification, args=(summary, attempts + 1)).start()
            return
        logger.info(f"处理通知 {summary!r} (attempt {attempts})")
        try:
            self._handle_notification(summary, attempts)
        except Exception as exc:
            logger.warning(f"通知处理失败 {summary!r}: {exc}")
        finally:
            try:
                self._schedule_defocus()
            finally:
                self.gui_lock.release()

    def _notify_admit(self, summary):
        """同一会话 NOTIFY_DEBOUNCE 秒内只真正处理一次。窗口内到达的通知合并进
        窗口末尾的一次尾随处理（不丢），不让渲染器为同一个群反复切会话。"""
        with self._notify_lock:
            now = time.monotonic()
            if summary in self._notify_pending:
                return False
            wait = self.NOTIFY_DEBOUNCE - (now - self._notify_last.get(summary, -1e9))
            if wait > 0:
                self._notify_pending.add(summary)
                threading.Timer(wait, self._notify_trailing, args=(summary,)).start()
                return False
            self._notify_last[summary] = now
            return True

    def _notify_trailing(self, summary):
        with self._notify_lock:
            self._notify_pending.discard(summary)
            self._notify_last[summary] = time.monotonic()
        self.on_notification(summary, trailing=True)

    def _handle_notification(self, summary, attempts=0):
        app = self._app()
        if app is None:
            # AT-SPI 还没起来（崩溃恢复回放在启动早期会撞上）——重试一次
            if attempts < 1:
                logger.info(f"通知 {summary!r}: AT-SPI 未就绪，3s 后重试")
                threading.Timer(3.0, self.on_notification,
                                args=(summary, attempts + 1)).start()
            else:
                self._give_up(summary)
            return

        t0 = time.monotonic()
        # 通知还活着就一律 ActionInvoked——QQ 按内部会话 id 路由，同名的
        # 另一个群也能切对；只比名字的「已经在目标会话」会把同名群当成一个。
        # 持锁后才切：ActionInvoked 插进别的发送操作中间会发错会话。
        invoked = bool(self.invoke_notification and self.invoke_notification(summary))
        if invoked:
            name = self._wait_conversation(app, summary, timeout=4.0)
            if name is None and on_message_tab(app) is False:
                # 系统通知（加群申请、好友申请、退群…）：QQ 打开的是联系人/
                # 群通知页而不是聊天——不按标题猜，看打开了什么就知道。
                # 切回消息页，这条通知到此为止（标题不是会话名，别去按名打开）
                ensure_message_tab(app)
                logger.info(f"通知 {summary!r}: 不是会话通知（打开的不是聊天页），已切回消息页")
                self.publish(core.SystemNotification(
                    title=summary, text=self._notify_text.get(summary, "")))
                return
            if name is None:
                logger.info(f"通知 {summary!r}: ActionInvoked 未切会话，走主动打开")
        else:
            name = current_conversation(app)
            if not (name and _names_match(name, summary)):
                name = None
                logger.info(f"通知 {summary!r}: 通知已被 QQ 撤回，走主动打开")
        if name is None:
            # 打不开的会话挂冷却，消息攒未读不丢。
            if time.monotonic() - self.open_attempted.get(summary, -61) < 60:
                logger.info(f"通知 {summary!r}: 打开冷却中，跳过主动打开")
                self._give_up(summary)
                return
            name = self._open_notified(app, summary)
            if name is not None:
                self.open_attempted.pop(summary, None)
            if name is None:
                if attempts < 1:
                    logger.info(f"通知 {summary!r}: 3s 后重试一次")
                    threading.Timer(3.0, self.on_notification,
                                    args=(summary, attempts + 1)).start()
                else:
                    self._give_up(summary)
                return
            if not _names_match(name, summary):
                logger.warning(
                    f"通知 {summary!r}: 打开后读到的是 {name!r}，名字不符放弃"
                )
                self._give_up(summary)
                return

        t_open = time.monotonic() - t0
        uin, kind = self._resolve_uin(app, name, summary)
        if uin is None:
            logger.warning(f"通知 {summary!r}: uin 归因失败")
            self._give_up(summary)
            return
        self._recover_tries.pop(summary, None)
        posted = self.poll_emit(app, str(uin), kind, notified=True)
        logger.info(
            f"通知 {summary!r} -> {uin}: emit {posted} 条"
            f"（切会话 {t_open:.1f}s，总 {time.monotonic() - t0:.1f}s）")

    def _open_notified(self, app, summary):
        """ActionInvoked 失灵时的轻量兜底：只点侧栏可见项（不滚动、不搜索
        ——那是渲染器崩溃高发区）。可见项没有就直接放弃，消息攒未读等
        用户/后续切过去。"""
        try:
            if open_conversation(app, summary, timeout=4.0, scroll=False):
                self.open_attempted.pop(summary, None)
                return current_conversation(app)
        except SendError as exc:
            logger.info(f"通知 {summary!r}: 按名字打开失败: {exc}")
            # 「搜索结果里没有」是确定性失败（会话不在这个名字下）——
            # 重试必然同样结果，挂冷却；超时类瞬态失败不挂。
            if "没有" in str(exc):
                self.open_attempted[summary] = time.monotonic()
        uin = self.cache.get(summary)
        if uin is None:
            logger.warning(f"通知 {summary!r}: 缓存无 uin，无法再按号兜底")
            return None
        try:
            return send_open(app, uin, self.cache) or current_conversation(app)
        except SendError as exc:
            logger.warning(f"通知 {summary!r}: 按 {uin} 打开也失败: {exc}")
            return None

    def _wait_conversation(self, app, summary, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # 离开了消息页（系统通知把界面带到联系人页）就不用再等——聊天区
            # 节点隐藏后 header 名字还读得到，永远等不来目标名
            if on_message_tab(app) is False:
                return None
            name = current_conversation(app)
            if name and _names_match(name, summary):
                return name
            time.sleep(0.15)
        return None

    def _resolve_uin(self, app, name, summary):
        """(uin, kind) for the open conversation; attributing via peer.uin_of
        only when the opened header really matches the notified summary."""
        uin = self.cache.get(name) or (_names_match(name, summary) and self.cache.get(summary))
        if not uin and self.cache.ambiguous(name) and self._last_view and self._last_view[0] == name:
            # 同名多号：沿用上次确认过的号做候选，poll_emit 的 _identity_ok
            # 用 msg_id 零成本复核（切到同名另一个会话时它会验号纠正）
            uin = self._last_view[1]
        row = self.cache.lookup(uin) if uin else None
        if row:
            return uin, row["kind"]
        if not _names_match(name, summary):
            return uin, "unknown"
        if time.monotonic() - self.attempted.get(summary, -61) < 60:
            return None, None
        try:
            ident = peer.uin_of(app)
        except peer.PeerError:
            ident = None
        if ident is None or not ident.uin:
            # 冷却只罚失败：同名会话（缓存一名多号）每次都要验号，成功不能挂冷却
            self.attempted[summary] = time.monotonic()
            return None, None
        self.attempted.pop(summary, None)
        # 归因竞态：uin_of 期间会话可能已切走——它验证的是「卡↔按下时
        # 的 header」，不保证「卡↔入口读到的 name」。重读当前名，对不上
        # 就弃用，不然会把 A 会话的号记到 B 会话名下（files↔NoneBot
        # 互串就是这么来的）。
        cur = current_conversation(app)
        if not (cur and _names_match(cur, name)):
            logger.warning(
                f"归因竞态丢弃: {name!r} 在归因期间切为 {cur!r}，弃用 {ident.uin}")
            return None, None
        self.cache.record(name, ident.uin, kind=ident.kind)
        if name != summary:
            self.cache.record(summary, ident.uin, kind=ident.kind)
        self._card_verified = (str(ident.uin), time.monotonic())
        logger.success(f"归因: {summary!r} -> {ident.uin} ({ident.kind})")
        return ident.uin, ident.kind

    def _read_tree(self, ml, timeout=3.0):
        """dump_tree 是同步递归 AT-SPI 调用——QQ 主进程忙（消息渲染、图片
        加载）时 ATK 响应慢，一次 dump 能卡几十秒。放线程里限时：超时放弃
        本轮，下个 tick 再读。超时阈值要短（3s）：poll 持 gui_lock 太久会
        把通知 worker 饿死（等锁超时 5 次放弃 → 消息通知全丢）。"""
        box = {}

        def work():
            try:
                box["msgs"] = parse_messages(atspi.dump_tree(ml))
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.warning("消息树读取超时，本轮放弃（ATK 响应慢）")
            return []
        if "err" in box:
            raise box["err"]
        return box.get("msgs") or []

    def poll_emit(self, app, uin, kind, notified=False):
        # 读树前先确认「当前打开的就是目标会话」——header 名字先变、
        # 消息树渲染滞后，此时读会拿到旧会话的树并归错 conv（串消息根因）。
        expected = self.cache.lookup(uin)
        expected_name = expected["name"] if expected else None
        if expected_name:
            ok = False
            for _ in range(4):
                if _names_match(current_conversation(app) or "", expected_name):
                    ok = True
                    break
                time.sleep(0.3)
            if not ok:
                logger.warning(
                    f"{uin} 当前会话({current_conversation(app)!r})与目标({expected_name!r})不符，放弃本轮"
                )
                return 0
            # header 先变、消息树渲染滞后——确认后再等一拍，否则可能读到旧会话树
            time.sleep(0.4)
        # 会话刚被 Invoke/搜索打开时 header 先到、虚拟列表还没物化——
        # 静默读空是这个路径上最常见的丢消息点，给几次短重试。
        # 注意：读不到就快速放弃，下个 poll tick（2s）自然补上——不依赖
        # 通知路径空等树渲染（QQ 通知本身延迟不可控，poll 才是主通道）。
        messages = []
        for _ in range(3):
            ml = backfill.message_list(app)
            if ml is not None:
                messages = self._read_tree(ml)
            if messages:
                break
            time.sleep(0.5)
        if not messages:
            logger.warning(f"{uin} 消息列表不可用或为空，本次轮询丢弃")
            return 0
        if expected_name:
            # header 与消息树的切换两个方向都可能滞后——等一拍再采第二帧，
            # msg_id 序列不同说明树还在重渲染，放弃本轮（下 tick 自然读对）。
            time.sleep(0.4)
            ml = backfill.message_list(app)
            probe = self._read_tree(ml) if ml is not None else []
            if [m.msg_id for m in probe if m.kind != "system"] != [
                m.msg_id for m in messages if m.kind != "system"
            ]:
                logger.info(f"{uin} 消息树仍在切换中，本轮不归因")
                return 0
            messages = probe
            if kind == "c2c":
                # 私聊树的 incoming 发送者只能是对面那个人；冒出第三个名字
                # 说明 header 匹配了但树还属于别的会话（反向漂移）
                for m in messages:
                    if (
                        m.kind != "system" and not m.is_self and m.sender
                        and m.sender != self.self_name
                        and not _names_match(m.sender, expected_name)
                    ):
                        logger.warning(
                            f"{uin} 消息树混入外部发送者 {m.sender!r}，本轮不归因"
                        )
                        return 0
        if not self._identity_ok(app, uin, messages):
            return 0
        anchor = self.cursor.seen(uin)
        result = self.cursor.filter_new(uin, messages)
        self.cursor.commit(uin, messages)
        emit = _select_emit(result)

        if result.gap:
            # 先交付尾部新消息——#ww 之类的触发不被回填拖延；
            # 再向上缝合历史。代价：回补事件时序落在新消息之后。
            posted = self._deliver_emit(app, uin, kind, emit)
            emitted_ids = {m.msg_id for m in emit}
            if activate_qq():
                time.sleep(0.3)
            recovered = backfill.backfill(
                app, self.cursor, uin, anchor=anchor, budget=30.0
            )
            tail = backfill.scroll_to_bottom(app, budget=30.0)
            tail_result = self.cursor.filter_new(uin, tail)
            self.cursor.commit(uin, tail)
            late = [
                m for m in recovered["recovered"] + tail_result.new
                if m.msg_id not in emitted_ids
            ]
            posted += self._deliver_emit(
                app, uin, kind, late, resolve_members=False, backlog=True
            )
            if not recovered["stitched"]:
                logger.warning(
                    f"{uin} 回填 {recovered['steps']} 步未缝合"
                    + ("（帧间跳窗）" if recovered["chain_broken"] else "")
                )
            return posted

        if not emit:
            logger.info(
                f"{uin} 无新消息可发"
                + (f"（首次读取 {len(result.new)} 条记为已见）" if result.first_run else "")
            )
            return 0
        return self._deliver_emit(app, uin, kind, emit)

    def _identity_ok(self, app, uin, messages):
        """名字→号缓存只认名字：同名的两个群会被当成一个，B 群的消息记到
        A 群号下、下游回复也发进 A 群。读到的树必须证明自己属于 uin：

        - 树里有 uin 名下见过的 msg_id（全局唯一）——必然是它，零成本；
        - 刚刚资料卡验过这个号（首次归因）——不重复验；
        - 都不满足（新会话/积压超过一屏）才验号一次（~1s）。
        验出别的号 = 同名会话：本轮不发（cursor 未提交，消息不丢），把名字
        也记到新号下——缓存一名多号后 get() 返回 None，此后每次都按号归因。
        """
        uin = str(uin)
        ids = [m.msg_id for m in messages if m.kind != "system" and m.msg_id]
        name = current_conversation(app)
        if ids and self.cursor.seen(uin, ids):
            self._last_view = (name, uin)
            return True
        cv = self._card_verified
        if cv and cv[0] == uin and time.monotonic() - cv[1] < 15.0:
            self._last_view = (name, uin)
            return True
        try:
            ident = peer.uin_of(app)
        except peer.PeerError as exc:
            logger.warning(f"{uin} 会话身份无法确认（{exc}），本轮不发")
            return False
        if ident.uin and str(ident.uin) == uin:
            self._card_verified = (uin, time.monotonic())
            self._last_view = (name, uin)
            return True
        cur = current_conversation(app)
        if ident.uin and cur:
            logger.warning(
                f"同名会话: {cur!r} 实为 {ident.uin}，不是缓存的 {uin}——本轮不发，改按号归因")
            self.cache.record(cur, str(ident.uin), kind=ident.kind)
            self._card_verified = (str(ident.uin), time.monotonic())
            self._last_view = (cur, str(ident.uin))
        else:
            logger.warning(f"{uin} 会话身份验号未得到号码，本轮不发")
        return False

    def _deliver_emit(self, app, uin, kind, emit, resolve_members=True, backlog=False):
        """One batch's downstream delivery: media correlation, ob11 record,
        event post, #ww trigger, member-name resolution.

        事件先走、成员解析殿后：未解成员带稳定合成 user_id 发帖，#ww 之类
        的触发立刻响应；解析（头像卡 ~3-6s / 成员列表 ~10-30s）在发完之后
        才跑，成功由 qqcache_member_resolved 通知把合成号升级成真号。
        反过来排队（先解后发）的代价是整批被解析拖住——成员一多，解析
        失败也照发合成，白等 30s。

        self.resolve_first（set_resolve_first 开关，默认开）：最大兼容性
        ——下游只裸读 event.user_id 不为桥改代码，所以真号必须在源头就位。
        #ww 回复仍然先行（回复不需要真号），解析完之后才发帖；合成号只是
        解析不动时的兜底。关掉则退回事件先走、解析殿后的低延迟模式。

        resolve_members=False（回填批）本批不解，下批按冷却再试。
        """
        if not emit:
            return 0
        self._msg_uins = {}
        pending = {}
        if resolve_members and kind == "group":
            for m in emit:
                if m.kind == "system" or m.is_self or not m.sender:
                    continue
                su = self._sender_uin(m, uin, kind)
                if str(su).isdigit() and int(su) >= SYN_BASE:
                    pending.setdefault(m.sender, []).append(m.msg_id)
        triggers_done = False
        if pending and self.resolve_first:
            self._nw_triggers(emit, uin)
            t_res = time.monotonic()
            try:
                # 解析炸了就发合成——消息丢了比号不对更糟
                self._resolve_members(app, str(uin), pending)
            except Exception as exc:
                logger.warning(f"成员解析异常 {uin}，本批按合成号发帖: {exc}")
            logger.info(f"成员解析 {uin}: {len(pending)} 人 {time.monotonic() - t_res:.1f}s")
            # 点头像会让 QQ 拿焦点（= 别的会话通知静默）。通知路径收尾本来就
            # 还焦点，poll 路径不会——这里补上；失焦时顺带巡检这段盲区
            self._schedule_defocus()
            pending = {}
            triggers_done = True
        links = {}
        if self.nt_data:
            with MediaIndex(self.nt_data, self.db_path) as media_index:
                fresh = media_index.scan()
                if not media_index.last_scan_was_initial:
                    links, _ = correlate(emit, fresh)

        posted = 0
        for msg in emit:
            if msg.kind == "system":
                continue
            reply_id = (
                self.ids.guess_reply_id(uin, msg.quoted_sender, msg.quoted_content)
                if msg.kind == "reply"
                else None
            )
            media = links.get(msg.msg_id)
            cq = message_to_cq(msg, reply_ob_id=reply_id,
                               media_path=media.path if media else None)
            if not cq:
                continue
            direction = "out" if msg.is_self else "in"
            sender_uin = self._sender_uin(msg, uin, kind)
            role = "owner" if msg.title and "群主" in msg.title else (
                "admin" if msg.title and "管理员" in msg.title else "member")
            ob_id = self.ids.record_message(
                uin, kind, msg.msg_id, direction, sender_uin, msg.sender, role,
                cq, msg.content,
            )
            reply_row = self.ids.get(reply_id) if reply_id is not None else None
            conv_row = self.cache.lookup(str(uin))
            self.publish(core.MessageCreated(
                conv_uin=str(uin), kind=kind, msg=msg, sender_uin=str(sender_uin),
                role=role, ob_id=ob_id, cq=cq,
                conv_name=(conv_row or {}).get("name") or "",
                media_path=media.path if media else None,
                reply_ob_id=reply_id,
                reply_msg_id=(reply_row or {}).get("msg_id"),
                backlog=backlog,
            ))
            posted += 1
        if not triggers_done:
            self._nw_triggers(emit, uin)
        if pending:
            try:
                self._resolve_members(app, str(uin), pending)
            except Exception as exc:
                logger.warning(f"成员解析异常 {uin}: {exc}")
        return posted

    MEMBER_RESOLVE_COOLDOWN = 120.0

    MEMBER_BUDGET = 15.0     # 一批成员解析的持锁总预算（秒）
    MEMBER_LIST_MIN = 10.0   # 私聊路径（含切回群）至少要剩这么多预算才上

    def _resolve_members(self, app, group_uin, pending, budget=None):
        """Best-effort upgrade of synthetic member ids to real uins.

        One avatar->profile-card round trip per unresolved sender — the
        message is still materialised since it just arrived. Success lands in
        ob11_members.real_uin: future events carry the real uin and member
        lists expose it, so only already-emitted events keep the synthetic.

        budget 是整个批次的持锁总预算：大群一批未解析成员不能无限期占着
        gui_lock（通知 worker 串行、别的会话事件全在排队、而且持焦期间别
        的会话通知被静默）。预算耗尽就停，剩余成员下批按冷却再试。
        """
        # 私聊路径最慢（进私聊复制号码、再切回群，置顶会话多时切回要滚动
        # 很久），所以每批最多走一次，且剩余预算够才走
        deadline = time.monotonic() + (budget if budget is not None else self.MEMBER_BUDGET)
        now = time.monotonic()
        list_used = False
        _main_window_id()  # 头像卡路径走坐标点击——主窗若被收进托盘先唤回
        done = set()
        # 先白捡：上一批点击开的卡可能晚到还开着，自带群昵称字段直接归因。
        # 同名多消息的名字不提前收工——卡只认得出名，分不出是哪条消息的。
        for name, real_uin in peer.harvest_member_cards(app, pending).items():
            self._apply_member_uin(group_uin, name, real_uin)
            if len(pending.get(name) or ()) <= 1:
                done.add(name)
        for name, msg_ids in pending.items():
            if name in done:
                continue
            if time.monotonic() > deadline:
                logger.info(f"成员解析预算耗尽 {group_uin}，剩余成员下批再试")
                break
            key = f"{group_uin}:{name}"
            ambiguous = self.ids.member_is_ambiguous(group_uin, name)
            if not ambiguous and now - self.member_attempted.get(key, -1e9) < self.MEMBER_RESOLVE_COOLDOWN:
                continue
            self.member_attempted[key] = now
            # 歧义名或同批同名多消息：每条消息点自己的头像——唯一能分人的锚。
            # 单条单名：一次头像往返就够。
            targets = msg_ids if (ambiguous or len(msg_ids) > 1) else msg_ids[:1]
            resolved = False
            for mid in targets:
                if time.monotonic() > deadline:
                    break
                node = backfill.find_item_node(app, mid)
                if node is None:
                    continue
                try:
                    u = peer.member_uin(app, node)
                except peer.PeerError as exc:
                    logger.info(f"头像卡解析失败 {group_uin}/{name!r}: {exc}")
                    continue
                self._msg_uins[mid] = u
                self._apply_member_uin(group_uin, name, u)
                resolved = True
            if resolved:
                self._check_member_ambiguous(app, group_uin, name)
                done.add(name)
                continue
            if ambiguous:
                continue  # 名字分不出人，按名兜底只会错归
            real_uin = None
            # 晚到的卡可能这会儿才填充完（可能是别人的卡，一并捡）
            found = peer.harvest_member_cards(
                app, (n for n in pending if n not in done))
            real_uin = found.pop(name, None)
            for hn, hu in found.items():
                self._apply_member_uin(group_uin, hn, hu)
                done.add(hn)
            if real_uin is None:
                # 私聊路径 ~5-8s + 切回群——每批最多一次、剩余预算够才上，
                # 解不了就留给下批（头像那条路下批多半能成）
                if list_used or time.monotonic() > deadline - self.MEMBER_LIST_MIN:
                    continue
                list_used = True
                t_list = time.monotonic()
                try:
                    real_uin = peer.member_uin_via_list(app, name)
                except peer.PeerError as exc:
                    logger.info(f"成员列表解析失败 {group_uin}/{name!r}: {exc}")
                    if "无法唯一归因" in str(exc):
                        self.ids.mark_member_ambiguous(group_uin, name)
                finally:
                    # 私聊路径把会话切走了——切回群，别影响后续成员归因
                    t_back = time.monotonic()
                    try:
                        send_open(app, str(group_uin), self.cache)
                    except Exception:
                        pass
                    logger.info(f"成员私聊路径 {group_uin}/{name!r}: 解析 {t_back - t_list:.1f}s"
                                f" / 切回群 {time.monotonic() - t_back:.1f}s")
                if real_uin is None:
                    continue
            self._apply_member_uin(group_uin, name, real_uin)
            self._check_member_ambiguous(app, group_uin, name)
            done.add(name)

    def _check_member_ambiguous(self, app, group_uin, name):
        """晚到同名坑：刚解出的 uin 只是其中一个持有人——成员列表 count>1
        就打持久歧义标记，该名从此逐条消息走自己的头像归因，不再错归。
        """
        try:
            _row, count = peer.find_member_row(app, name)
        except peer.PeerError:
            return
        if count > 1:
            self.ids.mark_member_ambiguous(group_uin, name)
            logger.info(f"群成员歧义: {group_uin}/{name!r} 同名成员 {count} 个，逐条归因")

    def _apply_member_uin(self, group_uin, name, real_uin):
        """真号落地：更新映射、推升级通知、落日志。"""
        was = self.ids.member_user_id(group_uin, name)
        self.ids.member_user_id(group_uin, name, real_uin=real_uin)
        if str(was) != str(real_uin):
            # 缓存窗口的重验拿回同号不算升级，别骚扰下游
            self._member_resolved_notice(group_uin, name, was, real_uin)
        logger.info(f"群成员归因: {group_uin}/{name!r} -> {real_uin}")

    def _member_resolved_notice(self, group_uin, name, was_user_id, real_uin):
        """合成 id 升级为真 uin 时推的 notice——下游按它合并两份身份。"""
        self.publish(core.MemberResolved(
            group_uin=str(group_uin), name=name,
            user_id=str(real_uin), was_user_id=str(was_user_id),
        ))

    def _resolve_member_now(self, app, group_uin, uid):
        """成员列表路径的现场解析：打开群 → 右键成员行 → 查看资料 → 真 uin。

        调用方须持 gui_lock。uid 可为合成 id / 真 uin / 显示名。
        返回真 uin；失败抛 ApiError 或 peer.PeerError。
        """
        name = self.ids.member_name(group_uin, uid)
        if name is None and not str(uid).isdigit():
            name = str(uid)  # 允许直接传显示名
        if not name:
            raise ApiError(1404, f"成员 {uid} 无记录，无法按显示名定位")
        try:
            send_open(app, str(group_uin), self.cache)
        except SendError as exc:
            raise ApiError(1400, f"打开群会话失败: {exc}") from exc
        try:
            real = peer.member_uin_via_list(app, name)
        finally:
            # 私聊路径把会话切到成员 DM 了——切回群再交账
            try:
                send_open(app, str(group_uin), self.cache)
            except Exception:
                pass
        was = self.ids.member_user_id(str(group_uin), name)
        if str(was) != str(real):
            self.ids.member_user_id(str(group_uin), name, real_uin=real)
            self._member_resolved_notice(str(group_uin), name, was, real)
        return real

    def _nw_triggers(self, emit, conv_uin):
        """一批里只回最新的一条 #ww。

        视口里可能躺着以前没处理过的旧 #ww（上次 serve 挂了、切会话失败…），
        它们和新消息同批成为「新」——逐条回复就是连发事故。旧的只记为已回复。
        """
        if not self.test_trigger:
            return
        hits = [m for m in emit if m.kind != "system" and m.content.strip() == "#ww"]
        if not hits:
            return
        for old in hits[:-1]:
            if not self.ids.ww_replied(conv_uin, old.msg_id):
                logger.info(f"#ww 旧消息 {conv_uin}/{old.msg_id} 不回复（同批有更新的）")
                self.ids.mark_ww_replied(conv_uin, old.msg_id)
        self._nw_trigger(hits[-1], conv_uin)

    def _nw_trigger(self, msg, conv_uin):
        """#ww 测试开关：收到内容恰为 #ww 的消息就回一条服务状态。

        安全闸（发错会话事故后加）：
        - 同一会话同一消息只回复一次（ob11_ww_replied 表，跨重启生效），
          崩溃恢复回放/重 poll 不会把回复连发好几条；
        - 回复走 api_send → send()，内部对目标会话做资料卡验证，对不上
          就抛错放弃，绝不在错误会话里敲字。
        """
        if not self.test_trigger or msg.content.strip() != "#ww":
            return
        if self.ids.ww_replied(conv_uin, msg.msg_id):
            logger.info(f"#ww 已回复过 {conv_uin}/{msg.msg_id}，跳过")
            return
        try:
            # 身份验证失败（资料卡读取慢/ATK 卡）时重试——一次放弃会让
            # 回复静默丢失（cursor 已把消息记为 seen，不会重触发）。
            last_exc = None
            for attempt in range(3):
                try:
                    self.api_send(conv_uin, status_text(time.time() - self.started_at))
                    last_exc = None
                    break
                except RateLimited as exc:
                    # 限速/重复拦截：重试只会再被拦，直接放弃
                    last_exc = exc
                    break
                except ApiError as exc:
                    last_exc = exc
                    logger.warning(
                        f"#ww 状态回复失败（重试 {attempt + 1}/3）: {exc}"
                    )
                    time.sleep(2.0)
            if last_exc is not None:
                raise last_exc
        except ApiError as exc:
            logger.warning(f"#ww 状态回复最终失败: {exc}")
        else:
            self.ids.mark_ww_replied(conv_uin, msg.msg_id)

    def _sender_uin(self, msg, conv_uin, kind):
        if msg.is_self:
            return str(self.self_id)
        if kind == "group":
            u = self._msg_uins.get(msg.msg_id)
            if u:
                return str(u)
            return str(
                self.ids.member_user_id(
                    str(conv_uin), msg.sender or "?",
                    max_age=self.member_bind_ttl if self.member_bind_ttl > 0 else None,
                )
            )
        return str(conv_uin) if str(conv_uin).isdigit() else str(
            self.ids.conv_user_id(str(conv_uin), msg.sender)
        )

    # -- outbound: OneBot actions --------------------------------------------

    def api_send(self, uin, message, auto_escape=False):
        if auto_escape:
            segments = [("text", {"text": message if isinstance(message, str) else ""})]
        else:
            segments = normalize_segments(message)
        temp_paths = []

        def resolve(ref):
            path = resolve_file_ref(ref)
            if path != ref and (ref.startswith("base64://") or ref.startswith("http")):
                temp_paths.append(path)
            return path

        ops, reply_ob = ops_from_segments(segments, resolve)
        reply_to = None
        if reply_ob is not None:
            row = self.ids.get(reply_ob)
            if row is None or not row.get("msg_id"):
                raise ApiError(1400, f"reply 指向的 message_id={reply_ob} 不存在或不可定位")
            reply_to = row["msg_id"]

        try:
            ticket = self.send_guard.reserve(uin, message)
        except ApiError as exc:
            logger.warning(f"拦截发送 → {uin}: {exc}")
            for p in temp_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise
        try:
            # 存档用 CQ 串：段数组（automatic/Satori 传进来的）直接 str() 会存成 repr
            raw = message if isinstance(message, str) and not auto_escape else segments_to_cq(segments)
            return self._api_send_locked(uin, message, ops, reply_to, temp_paths, raw)
        except ApiError as exc:
            # 确定没发出去（没碰输入框/没验过号）才退还名额，让重试不被当成重复
            clean = side_effect_free(str(exc))
            if clean:
                self.send_guard.release(ticket)
            logger.warning(f"发送 {uin} 失败（{'未发出，可重试' if clean else '可能已部分执行'}）: {exc}")
            raise

    def _api_send_locked(self, uin, message, ops, reply_to, temp_paths, raw):
        with self.gui_lock:
            try:
                app = self._app()
                if app is None:
                    raise ApiError(1400, "QQ 不在无障碍树里")
                echo = send(
                    app,
                    uin=str(uin),
                    cache=self.cache,
                    segments=ops,
                    reply_to=reply_to,
                    verify=True,
                    # 强制验证：自动发送（OneBot send_msg / #ww 回复）永不跳过
                    # 身份验证——切错会话就失败，绝不在错误会话里敲字
                    verify_identity=True,
                )
            except SendError as exc:
                raise ApiError(1400, str(exc)) from exc
            finally:
                for p in temp_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                # 发送把窗口弹起聚焦了 -> 通知会被抑制，收尾后还回焦点
                self._schedule_defocus()
            if echo is not None:
                # 已回显的消息登记+commit，避免下一轮 poll 再发 message_sent
                self.cursor.commit(str(uin), [echo])
                row_name = current_conversation(app)
                if row_name:
                    self.cache.record(row_name, str(uin), kind="unknown")
                ob_id = self.ids.record_message(
                    str(uin), "unknown", echo.msg_id, "out", str(self.self_id),
                    self.self_name, None,
                    raw,
                    echo.content,
                )
            else:
                ob_id = self.ids.record_message(
                    str(uin), "unknown", None, "out", str(self.self_id),
                    self.self_name, None,
                    raw, "",
                )
            return {"message_id": ob_id}

    def _member_payload(self, row, group_uin):
        return {
            "group_id": int(group_uin),
            "user_id": int(row["real_uin"] or row["user_id"]),
            "nickname": row["name"],
            "card": "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "join_time": int(row["first_seen"]),
            "last_sent_time": int(row["last_seen"]),
            "level": "",
            "role": row["role"] or "member",
            "unfriendly": False,
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
        }

    # -- action table ---------------------------------------------------------

    def dispatch(self, action, params):
        handler = ACTIONS.get(action)
        if handler is None:
            raise ApiError(1404, f"未实现的 action: {action}")
        if action in ADMIN_ACTIONS and not self.access_token:
            raise ApiError(1403, "管理接口需要先配置 --access-token")
        return handler(self, params)

    # -- admin: logs + transport management ------------------------------------

    def act_get_logs(self, p):
        """Buffered service logs. Params: after (seq), tail, level (min)."""
        return {
            "logs": self.logbuf.snapshot(
                after=int(p.get("after") or 0),
                tail=min(int(p.get("tail") or 200), 1000),
                level=p.get("level"),
            ),
            "latest_seq": self.logbuf.latest_seq,
        }

    def _init_test_trigger(self, flag):
        if not self._trigger_pref_path:
            return flag
        try:
            with open(self._trigger_pref_path, encoding="utf-8") as fh:
                return fh.read().strip() == "1"
        except OSError:
            return flag

    def _init_resolve_first(self):
        if not self._resolve_pref_path:
            return True
        try:
            with open(self._resolve_pref_path, encoding="utf-8") as fh:
                return fh.read().strip() == "1"
        except OSError:
            return True

    def _init_member_ttl(self):
        if self._member_ttl_pref_path:
            try:
                with open(self._member_ttl_pref_path, encoding="utf-8") as fh:
                    return float(fh.read().strip())
            except (OSError, ValueError):
                pass
        try:
            return float(os.environ.get("QQCACHE_MEMBER_TTL", "3600"))
        except ValueError:
            return 3600.0

    def act_get_member_ttl(self, p):
        return {"ttl": self.member_bind_ttl}

    def act_set_member_ttl(self, p):
        if "ttl" not in p:
            raise ApiError(1400, "缺少 ttl")
        try:
            ttl = float(p.get("ttl"))
        except (TypeError, ValueError):
            raise ApiError(1400, "ttl 必须是数字（秒）")
        if not (0 <= ttl < float("inf")):
            raise ApiError(1400, "ttl 必须是不小于 0 的有限秒数（0 = 关闭缓存窗口）")
        self.member_bind_ttl = ttl
        if self._member_ttl_pref_path:
            try:
                with open(self._member_ttl_pref_path, "w", encoding="utf-8") as fh:
                    fh.write(str(ttl))
            except OSError as exc:
                logger.warning(f"member_ttl 持久化失败: {exc}")
        logger.warning(
            f"成员绑定缓存窗口: {ttl:g}s" if ttl > 0
            else "成员绑定缓存窗口: 关闭（永久信任绑定）"
        )
        return {"ttl": self.member_bind_ttl}

    def act_get_resolve_first(self, p):
        return {"enabled": bool(self.resolve_first)}

    def act_set_resolve_first(self, p):
        if "enabled" not in p:
            raise ApiError(1400, "缺少 enabled")
        self.resolve_first = _truthy(p.get("enabled"))
        if self._resolve_pref_path:
            try:
                with open(self._resolve_pref_path, "w", encoding="utf-8") as fh:
                    fh.write("1" if self.resolve_first else "0")
            except OSError as exc:
                logger.warning(f"resolve_first 状态持久化失败: {exc}")
        logger.warning(
            "成员解析顺序: 先解析后发帖（默认）" if self.resolve_first
            else "成员解析顺序: 先发帖后解析"
        )
        return {"enabled": bool(self.resolve_first)}

    def act_run_selftest(self, p):
        """分阶段链路自检：按真实入站链逐段验证，报告断点。"""
        from . import selftest

        conv = str(p.get("conv") or "").strip()
        if not conv:
            raise ApiError(1400, "缺少 conv（目标会话名）")
        timeout = float(p.get("open_timeout") or 8.0)
        return selftest.run(self, conv, open_timeout=timeout)

    def act_get_test_trigger(self, p):
        return {"enabled": bool(self.test_trigger)}

    def act_set_test_trigger(self, p):
        if "enabled" not in p:
            raise ApiError(1400, "缺少 enabled")
        self.test_trigger = _truthy(p.get("enabled"))
        if self._trigger_pref_path:
            try:
                with open(self._trigger_pref_path, "w", encoding="utf-8") as fh:
                    fh.write("1" if self.test_trigger else "0")
            except OSError as exc:
                logger.warning(f"测试开关状态持久化失败: {exc}")
        logger.warning(
            f"测试触发器已{'启用' if self.test_trigger else '禁用'}（API 切换）"
        )
        return {"enabled": bool(self.test_trigger)}

    def act_list_protocols(self, p):
        return {"protocols": self.protocol_mgr.list()}

    def act_set_protocol(self, p):
        """启停/改配置：{name, enabled?, host?, port?, token?, platform?, regenerate_token?}"""
        name = str(p.get("name") or "")
        if not name:
            raise ApiError(1400, "缺少 name")
        # HTTP 调用会把 URL 里的 access_token 也并进 params
        changes = {k: v for k, v in p.items() if k not in ("name", "access_token")}
        return {"protocols": self.protocol_mgr.update(name, changes)}

    def act_get_protocol_token(self, p):
        return {"token": self.protocol_mgr.token(str(p.get("name") or ""))}

    def act_list_transports(self, p):
        return {"transports": self.transports.list()}

    def act_add_transport(self, p):
        """Create a transport. Params: type http/ws_reverse/http_post + spec."""
        tid = self.transports.add_spec(dict(p))
        return {"id": tid, "transports": self.transports.list()}

    def act_del_transport(self, p):
        if not p.get("id"):
            raise ApiError(1400, "缺少 id")
        self.transports.remove(str(p["id"]))
        return {"transports": self.transports.list()}

    def act_send_msg(self, p):
        message = p.get("message")
        if p.get("group_id"):
            return self.api_send(p["group_id"], message, _truthy(p.get("auto_escape")))
        if p.get("user_id") or p.get("message_type") == "private":
            if not p.get("user_id"):
                raise ApiError(1400, "缺少 user_id/group_id")
            return self.api_send(p["user_id"], message, _truthy(p.get("auto_escape")))
        raise ApiError(1400, "缺少 user_id/group_id")

    def act_send_private_msg(self, p):
        if not p.get("user_id"):
            raise ApiError(1400, "缺少 user_id")
        return self.api_send(p["user_id"], p.get("message"), _truthy(p.get("auto_escape")))

    def act_send_group_msg(self, p):
        if not p.get("group_id"):
            raise ApiError(1400, "缺少 group_id")
        return self.api_send(p["group_id"], p.get("message"), _truthy(p.get("auto_escape")))

    def act_get_login_info(self, p):
        return {"user_id": self.self_id, "nickname": self.self_name}

    def act_get_friend_list(self, p):
        return [
            {"user_id": int(r["uin"]), "nickname": r["name"], "remark": ""}
            for r in self.cache.conversations()
            if r["kind"] == "c2c" and is_uin(r["uin"])
        ]

    def act_get_group_list(self, p):
        out = []
        for r in self.cache.conversations():
            if r["kind"] != "group" or not is_uin(r["uin"]):
                continue
            out.append({
                "group_id": int(r["uin"]),
                "group_name": re.split(r"[（(]", r["name_clean"] or r["name"])[0],
                "member_count": _member_count(r["name"]),
                "max_member_count": 0,
            })
        return out

    def act_get_group_info(self, p):
        gid = str(p.get("group_id") or "")
        row = self.cache.lookup(gid)
        if row is None:
            raise ApiError(1404, f"群 {gid} 不在缓存里")
        return {
            "group_id": int(gid),
            "group_name": re.split(r"[（(]", row["name_clean"] or row["name"])[0],
            "member_count": _member_count(row["name"]),
            "max_member_count": 0,
        }

    def act_get_group_member_list(self, p):
        gid = str(p.get("group_id") or "")
        return [self._member_payload(r, gid) for r in self.ids.members(gid)]

    def act_get_group_member_info(self, p):
        gid = str(p.get("group_id") or "")
        uid = p.get("user_id")
        row = self.ids.member_info(gid, uid)
        if row is None:
            raise ApiError(1404, "成员未出现过（只有发过消息的成员有记录）")
        if not row.get("real_uin") and self._login_state_cached() == "logged_in":
            # 点名要 user_id ——合成号现场解真号；解不动就退回合成 payload
            try:
                with self.gui_lock:
                    app = self._app()
                    if app is not None:
                        self._resolve_member_now(app, gid, uid)
                        row = self.ids.member_info(gid, uid) or row
            except (peer.PeerError, ApiError) as exc:
                logger.info(f"成员现场解析失败 {gid}/{uid}: {exc}")
        return self._member_payload(row, gid)

    def act_resolve_member(self, p):
        """Custom action: 按需把成员的合成 user_id 解析成真 uin。

        走成员列表右键「查看资料」→ 资料卡，不依赖该成员发过消息；
        ~3-6s 的 GUI 操作。Params: group_id, user_id（合成 id 或显示名）。
        """
        self._require_logged_in()
        gid = str(p.get("group_id") or "")
        uid = str(p.get("user_id") or "")
        if not gid or not uid:
            raise ApiError(1400, "缺少 group_id/user_id")
        row = self.ids.member_info(gid, uid)
        if row and row.get("real_uin"):
            return {
                "user_id": int(row["real_uin"]),
                "was_user_id": int(row["user_id"]),
                "nickname": row["name"],
            }
        with self.gui_lock:
            app = self._app()
            if app is None:
                raise ApiError(1400, "QQ 不在无障碍树里")
            try:
                real = self._resolve_member_now(app, gid, uid)
            except peer.PeerError as exc:
                raise ApiError(1400, str(exc)) from exc
        row = self.ids.member_info(gid, real) or row
        return {
            "user_id": int(real),
            "was_user_id": int(row["user_id"]) if row else None,
            "nickname": row["name"] if row else None,
        }

    def _row_to_message(self, row):
        is_group = row["kind"] == "group"
        out = {
            "time": int(row["post_time"]),
            "self_id": self.self_id,
            "post_type": "message_sent" if row["direction"] == "out" else "message",
            "message_type": "group" if is_group else "private",
            "sub_type": "normal" if is_group else "friend",
            "message_id": row["ob_id"],
            "user_id": int(row["sender_uin"]) if str(row["sender_uin"]).isdigit() else 0,
            "message": row["message"],
            "raw_message": row["raw"],
            "font": 0,
            "sender": {
                "user_id": int(row["sender_uin"]) if str(row["sender_uin"]).isdigit() else 0,
                "nickname": row["sender_name"] or "",
                "sex": "unknown",
                "age": 0,
            },
            "qqcache_msg_id": row["msg_id"],
            "qqcache_kind": row["kind"],
        }
        if is_group and str(row["conv_uin"]).isdigit():
            out["group_id"] = int(row["conv_uin"])
            out["sender"]["role"] = row["role"] or "member"
        return out

    def act_get_msg_history(self, p):
        """Custom action (NapCat-style): stored messages for a conversation.

        Only covers what this service emitted or sent since startup — not the
        full QQ history. Params: user_id or group_id, optional message_id
        (return rows before it, for pagination), count.
        """
        conv = str(p.get("group_id") or p.get("user_id") or "")
        if not conv:
            raise ApiError(1400, "缺少 user_id/group_id")
        rows = self.ids.history(
            conv,
            before_id=int(p.get("message_id") or 0),
            limit=min(int(p.get("count") or 20), 100),
        )
        return {"messages": [self._row_to_message(r) for r in rows]}

    def act_get_msg(self, p):
        row = self.ids.get(p.get("message_id"))
        if row is None:
            raise ApiError(1404, "message_id 不存在")
        is_group = row["kind"] == "group"
        return {
            "time": int(row["post_time"]),
            "message_type": "group" if is_group else "private",
            "message_id": row["ob_id"],
            "real_id": row["ob_id"],
            "sender": {
                "user_id": int(row["sender_uin"]) if str(row["sender_uin"]).isdigit() else 0,
                "nickname": row["sender_name"] or "",
                "sex": "unknown",
                "age": 0,
            },
            "group_id": int(row["conv_uin"]) if is_group and str(row["conv_uin"]).isdigit() else None,
            "message": row["message"],
            "raw_message": row["raw"],
        }

    def _login_state_cached(self, ttl=2.0, lock_timeout=5.0):
        """TTL 缓存的登录态——BFF 高频轮询不再每次都全树遍历。

        a11y 是一条串行 socket，裸并发遍历会互相排队压垮对端；
        miss 路径一律进 gui_lock 串行化，hit 路径无锁。
        """
        now = time.monotonic()
        probe = self._login_probe
        if probe and now - probe[0] < ttl:
            return probe[1]
        # 只读查询不无限等锁：GUI 忙（或卡死待看门狗处理）时返回最近已知态，
        # 而不是让 WebUI 的轮询请求一个个堆在锁上
        if not self.gui_lock.acquire(timeout=lock_timeout):
            return probe[1] if probe else "unknown"
        try:
            probe = self._login_probe
            if probe and now - probe[0] < ttl:
                return probe[1]
            state = login.login_state(self._app())
            self._login_probe = (now, state)
            return state
        finally:
            self.gui_lock.release()

    def act_get_system_info(self, p):
        """Dashboard payload: account, versions, host resources, stats."""
        from . import sysinfo
        return sysinfo.overview(self)

    def act_get_login_state(self, p):
        """Custom action: whether the client is showing login or main UI."""
        state = self._login_state_cached()
        qr_expired = False
        if state == "login_required" and self.gui_lock.acquire(timeout=5.0):
            try:
                qr_expired = login.qr_expired(self._app())
            finally:
                self.gui_lock.release()
        return {
            "state": state,
            "qr_expired": qr_expired,
            "self_id": self.self_id,
            "self_name": self.self_name,
            "notice": self.last_kick if state != "logged_in" else None,
        }

    def http_routes(self):
        """Extra GET routes: binary endpoints for the login/webui flows."""
        def screen(query):
            # 窗口被最小化时 framebuffer 是全黑的——先 map 回来再截
            _main_window_id()
            data = login.screenshot()
            # windowmap 会让 WM 把焦点给回 QQ——截完还回去
            self._schedule_defocus()
            return (200, "image/png", data) if data else None

        def qr(query):
            with self.gui_lock:
                _main_window_id()
                app = self._app()
                state = self._login_state_cached()
                if state not in ("login_required", "unknown"):
                    return (404, "application/json", json.dumps(
                        {"status": "failed", "retcode": 1404, "data": None,
                         "message": f"当前状态 {state}，无可取二维码"},
                        ensure_ascii=False).encode())
                login.prepare(app)
                data = login.qr_png(app)
                self._schedule_defocus()
            if data:
                return (200, "image/png", data)
            return (404, "application/json", json.dumps(
                {"status": "failed", "retcode": 1404, "data": None,
                 "message": "未能取得二维码"}, ensure_ascii=False).encode())

        return {"/screen": screen, "/qr": qr}

    def _require_logged_in(self):
        state = self._login_state_cached()
        if state != "logged_in":
            raise ApiError(1400, f"当前状态 {state}，管理操作不可用")

    def act_delete_msg(self, p):
        """OneBot delete_msg：撤回消息（GUI 右键菜单 → 撤回）。"""
        from . import admin

        self._require_logged_in()
        ob_id = int(p.get("message_id") or 0)
        row = self.ids.get(ob_id)
        if row is None or not row.get("msg_id"):
            raise ApiError(1404, "message_id 不存在或不可定位")
        with self.gui_lock:
            app = self._app()
            if app is None:
                raise ApiError(1400, "QQ 不在无障碍树里")
            try:
                admin.recall_message(
                    app, row["conv_uin"], row["msg_id"],
                    cache=self.cache, verify_identity=self.verify_identity,
                )
            except SendError as exc:
                raise ApiError(1400, str(exc)) from exc
        return {}

    def act_send_poke(self, p):
        """自定义 send_poke（go-cqhttp 式）：戳一戳群成员。"""
        from . import admin

        self._require_logged_in()
        group_id = p.get("group_id")
        user_id = p.get("user_id")
        if not group_id or not user_id:
            raise ApiError(1400, "缺少 group_id/user_id")
        with self.gui_lock:
            app = self._app()
            if app is None:
                raise ApiError(1400, "QQ 不在无障碍树里")
            try:
                admin.poke_member(
                    app, str(group_id), str(user_id), self.ids,
                    cache=self.cache, verify_identity=self.verify_identity,
                )
            except SendError as exc:
                raise ApiError(1400, str(exc)) from exc
        return {}

    def act_set_group_ban(self, p):
        """OneBot set_group_ban：右键头像 → 禁言对话框（分钟粒度，向上取整）。"""
        from . import admin

        self._require_logged_in()
        group_id = p.get("group_id")
        user_id = p.get("user_id")
        if not group_id or not user_id:
            raise ApiError(1400, "缺少 group_id/user_id")
        duration = int(p.get("duration") or 0)
        with self.gui_lock:
            app = self._app()
            if app is None:
                raise ApiError(1400, "QQ 不在无障碍树里")
            try:
                admin.ban_member(
                    app, str(group_id), str(user_id), duration, self.ids,
                    cache=self.cache, verify_identity=self.verify_identity,
                    deadline=time.monotonic() + 45.0,
                )
            except SendError as exc:
                raise ApiError(1400, str(exc)) from exc
        return {}

    def act_get_status(self, p):
        if self.gui_lock.acquire(timeout=3.0):
            try:
                app = self._app()
            finally:
                self.gui_lock.release()
        else:
            # GUI 忙：不碰 a11y，按 QQ 进程在不在回答
            from .__main__ import find_qq_pid

            app = find_qq_pid()
        return {
            "app_initialized": True,
            "app_enabled": True,
            "plugins_good": True,
            "app_good": app is not None,
            "online": app is not None,
            "good": app is not None,
        }

    def act_get_version_info(self, p):
        from . import __version__

        return {
            "app_name": "whiskerwind",
            "app_version": __version__,
            "protocol_version": "v11",
            "nt_protocol": "linux-qq-gui",
        }

    def act_can_send_image(self, p):
        return {"yes": True}

    def act_can_send_record(self, p):
        return {"yes": False}


def _truthy(v):
    return v in (True, "true", "1", 1, "yes")


def _select_emit(result):
    """Messages to emit from one poll pass.

    first_run (cursor has no history for the conversation) emits just the
    newest non-system message, never the whole viewport dump. On the
    notification path that's exactly what the notification pointed at; on
    the poll path it also catches messages whose notification died in a
    serve crash instead of swallowing them into the seen set.
    """
    if not result.first_run:
        return list(result.new)
    tail = [m for m in result.new if m.kind != "system"]
    return tail[-1:]


def send_open(app, uin, cache):
    """Fallback open when ActionInvoked did not route the conversation."""
    from .send import open_conversation_by_uin

    return open_conversation_by_uin(app, str(uin), cache=cache)


ACTIONS = {
    "send_msg": Bridge.act_send_msg,
    "send_private_msg": Bridge.act_send_private_msg,
    "send_group_msg": Bridge.act_send_group_msg,
    "delete_msg": Bridge.act_delete_msg,
    "send_poke": Bridge.act_send_poke,
    "set_group_ban": Bridge.act_set_group_ban,
    "get_login_info": Bridge.act_get_login_info,
    "get_friend_list": Bridge.act_get_friend_list,
    "get_group_list": Bridge.act_get_group_list,
    "get_group_info": Bridge.act_get_group_info,
    "get_group_member_list": Bridge.act_get_group_member_list,
    "get_group_member_info": Bridge.act_get_group_member_info,
    "resolve_member": Bridge.act_resolve_member,
    "get_msg": Bridge.act_get_msg,
    "get_msg_history": Bridge.act_get_msg_history,
    "get_login_state": Bridge.act_get_login_state,
    "get_status": Bridge.act_get_status,
    "get_system_info": Bridge.act_get_system_info,
    "get_version_info": Bridge.act_get_version_info,
    "can_send_image": Bridge.act_can_send_image,
    "can_send_record": Bridge.act_can_send_record,
    "get_logs": Bridge.act_get_logs,
    "list_transports": Bridge.act_list_transports,
    "list_protocols": Bridge.act_list_protocols,
    "set_protocol": Bridge.act_set_protocol,
    "get_protocol_token": Bridge.act_get_protocol_token,
    "add_transport": Bridge.act_add_transport,
    "del_transport": Bridge.act_del_transport,
    "get_test_trigger": Bridge.act_get_test_trigger,
    "set_test_trigger": Bridge.act_set_test_trigger,
    "get_resolve_first": Bridge.act_get_resolve_first,
    "set_resolve_first": Bridge.act_set_resolve_first,
    "get_member_ttl": Bridge.act_get_member_ttl,
    "set_member_ttl": Bridge.act_set_member_ttl,
    "run_selftest": Bridge.act_run_selftest,
}

ADMIN_ACTIONS = {"get_logs", "list_transports", "add_transport", "del_transport",
                 "list_protocols", "set_protocol", "get_protocol_token",
                 "set_test_trigger", "set_resolve_first", "set_member_ttl",
                 "run_selftest"}


def poll_loop(bridge, interval, stop):
    """Safety net for the currently-open conversation.

    QQ suppresses desktop notifications for the conversation that is already
    open, so the notification-triggered path alone is blind to messages
    arriving in whatever chat is on screen. A light periodic read of the
    current conversation covers the hole — one dump per tick, no window
    movement. 主通道是通知驱动（见 on_notification），poll 只是兜底。
    """
    while not stop.wait(interval):
        try:
            # 通知刚到达时让路：poll 持锁读树会饿死通知 worker（等锁超时
            # 5 次放弃 → 消息通知全丢）。通知来了就跳过本轮 poll。
            if time.monotonic() - bridge._notify_since < 2.0:
                continue
            # 登录页上不存在"打开的会话"——跳过整树遍历，别把 a11y 压垮
            if bridge._login_state_cached(ttl=4.0) != "logged_in":
                continue
        except Exception:
            continue
        with bridge.gui_lock:
            try:
                app = bridge._app()
                if app is None:
                    continue
                # 被系统通知/误操作带离了消息页就切回来——聊天区节点在
                # 联系人页只是隐藏、header 名字照样读得到，不能等它读空才管
                if on_message_tab(app) is False:
                    ensure_message_tab(app)
                name = current_conversation(app)
                if not name:
                    bridge._defocus()
                    continue
                uin, kind = bridge._resolve_uin(app, name, name)
                if uin is None:
                    bridge._defocus()
                    continue
                bridge.poll_emit(app, str(uin), kind)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning(f"打开会话轮询失败: {exc}")


def _login_state(bridge):
    """login.login_state under the gui lock; 'unknown' on any failure."""
    try:
        if not bridge.gui_lock.acquire(timeout=10.0):
            # GUI 忙：沿用最近已知态，别因为拿不到锁就推一个假的状态变化
            probe = bridge._login_probe
            return probe[1] if probe else "unknown"
        notice = None
        try:
            app = bridge._app()
            state = login.login_state(app)
            if state == "login_required":
                # 被挤下线/登录失败的通知框挡在登录页上：点掉，扫码/登录才能继续。
                # 不自动重新登录——被另一台终端挤下线时自动登回去会把对方
                # 挤掉，两边来回踢
                notice = login.dismiss_notice(app)
        finally:
            bridge.gui_lock.release()
        if notice:
            title, text = notice
            logger.warning(f"QQ 弹出「{title}」，已点确定: {text}")
            # 弹框已经点掉了，原文只剩这一份：留给 get_login_state，WebUI
            # 晚打开也能看到为什么掉线
            ev = core.SystemNotification(title=title, text=text)
            bridge.last_kick = {"title": title, "text": text, "time": int(ev.time)}
            bridge.publish(ev)
        if state == "logged_in":
            bridge.last_kick = None
        # 顺手刷新 TTL 缓存，让 act_get_login_state 直接命中
        bridge._login_probe = (time.monotonic(), state)
        return state
    except Exception:
        return "unknown"


def heartbeat_tick(bridge, interval, last_state):
    """One heartbeat pass; returns the current login state for the next tick.

    Login-state transitions emit a ``meta_event/login_state`` event — this is
    how a kick-off (modal dialog -> login window) reaches the BFF without
    polling get_login_state.
    """
    state = _login_state(bridge)
    bridge.publish(core.Heartbeat(interval=interval, state=state))
    if state != last_state:
        bridge.publish(core.LoginChanged(prev=last_state, state=state))
        if state == "logged_in" and last_state is not None:
            # 掉线期间的消息登录后同步进来，不弹通知
            bridge.request_sweep()
    return state


def heartbeat_loop(bridge, interval, stop):
    last = None
    while not stop.wait(interval):
        last = heartbeat_tick(bridge, interval, last)
