"""Outbound messages: switch conversation, fill the composer, press send.

The composer is a ProseMirror contenteditable. It exposes no EditableText
interface, so text cannot be set through accessibility — it has to go through
the clipboard plus a real Ctrl+V. Everything else is semantic:

    div.ProseMirror...is-empty        composer is empty
    div.send.send--disabled           nothing to send yet
    button '发送'                      action: press
"""

import re
import shutil
import subprocess
import time

from loguru import logger

from . import atspi, clipboard, peer
from .messages import parse_messages

CONVERSATION_LIST = "会话列表"
SENTINEL = "__qqcache__"

AT_MARKER = re.compile(r"\{at:(\d+)\}")


def parse_segments(text):
    """Split text into composer ops: ``{at:12345}`` becomes a mention.

    Returns a list of ``("text", str)`` / ``("at", uin)`` tuples.
    """
    segments, last = [], 0
    for m in AT_MARKER.finditer(text or ""):
        if m.start() > last:
            segments.append(("text", text[last : m.start()]))
        segments.append(("at", m.group(1)))
        last = m.end()
    if last < len(text or ""):
        segments.append(("text", text[last:]))
    return segments


class SendError(RuntimeError):
    pass


def conversation_list(app):
    node = atspi.find_one(atspi.ui_root(app), role="embedded", name=CONVERSATION_LIST)
    if node is None and ensure_message_tab(app):
        node = atspi.find_one(atspi.ui_root(app), role="embedded", name=CONVERSATION_LIST)
    if node is None:
        raise SendError("找不到「会话列表」")
    return node


def message_tab_button(app):
    """左侧导航栏的「消息」切换按钮——只有登录后的主界面才有。"""
    return atspi.find_one(atspi.ui_root(app), role="toggle button", name="消息")


def on_message_tab(app):
    """主界面当前是否在「消息」页：True/False，不在主界面（登录页等）为 None。

    「消息」是个切换按钮——选中时动作是 uncheck，没选中时是 check。
    比找会话列表便宜（导航栏在树的最前面），轮询里可以每 tick 查。
    """
    button = message_tab_button(app)
    if button is None:
        return None
    return "uncheck" in atspi.actions(button)


def ensure_message_tab(app, timeout=2.0):
    """主界面不在「消息」页（被系统通知带去了联系人/群通知页等）时切回来。

    返回是否做了切换且会话列表已出现。不在主界面（登录页等）什么都不做。
    """
    if atspi.find_one(atspi.ui_root(app), role="embedded", name=CONVERSATION_LIST) is not None:
        return False
    button = message_tab_button(app)
    if button is None:
        return False
    if not (atspi.invoke(button, "check") or atspi.invoke(button)):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if atspi.find_one(atspi.ui_root(app), role="embedded", name=CONVERSATION_LIST) is not None:
            logger.info("主界面不在消息页，已切回")
            return True
        time.sleep(0.1)
    return False


def conversation_items(app):
    for item in atspi.children(conversation_list(app)):
        for node in atspi.walk(item, max_depth=6):
            if atspi.node_role(node) == "static":
                yield atspi.node_name(node), item
                break


def conversation_unreads(app):
    """(name, unread) for every visible sidebar conversation with an unread
    badge. QQ renders unread counts as ``q-badge-num`` nodes inside each
    ``recent-contact-item``; the number updates as soon as a message arrives
    (no desktop-notification dependency, no delay)."""
    out = []
    for item_name, item in conversation_items(app):
        for node in atspi.walk(item, max_depth=8):
            if "q-badge-num" not in (atspi.attr(node, "class") or ""):
                continue
            try:
                txt = atspi.text_of(node).strip()
            except Exception:
                continue
            if not txt:
                continue
            try:
                n = int(txt)
            except ValueError:
                n = 1  # "99+" 之类的溢出显示
            if n > 0:
                out.append((item_name, n))
            break  # 每个会话项只取一个 badge
    return out


def current_conversation(app):
    """Name shown in the chat header, or None when no chat is open."""
    node = atspi.find_by_class(atspi.ui_root(app), "chat-header__contact-name")
    return atspi.node_name(node) or None


