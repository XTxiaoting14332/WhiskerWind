"""AT-SPI helpers for driving a running QQ (Electron/Chromium) instance."""

import time
import warnings

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

# libatspi 底层走 libdbus——atspi_init() 负责调 dbus_threads_init_default()
# 打开 libdbus 的线程锁。跳过它，多线程（poll/通知 worker/HTTP）走
# libatspi→libdbus 时锁未初始化，连接内部状态被踩坏：生产环境表现为
# malloc fastbin 损坏 / libdbus 内段错误，gdb 实锤过。
Atspi.init()

MAX_DEPTH = 30

# pyatspi 同步调用默认无超时——QQ 的 a11y 连接卡死/断开时，任何节点方法调用
# 会永久阻塞（曾拖死 gui_lock 导致 /qr、login_state、轮询全部挂死）。
# 2s 常规超时让坏调用快速抛错；启动期给 15s 宽容。
try:
    Atspi.set_timeout(2000, 15000)
except Exception:
    pass

# 熔断：a11y 连接整体病变时每个节点调用都超时——树上几百个节点 × 2s
# 依旧等于锁死。连续失败达到阈值后短路一段时间，所有遍历视为空树，
# 让持锁路径快速返回；连接恢复（调用成功）后自动复位。
_SICK_TRIP = 4
_SICK_SECS = 15.0
_sick_until = 0.0
_fail_streak = 0


def _note_fail():
    global _sick_until, _fail_streak
    _fail_streak += 1
    if _fail_streak >= _SICK_TRIP:
        _sick_until = time.monotonic() + _SICK_SECS
        _fail_streak = 0


def _note_ok():
    global _fail_streak
    _fail_streak = 0


def _sick():
    return time.monotonic() < _sick_until


def desktop():
    return Atspi.get_desktop(0)


def find_app(pid):
    root = desktop()
    try:
        count = root.get_child_count()
    except Exception:
        _note_fail()
        return None
    _note_ok()
    for i in range(count):
        try:
            app = root.get_child_at_index(i)
            if app and app.get_process_id() == pid:
                _note_ok()
                return app
        except Exception:
            _note_fail()
            break
    return None


def ui_root(app):
    """The live main-window frame.

    QQ leaks dead/hidden toplevels (old pickers, closed popups, helper
    windows) into the same application tree, and their accessible nodes stay
    fully formed — lookups that scan the whole app hit stale frames at
    random. The real UI is the frame that owns the sidebar 会话列表; fall
    back to the largest frame when the sidebar isn't mounted yet.
    """
    sidebar = find_one(app, name="会话列表")
    if sidebar is not None:
        node = sidebar
        while node is not None:
            if node_role(node) in ("frame", "window"):
                return node
            node = parent(node)
    best, best_area = None, 0
    for n in walk(app):
        if node_role(n) in ("frame", "window"):
            ext = extents(n)
            if ext and ext[2] * ext[3] > best_area:
                best, best_area = n, ext[2] * ext[3]
    return best or app


def children(node):
    if _sick():
        return
    try:
        count = node.get_child_count()
    except Exception:
        _note_fail()
        return
    _note_ok()
    for i in range(count):
        try:
            child = node.get_child_at_index(i)
        except Exception:
            # 节点已死——其余子项同理，继续遍历只会白白吃满超时
            _note_fail()
            break
        _note_ok()
        if child is not None:
            yield child


def walk(node, max_depth=MAX_DEPTH):
    yield node
    if max_depth <= 0:
        return
    for child in children(node):
        yield from walk(child, max_depth - 1)


def node_role(node):
    try:
        return node.get_role_name()
    except Exception:
        return None


def node_name(node):
    try:
        return node.get_name() or ""
    except Exception:
        return ""


def find_all(root, role=None, name=None):
    if root is None:
        return []
    return [
        n
        for n in walk(root)
        if (role is None or node_role(n) == role)
        and (name is None or node_name(n) == name)
    ]


def find_one(root, role=None, name=None):
    if root is None:
        return None
    for n in walk(root):
        if (role is None or node_role(n) == role) and (name is None or node_name(n) == name):
            return n
    return None


def actions(node):
    try:
        count = Atspi.Action.get_n_actions(node)
    except Exception:
        return []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return [Atspi.Action.get_action_name(node, i) for i in range(count)]


def invoke(node, action=None, index=0):
    names = actions(node)
    if not names:
        return False
    if action is None:
        return bool(Atspi.Action.do_action(node, index))
    if action not in names:
        return False
    return bool(Atspi.Action.do_action(node, names.index(action)))


def extents(node):
    try:
        e = node.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
        return e.x, e.y, e.width, e.height
    except Exception:
        return None


def parent(node):
    try:
        return node.get_parent()
    except Exception:
        return None


def ancestor_with_class(node, token, limit=6):
    current = node
    for _ in range(limit):
        current = parent(current)
        if current is None:
            return None
        if has_class(current, token):
            return current
    return None


def attrs(node):
    """Chromium exposes the DOM's class/id/tag through AT-SPI object attributes."""
    try:
        return node.get_attributes() or {}
    except Exception:
        return {}


def attr(node, key, default=""):
    return attrs(node).get(key, default)


def has_class(node, token):
    return token in attr(node, "class").split()


def find_by_class(root, token, max_depth=MAX_DEPTH):
    if root is None:
        return None
    for n in walk(root, max_depth=max_depth):
        if has_class(n, token):
            return n
    return None


def find_all_by_class(root, token, max_depth=MAX_DEPTH):
    if root is None:
        return []
    return [n for n in walk(root, max_depth=max_depth) if has_class(n, token)]


def text_of(node, max_depth=MAX_DEPTH):
    """Concatenate every static descendant in document order.

    QQ wraps IPs, links and @-mentions in nested sections, so the visible text
    of one message is spread over several nodes.
    """
    parts = []
    for n in walk(node, max_depth=max_depth):
        if node_role(n) == "static":
            parts.append(node_name(n))
    return "".join(parts)


def dump_tree(node, max_depth=MAX_DEPTH):
    if node is None or max_depth < 0:
        return None
    return {
        "role": node_role(node),
        "name": node_name(node),
        "attrs": attrs(node),
        "children": [c for c in (dump_tree(ch, max_depth - 1) for ch in children(node)) if c],
    }


def wait_for(root_fn, role=None, name=None, timeout=2.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = find_one(root_fn(), role=role, name=name)
        if found is not None:
            return found
        time.sleep(interval)
    return None


def wait_gone(root_fn, role=None, name=None, timeout=2.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_one(root_fn(), role=role, name=name) is None:
            return True
        time.sleep(interval)
    return False