def open_conversation(app, name, timeout=3.0, scroll=True):
    """Click a conversation in the sidebar and wait for the header to switch.

    Clicking the already-open conversation collapses the chat area, so the
    no-op case has to be detected before clicking.

    ``scroll=False``（通知路径用）：只查当前渲染的可见项，找不到直接抛错
    ——不滚动侧栏、不输入搜索框。滚动/搜索是渲染器崩溃高发区，通知路径
    靠 ActionInvoked 切会话（QQ 内部路由），这里的主动打开只是轻量兜底。
    """
    if current_conversation(app) == name:
        return True

    item = _find_conversation_item(app, name) if scroll else _visible_conversation_item(app, name)
    if item is None:
        return search_and_open(app, name, timeout=timeout) if scroll else _open_fail(name)
    # invoke click 对 QML 侧栏项是假阳性高发——先 invoke，0.5s 无切换
    # 就真左键点项中心兜底
    atspi.invoke(item, "click")
    deadline = time.monotonic() + timeout
    fallback_at = time.monotonic() + 0.5
    real_clicked = False
    while time.monotonic() < deadline:
        if current_conversation(app) == name:
            return True
        if not real_clicked and time.monotonic() >= fallback_at:
            ext = atspi.extents(item)
            if ext:
                subprocess.run(
                    ["xdotool", "mousemove",
                     str(ext[0] + ext[2] // 2), str(ext[1] + ext[3] // 2),
                     "click", "1"],
                    capture_output=True,
                )
            real_clicked = True
        time.sleep(0.05)
    raise SendError(f"切到 {name!r} 超时")


def _visible_conversation_item(app, name):
    for item_name, item in conversation_items(app):
        if item_name == name:
            return item
    return None


def _open_fail(name):
    raise SendError(f"搜索结果里没有 {name!r}")


def _find_conversation_item(app, name, max_steps=60, stall_limit=4):
    """侧栏是虚拟化列表：渲染出来的项里找不到时，逐屏滚动侧栏，每屏读一遍
    名字。不输入、不粘贴、不切搜索态——比搜索框轻得多，搜索框是渲染器崩溃
    的高发路径，只留作这里也找不到时的最后兜底。
    滚动优先走 AT-SPI scrollForward；渲染器崩溃重生后该动作可能变死链
    （invoke 返回 True 但列表不动）——用真实滚轮兜底，以「可见首项变化」
    判定是否真的动了。"""
    for item_name, item in conversation_items(app):
        if item_name == name:
            return item
    lst = conversation_list(app)
    seen, stall, found = set(), 0, None
    for _ in range(max_steps):
        if not _scroll_conversations(app, lst):
            break
        time.sleep(0.35)
        grew = False
        for item_name, item in conversation_items(app):
            if item_name == name:
                found = item
                break
            if item_name not in seen:
                seen.add(item_name)
                grew = True
        if found is not None:
            return found
        stall = 0 if grew else stall + 1
        if stall >= stall_limit:
            break
    _scroll_conversations_top(app, lst)
    return None


def _scroll_conversations(app, lst, up=False):
    """滚一屏；返回是否真的发生了滚动（可见首项变了）。"""
    before = next((n for n, _ in conversation_items(app)), None)
    if atspi.invoke(lst, "scrollBackward" if up else "scrollForward"):
        time.sleep(0.3)
        after = next((n for n, _ in conversation_items(app)), None)
        if after is not None and after != before:
            return True
    # AT-SPI 滚动是死链（invoke 成功但列表不动）——真滚轮兜底
    ext = atspi.extents(lst)
    if not ext:
        return False
    subprocess.run(
        ["xdotool", "mousemove", str(ext[0] + ext[2] - 6),
         str(ext[1] + ext[3] // 2), "click", "4" if up else "5"],
        capture_output=True, timeout=3,
    )
    time.sleep(0.3)
    after = next((n for n, _ in conversation_items(app)), None)
    return after is not None and after != before


def _scroll_conversations_top(app, lst, max_steps=80):
    prev = None
    for _ in range(max_steps):
        if not _scroll_conversations(app, lst, up=True):
            break
        cur = next((n for n, _ in conversation_items(app)), None)
        if cur == prev:
            break
        prev = cur


def _name_base(s):
    """Display name minus the decorations QQ appends/truncates: member counts
    ``(145人)``, ellipsis, and the header's trailing count."""
    return re.split(r"[（(…]", s)[0].rstrip(" .…")


def _names_match(a, b):
    """Tolerant compare: result rows and the header truncate long names."""
    a, b = _name_base(a), _name_base(b)
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def search_and_open(app, name, timeout=4.0):
    """Open a conversation that is not in the visible sidebar via the search box.

    The sidebar is virtualized: targets below the fold are absent from the
    a11y tree entirely. The search box is the stable path — typing the name
    lists matching conversations under a ``群聊``/``好友`` category as
    ``list item`` nodes named e.g. ``'files (4人)'``.
    """
    _paste_query(app, name)

    target = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and target is None:
        for item in atspi.find_all(atspi.ui_root(app), role="list item"):
            cl = atspi.attr(item, "class")
            if "search-result" not in cl or "related-search" in cl:
                continue
            if _names_match(atspi.node_name(item), name):
                target = item
                break
        if target is None:
            time.sleep(0.15)

    if target is None:
        raise SendError(f"搜索结果里没有 {name!r}")
    _open_result(target)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = current_conversation(app)
        if cur and _names_match(cur, name):
            return True
        time.sleep(0.05)
    raise SendError(f"搜索后切到 {name!r} 超时")


def _open_result(item):
    """Open a search-result item.

    Conversation results open on ``select``; contact hits only get highlighted
    — those still need a ``click`` on their inner section or Enter. Probe by
    whether the dropdown is still up.
    """
    def dropdown_alive():
        root = atspi.ui_root(item.get_application())
        return any(
            "search" in atspi.attr(i, "class")
            for i in atspi.find_all(root, role="list item")
        )

    atspi.invoke(item, "select")
    time.sleep(0.4)
    if not dropdown_alive():
        return
    for sub in atspi.walk(item):
        if atspi.invoke(sub, "click"):
            time.sleep(0.4)
            if not dropdown_alive():
                return
            break
    _key("Return", node=item)
    time.sleep(0.3)
    # invoke/Enter 全假阳性时最后一道兜底：真鼠标点项中心。
    # 下拉若视觉已关只是 a11y 僵尸节点残留，点陈旧坐标顶多点歪——
    # 比「必然打不开」强
    if dropdown_alive():
        _click_node_center(item)


def _paste_query(app, query):
    activate_qq()
    entry = atspi.find_one(atspi.ui_root(app), role="entry")
    if entry is None:
        raise SendError("找不到搜索框")
    try:
        entry.get_component_iface().grab_focus()
    except Exception as exc:
        raise SendError(f"聚焦搜索框失败: {exc}")
    time.sleep(0.3)
    # 上次搜索的残留文字会和新查询拼接，先清空再贴
    _key("ctrl+a", node=entry)
    _key("Delete", node=entry)
    time.sleep(0.15)
    if not _claim_clipboard(query):
        raise SendError("剪贴板没有拿到文本")
    _key("ctrl+v", node=entry)


def _wait_header(app, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if current_conversation(app):
            return True
        time.sleep(0.05)
    return False


def _dropdown_candidates(app, uin, timeout, limit=3):
    """Contact-shaped hits in the main-panel search dropdown, best first.

    A user-number query can render the friend as ``名字 (uin) 来自: 我的好友``
    (bracketed number = the peer) or bare-name recent entries; ``包含：uin``
    groups and file results are decoys. Result texts are not trustworthy, so
    candidates are only pre-filtered — the profile card is the arbiter.
    """
    strong, weak = [], []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not strong and not weak:
        strong, weak = [], []
        for it in atspi.find_all(atspi.ui_root(app), role="list item"):
            cl = atspi.attr(it, "class")
            # 好友直达是 search-result-list，群成员命中是 search-result-item
            if "search-result" not in cl or "related-search" in cl:
                continue
            text = atspi.text_of(it)
            if any(s in text for s in ("进入综合搜索", "发给", "包含：", "包含:", "相关聊天记录")):
                continue
            (strong if uin in text else weak).append(it)
        if not strong and not weak:
            time.sleep(0.15)
    return (strong + weak)[:limit]


def _open_via_global_search(app, uin, timeout=4.0):
    """Drive the 综合搜索 window to find a peer by number.

    Works for QQ numbers; group numbers are deliberately not listed in this
    client's public search. Selecting a hit carries its number in the result
    text, so only exact-number rows are candidates.
    """
    _paste_query(app, uin)
    related = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and related is None:
        for it in atspi.find_all(atspi.ui_root(app), role="list item"):
            if "related-search" in atspi.attr(it, "class"):
                related = it
                break
        if related is None:
            time.sleep(0.15)
    if related is None:
        raise SendError(f"搜索框没有出现 {uin} 的综合搜索入口")
    if not atspi.invoke(related, "select"):
        _open_result(related)

    window = atspi.wait_for(lambda: app, name="综合搜索", timeout=timeout)
    if window is None:
        raise SendError("综合搜索窗口没有打开")
    try:
        button = atspi.wait_for(lambda: window, role="button", name="搜索", timeout=timeout)
        if button is None or not atspi.invoke(button, "press"):
            raise SendError("综合搜索的「搜索」按钮不可用")

        target = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and target is None:
            for it in atspi.find_all(window, role="list item"):
                if "search-result-item" in atspi.attr(it, "class").split() and uin in atspi.text_of(it):
                    target = it
                    break
            if target is None:
                time.sleep(0.15)
        if target is None:
            raise SendError(f"综合搜索没有 {uin} 的结果")
        _open_result(target)
        if not _wait_header(app, timeout):
            raise SendError(f"打开 {uin} 的会话超时")
        return current_conversation(app)
    finally:
        for wid in peer._card_windows() + _named_windows("综合搜索"):
            try:
                subprocess.run(["xdotool", "windowclose", wid], capture_output=True, timeout=2)
            except (subprocess.SubprocessError, OSError):
                pass


def _named_windows(name):
    try:
        return [
            w.decode()
            for w in subprocess.run(
                ["xdotool", "search", "--name", name], capture_output=True, timeout=2
            ).stdout.split()
        ]
    except (subprocess.SubprocessError, OSError):
        return []


def _verify_uin(app, uin, retries=2):
    """Best-effort identity proof with retries.

    Card nodes persist in the a11y tree forever — a residual member card
    makes uin_of read someone else's number, which is 「未证明」, not
    「验证失败」: retry rather than fail the open outright.
    """
    for _ in range(retries):
        try:
            if peer.uin_of(app).uin == uin:
                return True
        except peer.PeerError:
            pass
        time.sleep(0.3)
    return False


def _assert_send_target(app, uin, cache):
    """发送前的最终闸：目标会话必须同时满足「名字匹配缓存」和「资料卡
    验号」。任何一环失败立即抛错——宁可不发，绝不在错误的会话里敲字。"""
    row = cache.lookup(uin) if cache else None
    want = row["name"] if row else None
    cur = current_conversation(app)
    if want and not _names_match(cur or "", want):
        raise SendError(f"发送目标被切换: 当前会话 {cur!r} ≠ 目标 {want!r}")
    if not _verify_uin(app, uin):
        raise SendError(f"目标 {uin} 身份验证失败，拒绝发送")


def _assert_unchanged(app, uin, cache, opened_name, known_ids):
    """粘贴前确认验号之后会话没被换掉——不再点第二次资料卡（2-4s）。

    会话名 + 消息 ID：msg_id 全局唯一，快照有交集就证明还是验号时那个
    会话（同名的另一个群不可能有交集）。没有交集不等于被换——刷屏也会
    整屏顶掉——所以无交集只触发退回资料卡验号，不直接拒发。"""
    cur = current_conversation(app)
    if not (cur and opened_name and _names_match(cur, opened_name)):
        raise SendError(f"发送目标被切换: 当前会话 {cur!r} ≠ 验号时的 {opened_name!r}")
    if known_ids and known_ids & snapshot_ids(app):
        return
    # 快照为空（新会话没消息）或没有交集：可能是被换成了同名会话，也可能
    # 只是群里刷屏把可见消息整屏顶掉了（虚拟列表只留可见的十几条）。
    # 分不清就退回资料卡验号——慢一点，但刷屏时不误拒、换会话时照样拦住。
    logger.info(f"发送复核 {uin}: 消息快照无法确认，退回资料卡验号")
    _assert_send_target(app, uin, cache)


def open_conversation_by_uin(app, uin, cache=None, timeout=4.0, verify=True):
    """Open the conversation whose peer uin/group number is ``uin``.

    Groups cannot be found by number anywhere in this client, so a group uin
    must come through the name cache (``uin -> name -> open -> verify``). QQ
    numbers additionally have the global-search path. Either way the opened
    chat is proven by its profile card before returning.
    """
    # 0) 当前打开的会话可能本来就是目标（免一次搜索和换页）
    if verify and current_conversation(app) and _verify_uin(app, uin):
        return current_conversation(app)
    # 1) 缓存里的名字（群号只能靠这条路；私聊也行）
    if cache is not None:
        row = cache.lookup(uin)
        if row and row.get("name"):
            try:
                open_conversation(app, row["name"], timeout=timeout)
            except SendError as exc:
                logger.info(f"按缓存名 {row['name']!r} 打开 {uin} 失败: {exc}")
            else:
                if not verify or _verify_uin(app, uin):
                    return current_conversation(app)
                # 验证失败即失败：继续 fallthrough 只会误开别的会话并发错
                raise SendError(f"目标 {uin} 身份验证失败，拒绝打开/发送")
    # 2) 搜索下拉：逐个候选打开，靠资料卡裁决
    _paste_query(app, uin)
    for hit in _dropdown_candidates(app, uin, timeout):
        prev = current_conversation(app)
        _open_result(hit)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and current_conversation(app) == prev:
            time.sleep(0.1)
        time.sleep(0.3)
        if not verify or _verify_uin(app, uin):
            return current_conversation(app)
    if verify:
        raise SendError(f"目标 {uin} 身份验证失败，拒绝打开/发送")
    try:
        name = _open_via_global_search(app, uin, timeout=timeout)
    except SendError:
        name = None
    if name is not None and (not verify or _verify_uin(app, uin)):
        return name
    # verify 失败一律拒绝，不做「名字匹配缓存就采信」的降级——名字匹配
    # 不证明号对得上，降级正是发错会话的洞（files 串发事故的直接原因）。
    raise SendError(
        f"按 {uin} 没有找到会话"
        "（群号不可直接搜索，需要先 sync 入缓存或改用 --name）"
    )


def composer(app):
    return atspi.find_by_class(atspi.ui_root(app), "qq-msg-editor__root")


def _rect_overlap(a, b):
    return not (
        a[0] >= b[0] + b[2] or b[0] >= a[0] + a[2]
        or a[1] >= b[1] + b[3] or b[1] >= a[1] + a[3]
    )


def muted_reason(app):
    """禁言/输入框不可用的提示文本，没有则 None。

    只认输入区内的提示：composer 容器内部、或消息列表底缘之下。
    消息列表里的「已被禁言」系统灰条和群名带「禁言」的标题都不能误伤。
    """
    root = atspi.ui_root(app)
    comp = composer(app)
    comp_ext = atspi.extents(comp) if comp is not None else None
    ml = atspi.find_one(root, role="embedded", name="消息列表")
    ml_ext = atspi.extents(ml)
    for n in atspi.walk(root):
        name = (atspi.node_name(n) or "").strip()
        if "禁言" not in name:
            continue
        ext = atspi.extents(n)
        if ext is None:
            continue
        if comp_ext is not None and _rect_overlap(ext, comp_ext):
            return name
        if ml_ext is not None and ext[1] >= ml_ext[1] + ml_ext[3] - 4:
            return name
    return None


# 输入框/发送按钮节点缓存：paste/press 的轮询每 50ms 问一次「空了吗」，
# 每次都从窗口根整树遍历要 ~0.3s（实测一次发送 1.5 万次节点访问、3s）。
# 缓存的节点每次用前做一次廉价复核（类名/角色名 + 可见尺寸），节点被
# 销毁（切会话重建 DOM、渲染器重生）时复核失败，退回整树查找。
_node_cache = {}


def _visible(node):
    ext = atspi.extents(node)
    return bool(ext) and ext[2] > 0 and ext[3] > 0


def _cached_node(key, find, valid):
    node = _node_cache.get(key)
    try:
        if node is not None and valid(node) and _visible(node):
            return node
    except Exception:  # noqa: BLE001 - defunct node
        pass
    node = find()
    if node is None:
        _node_cache.pop(key, None)
    else:
        _node_cache[key] = node
    return node


def editor(app):
    return _cached_node(
        ("editor", id(app)),
        lambda: atspi.find_by_class(atspi.ui_root(app), "ProseMirror"),
        lambda n: atspi.has_class(n, "ProseMirror"),
    )


def is_empty(app):
    node = editor(app)
    return node is None or atspi.has_class(node, "is-empty")


def send_button(app):
    return _cached_node(
        ("send", id(app)),
        lambda: atspi.find_one(atspi.ui_root(app), role="button", name="发送"),
        lambda n: atspi.node_role(n) == "button" and atspi.node_name(n) == "发送",
    )


def send_enabled(app):
    """True/False from the DOM's ``send--disabled`` flag, None if unknown."""
    button = send_button(app)
    if button is None:
        return None
    node = atspi.ancestor_with_class(button, "send")
    if node is None:
        return None
    return not atspi.has_class(node, "send--disabled")


def state(app):
    return {"empty": is_empty(app), "send_enabled": send_enabled(app)}


def _xdotool(*args, timeout=3):
    if not shutil.which("xdotool"):
        raise SendError("需要 xdotool")
    return subprocess.run(["xdotool", *args], capture_output=True, timeout=timeout)


def _main_window_id():
    """QQ 主窗口的 X id；主窗被收进托盘（unmapped）时只把它自己 map 回来。

    QQ 预建了好几个隐藏的 Electron 窗口（资料卡宿主、800x600 默认尺寸的
    空窗…）。旧实现对所有 qq 窗口无差别 windowmap，把这些隐藏窗强行显示
    成桌面上的灰色空白块，Electron 自认的可见状态与 X 实际状态错位——
    疑似资料卡卡死（点头像不再弹卡）的诱因。主窗 = 名为 QQ 的最大窗口。
    """
    try:
        wids = subprocess.run(
            ["xdotool", "search", "--class", "qq"], capture_output=True, timeout=2
        ).stdout.split()
        visible = set(subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "qq"],
            capture_output=True, timeout=2,
        ).stdout.split())
    except (subprocess.SubprocessError, OSError):
        return None
    best, best_area = None, 0
    for wid in wids:
        try:
            name = subprocess.run(
                ["xdotool", "getwindowname", wid.decode()],
                capture_output=True, timeout=2,
            ).stdout.decode().strip()
            if name != "QQ":
                continue
            geo = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", wid.decode()],
                capture_output=True,
                timeout=2,
            ).stdout.decode()
            vals = dict(kv.split("=") for kv in geo.split() if "=" in kv)
            area = int(vals.get("WIDTH", 0)) * int(vals.get("HEIGHT", 0))
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
        if area > best_area:
            best, best_area = wid, area
    if best is not None and best not in visible:
        try:
            subprocess.run(["xdotool", "windowmap", "--sync", best.decode()],
                           capture_output=True, timeout=2)
        except (subprocess.SubprocessError, OSError):
            pass
    return best


def activate_qq(timeout=2):
    """Pull X focus onto the QQ main window.

    AT-SPI ``grab_focus`` only moves Qt's internal focus; ``xdotool key``
    events still go to whatever window X has focused. Without this, Ctrl+V
    and Escape get dropped whenever focus sits on a helper window or nowhere.
    """
    wid = _main_window_id()
    if wid is None:
        return False
    try:
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", wid.decode()],
            capture_output=True,
            timeout=timeout,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _node_window(node):
    """X window whose rect contains the node's center. QQ keeps several
    toplevels around (main UI, search, card overlays), and the widget we want
    to drive is not necessarily in the X-active one."""
    if not shutil.which("xdotool"):
        return None
    ext = atspi.extents(node)
    if ext is None:
        return None
    cx, cy = ext[0] + ext[2] // 2, ext[1] + ext[3] // 2
    try:
        # 只认可见窗口：QQ 有好几个隐藏的 Electron 窗口（400x580、800x600…）
        # 恰好罩住侧栏/消息区且比主窗小，曾被选中再 windowmap，强行显示成
        # 桌面上的灰块、打乱 Electron 窗口状态（资料卡卡死疑与此有关）
        wids = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "qq"],
            capture_output=True, timeout=2,
        ).stdout.split()
    except (subprocess.SubprocessError, OSError):
        return None
    best, best_area = None, None
    for wid in wids:
        try:
            geo = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", wid.decode()],
                capture_output=True,
                timeout=2,
            ).stdout.decode()
            vals = dict(kv.split("=") for kv in geo.split() if "=" in kv)
            x, y, w, h = (int(vals.get(k, 0)) for k in ("X", "Y", "WIDTH", "HEIGHT"))
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
        if w < 4 or h < 4 or not (x <= cx < x + w and y <= cy < y + h):
            continue
        if best_area is None or w * h < best_area:
            best, best_area = wid, w * h
    return best


def _key(*keys, node=None):
    """Keystroke delivery.

    ``key --window`` emits XSendEvent synthetic events, which Qt filters out —
    so when ``node`` is given we instead activate the *real* window hosting it
    (XTEST activation is genuine focus), re-grab the widget, then send a plain
    global keystroke to the now-active window.
    """
    wid = _node_window(node) if node is not None else None
    if wid is not None:
        try:
            active = subprocess.run(
                ["xdotool", "getactivewindow"], capture_output=True, timeout=2
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            active = b""
        if active and active == wid:
            wid = None  # 已经是活动窗口：省掉 map/activate 和 0.25s 等待
    if wid is not None:
        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", wid],
                capture_output=True,
                timeout=3,
            )
            time.sleep(0.15)
            node.get_component_iface().grab_focus()
            time.sleep(0.1)
        except Exception:
            pass
    _xdotool("key", "--clearmodifiers", *keys)


def focus_editor(app):
    node = editor(app)
    if node is None:
        raise SendError("找不到输入框")
    try:
        node.get_component_iface().grab_focus()
    except Exception as exc:
        raise SendError(f"聚焦输入框失败: {exc}") from exc
    return True


def _claim_clipboard(text, timeout=1.0):
    clipboard.write_text(text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (clipboard.read_text() or "") == text:
            return True
        time.sleep(0.02)
    return False


def _click_node_center(node):
    """Real left-click on a node's centre in its owning X toplevel.

    AT-SPI grab_focus only moves Qt's internal focus; a ProseMirror
    contenteditable also needs a DOM caret before ctrl+v lands. The click
    plants the caret and pulls genuine X focus onto the right window.
    """
    ext = atspi.extents(node)
    if ext is None:
        return False
    wid = _node_window(node)
    if wid is not None:
        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", wid],
                capture_output=True, timeout=3,
            )
            time.sleep(0.15)
        except (subprocess.SubprocessError, OSError):
            pass
    try:
        subprocess.run(
            ["xdotool", "mousemove", str(ext[0] + ext[2] // 2),
             str(ext[1] + ext[3] // 2), "click", "1"],
            capture_output=True, timeout=2,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def paste(app, text, timeout=2.0):
    """Focus the composer and paste through the clipboard."""
    node = editor(app)
    if node is None:
        raise SendError("找不到输入框")
    try:
        node.get_component_iface().grab_focus()
    except Exception as exc:
        raise SendError(f"聚焦输入框失败: {exc}") from exc
    if not _claim_clipboard(text):
        raise SendError("剪贴板没有拿到文本")
    _key("ctrl+v", node=node)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_empty(app):
            return True
        time.sleep(0.05)
    # grab_focus 在 QML 下常是假成功：DOM caret 没落位时 ctrl+v 整个丢掉。
    # 真点一次输入框中心补 caret，再重贴一次
    _click_node_center(node)
    if not _claim_clipboard(text):
        raise SendError("剪贴板没有拿到文本")
    _key("ctrl+v", node=node)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_empty(app):
            return True
        time.sleep(0.05)
    raise SendError("粘贴后输入框仍然是空的")


def clear(app):
    node = editor(app)
    if node is None:
        raise SendError("找不到输入框")
    try:
        node.get_component_iface().grab_focus()
    except Exception as exc:
        raise SendError(f"聚焦输入框失败: {exc}") from exc
    _key("ctrl+a", node=node)
    _key("Delete", node=node)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if is_empty(app):
            return True
        time.sleep(0.05)
    return False


def paste_image(app, path, timeout=3.0):
    """Paste an image file into the composer via the clipboard."""
    node = editor(app)
    if node is None:
        raise SendError("找不到输入框")
    try:
        node.get_component_iface().grab_focus()
    except Exception as exc:
        raise SendError(f"聚焦输入框失败: {exc}") from exc
    if not clipboard.write_image(path):
        raise SendError("剪贴板不支持图片写入")
    _key("ctrl+v", node=node)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_empty(app):
            return True
        time.sleep(0.05)
    _click_node_center(node)
    _key("ctrl+v", node=node)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_empty(app):
            return True
        time.sleep(0.05)
    raise SendError("粘贴图片后输入框仍然是空的")


def mention(app, uin, timeout=3.0):
    """Insert a real @mention chip by typing @ + the member's uin.

    QQ's mention panel filters members by uin prefix, so digits alone narrow
    the candidate list to the target; clicking the item inserts the chip.
    ``uin="all"`` picks the 全体成员 entry instead. On failure the typed
    ``@<digits>`` is removed again.

    面板 item 只暴露显示名、不带 uin，所以不能反过来用它解析成员号。
    """
    node = editor(app)
    if node is None:
        raise SendError("找不到输入框")
    try:
        node.get_component_iface().grab_focus()
    except Exception as exc:
        raise SendError(f"聚焦输入框失败: {exc}") from exc
    digits = str(uin)
    want_all = digits == "all"
    _key("at", node=node)
    time.sleep(0.3)
    if not want_all:
        for ch in digits:
            _key(ch, node=node)
            time.sleep(0.05)

    deadline = time.monotonic() + timeout
    items = []
    while time.monotonic() < deadline:
        items = atspi.find_all_by_class(app, "editor-at-panel__item")
        if items:
            break
        time.sleep(0.1)
    target = None
    if items:
        if want_all:
            for it in items:
                if "全体" in atspi.text_of(it):
                    target = it
                    break
        else:
            target = items[0]
    if target is None or not atspi.invoke(target, "click"):
        for _ in range(1 if want_all else len(digits) + 1):
            _key("BackSpace", node=node)
        raise SendError(f"@ 面板没有匹配到 uin={uin} 的成员")
    return True


def reply_to_message(app, msg_id, remove_at=True, timeout=4.0):
    """Context-menu → 引用 on a message, putting the composer into reply mode.

    Scrolls the list until the message materialises if it isn't rendered.
    Group replies auto-insert an @ chip for the original sender; two
    Backspaces drop it while keeping the quote chip.
    """
    from .backfill import find_item_node, scroll_to_item

    item = find_item_node(app, msg_id) or scroll_to_item(app, msg_id)
    if item is None:
        raise SendError(f"消息 {msg_id} 不在消息列表里（上滚下滚都找不到）")
    wrapper = None
    for n in atspi.walk(item):
        if "message-content__wrapper" in atspi.attr(n, "class"):
            wrapper = n
            break
    if wrapper is None:
        raise SendError("消息节点上没有可调出的内容菜单")
    atspi.invoke(wrapper, "showContextMenu")

    deadline = time.monotonic() + timeout
    quote_item = None
    while time.monotonic() < deadline:
        for n in atspi.walk(app):
            if "q-context-menu-item" in atspi.attr(n, "class") and "引用" in (
                atspi.node_name(n) or atspi.text_of(n)
            ):
                quote_item = n
                break
        if quote_item is not None:
            break
        time.sleep(0.1)
    if quote_item is None:
        raise SendError("右键菜单里没有「引用」项")
    atspi.invoke(quote_item, "select")

    deadline = time.monotonic() + timeout
    chip = None
    while time.monotonic() < deadline:
        chip = atspi.find_by_class(atspi.ui_root(app), "editor-el__reply-wrapper")
        if chip is not None:
            break
        time.sleep(0.1)
    if chip is None:
        raise SendError("点「引用」后没有出现引用芯片")

    if remove_at:
        node = editor(app)
        if node is not None:
            try:
                node.get_component_iface().grab_focus()
            except Exception:
                pass
            _key("BackSpace", node=node)
            _key("BackSpace", node=node)
    return True


def press_send(app, timeout=2.0):
    # 粘贴后输入区重渲染，按钮节点会短暂从树里消失——还没按，等它回来是安全的
    deadline = time.monotonic() + 2.0
    button = send_button(app)
    while button is None and time.monotonic() < deadline:
        time.sleep(0.1)
        button = send_button(app)
    if button is None:
        raise SendError("找不到发送按钮")
    if is_empty(app):
        raise SendError("输入框是空的，拒绝发送")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        enabled = send_enabled(app)
        if enabled is None or enabled:
            break
        time.sleep(0.05)
    else:
        raise SendError("发送按钮一直是禁用的")

    if not atspi.invoke(button, "press"):
        raise SendError("发送按钮 press 失败")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if is_empty(app):
            return True
        time.sleep(0.05)
    return False


def snapshot_ids(app):
    message_list = atspi.find_one(atspi.ui_root(app), role="embedded", name="消息列表")
    if message_list is None:
        return set()
    return {m.msg_id for m in parse_messages(atspi.dump_tree(message_list))}


def wait_for_message(app, text, known_ids, timeout=5.0):
    """Confirm delivery by finding a new outgoing message carrying our text."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message_list = atspi.find_one(atspi.ui_root(app), role="embedded", name="消息列表")
        if message_list is not None:
            for message in parse_messages(atspi.dump_tree(message_list)):
                if message.msg_id in known_ids or message.is_self is not True:
                    continue
                if not text.strip() or message.content.strip() == text.strip():
                    return message
        time.sleep(0.2)
    return None


def send(
    app,
    name=None,
    text="",
    uin=None,
    cache=None,
    dry_run=False,
    verify=True,
    verify_identity=True,
    segments=None,
    reply_to=None,
    images=None,
):
    activate_qq()
    """Switch to a conversation and send one message.

    ``name`` targets by display name (sidebar or search); ``uin`` searches the
    number directly and — unless ``verify_identity`` is off — re-reads the
    profile card to prove the opened chat is really that uin before typing.
    ``reply_to`` quotes a message (msg_id) via context-menu 引用;
    ``segments`` mixes ("text", s) / ("at", uin) / ("image", path) ops;
    ``images`` appends image segments last. Returns the delivered Message,
    or None when running dry.
    """
    t0 = time.monotonic()
    marks = []

    def mark(step):
        marks.append(f"{step} {time.monotonic() - t0:.1f}s")

    opened_name = None
    if uin is not None:
        # 资料卡验号（打开即验证）——证明此刻打开的就是 uin 本尊
        open_conversation_by_uin(app, uin, cache=cache, verify=verify_identity)
        opened_name = current_conversation(app)
        mark("打开+验号")
    elif name is None:
        raise SendError("需要 --name 或 --uin")
    else:
        open_conversation(app, name)
    known = snapshot_ids(app)

    ops = list(segments) if segments else parse_segments(text)
    for path in images or []:
        ops.append(("image", path))
    if not ops and not reply_to:
        raise SendError("没有要发送的内容")

    # 输入框不可用（禁言等）：先给明确失败原因，再走逐段填充
    if editor(app) is None:
        muted = muted_reason(app)
        raise SendError(f"输入框不可用（{muted}）" if muted else "找不到输入框")

    if uin is not None and verify_identity:
        # 验号与粘贴之间会话可能被切走——粘贴前再确认一道
        _assert_unchanged(app, uin, cache, opened_name, known)
        mark("复核")

    # 输入框必须从空开始：上一次失败的发送可能把文字留在里面，再粘贴
    # 就是两份内容叠在一条消息里发出去
    if not is_empty(app) and not clear(app):
        raise SendError("输入框有残留内容且清不掉，拒绝发送")

    with clipboard.preserved():
        try:
            _compose_and_press(app, ops, reply_to, dry_run)
            mark("组装+发送")
        except SendError as exc:
            mark(f"失败({exc})")
            logger.info(f"发送 {uin or name}: {' / '.join(marks)}")
            # 发送键之前失败：消息没发出，清掉半成品，别留给下一次叠加
            try:
                clear(app)
            except Exception:  # noqa: BLE001
                pass
            raise
        if dry_run:
            after_paste = state(app)
            cleared = clear(app)
            return {"after_paste": after_paste, "cleared": cleared, "final": state(app)}

    if not verify:
        logger.info(f"发送 {uin or name}: {' / '.join(marks)}")
        return None
    echo = wait_for_message(app, text, known)
    mark("回显" if echo is not None else "回显超时")
    logger.info(f"发送 {uin or name}: {' / '.join(marks)}")
    return echo


def _compose_and_press(app, ops, reply_to, dry_run):
    """Fill the composer segment by segment, then press 发送 (unless dry)."""
    if reply_to:
        reply_to_message(app, reply_to)
    for kind, value in ops:
        if kind == "text":
            paste(app, value)
        elif kind == "at":
            mention(app, value)
        elif kind == "image":
            paste_image(app, value)
        else:
            raise SendError(f"未知的消息段类型 {kind!r}")
    if send_enabled(app) is False:
        raise SendError("组装后发送按钮仍未启用")
    if not dry_run:
        press_send(app)
