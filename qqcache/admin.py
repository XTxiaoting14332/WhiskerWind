"""右键菜单驱动的管理动作：撤回 / 戳一戳 / 禁言。

真机探测结论（QQ 3.2.33, files 群管理员视角）：

- 消息菜单锚点 ``message-content__wrapper``：复制/转发/收藏/多选/引用/删除，
  自己消息额外有「撤回」「设为精华」
- 成员菜单锚点 ``avatar-span``：发送消息/TA/戳一戳/查看资料/…，
  管理员额外有 设为管理员/移出本群/屏蔽此人发言/「设置群内禁言」
- **只对视口内节点有效**：QQ 菜单是真实弹窗，节点滚出视口（extents.y<0）
  时 showContextMenu 唤出无响应 —— 定位后必须先确认在屏
- 唤出偶发失败：invoke 返回 True 但菜单不出现，重试是必要的
- 菜单项随成员 role 变化：群主/管理员没有「设置群内禁言」「移出本群」
"""

import time

from . import atspi
from .backfill import SCROLL_DOWN, SCROLL_UP, find_item_node, scroll_to_item, _scroller
from .send import SendError, activate_qq, open_conversation_by_uin

CTX_ITEM = "q-context-menu-item"
SCREEN_BOTTOM = 950  # 1920x1080 下消息区可见下沿的保守值


def _press_escape():
    import subprocess

    subprocess.run(["xdotool", "key", "Escape"], capture_output=True)


def _ctx_items(app):
    for n in atspi.walk(app):
        if CTX_ITEM in atspi.attr(n, "class"):
            label = atspi.node_name(n) or atspi.text_of(n) or ""
            if label:
                yield n, label


def _dismiss_dialogs(app, timeout=2.0):
    """收掉残留的 q-dialog 模态框——它悬着时菜单 hover/子菜单全被挡住。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dlg = _dialog(app)
        if dlg is None:
            return
        closed = False
        for n in atspi.walk(dlg):
            cls = atspi.attr(n, "class")
            nm = atspi.node_name(n) or ""
            if "q-dialog-close" in cls or nm in ("关闭", "取消"):
                if atspi.invoke(n, "press") or _click_item(n):
                    closed = True
                    break
        if not closed:
            _press_escape()
        time.sleep(0.3)


def _ctx_pick_now(app, names, exclude=()):
    for n, label in _ctx_items(app):
        if any(x in label for x in exclude):
            continue
        if any(k in label for k in names):
            return n
    return None


def _open_menu(app, node, names, timeout=2.5, exclude=()):
    """唤出一次右键菜单并等匹配项。先 a11y invoke，不落再换真右键。"""
    if atspi.invoke(node, "showContextMenu"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pick = _ctx_pick_now(app, names, exclude)
            if pick is not None:
                return pick
            time.sleep(0.15)
        _press_escape()
        time.sleep(0.2)
        return None
    # invoke 不可用/失败 → 真右键点击节点中心
    ext = atspi.extents(node)
    if not ext:
        return None
    import subprocess

    subprocess.run(
        ["xdotool", "mousemove",
         str(ext[0] + ext[2] // 2), str(ext[1] + ext[3] // 2), "click", "3"],
        capture_output=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pick = _ctx_pick_now(app, names, exclude)
        if pick is not None:
            return pick
        time.sleep(0.15)
    _press_escape()
    time.sleep(0.2)
    return None


def _show_ctx_menu(app, node, names, timeout=2.5, attempts=3, exclude=()):
    """唤出右键菜单并等一个匹配 ``names`` 的项；失败收菜单重试。

    showContextMenu 偶发「invoke 返回 True 但菜单没弹」——固定等待后没见到
    目标项就 Esc 收掉（可能没有菜单）再来，最多 ``attempts`` 次。
    ``exclude`` 命中的项直接跳过（防「解除禁言」误中「禁言」）。
    """
    for _ in range(attempts):
        pick = _open_menu(app, node, names, timeout=timeout, exclude=exclude)
        if pick is not None:
            return pick
        time.sleep(0.2)
    return None


_MIN_VISIBLE_PX = 24  # 节点下缘进入视口这么多就算可见（32px 头像露出大半）


def _in_viewport(node):
    ext = atspi.extents(node)
    # 部分可见即可：虚拟化让节点常停在视口上沿外几 px，y>=0 太严会误判屏外
    return bool(ext) and ext[1] + ext[3] > _MIN_VISIBLE_PX and ext[1] < SCREEN_BOTTOM


def _scroll_into_view(app, node_fn, max_steps=40):
    """Roll the message list until ``node_fn()`` yields a node inside the viewport.

    QQ materialises a buffer band beyond the visible list — find_item_node
    can hit a node whose extents sit off-screen (y<0), where showContextMenu
    silently no-ops. So a hit isn't enough: roll toward the node's side
    until it's actually rendered on screen.
    """
    for _ in range(max_steps):
        node = node_fn()
        if node is None:
            return None
        if _in_viewport(node):
            return node
        ext = atspi.extents(node)
        direction = SCROLL_UP if (ext and ext[1] < 0) else SCROLL_DOWN
        scroller = _scroller(app)
        if scroller is None or not atspi.invoke(scroller, direction):
            return None
        time.sleep(0.25)
    return None


def _wrapper_of(item):
    for n in atspi.walk(item):
        if "message-content__wrapper" in atspi.attr(n, "class"):
            return n
    return None


def _avatar_of(item):
    for n in atspi.walk(item):
        if "avatar-span" in atspi.attr(n, "class"):
            return n
    return None


def recall_message(app, conv_uin, msg_id, cache=None, verify_identity=True, timeout=4.0):
    """撤回一条消息：切会话 → 右键消息正文 → 菜单「撤回」。

    QQ 只给有权限的消息显示撤回项（自己的消息，或管理员对群成员消息）。
    """
    if not activate_qq(timeout=5):
        raise SendError("QQ 主窗口无法唤回（窗口处于托盘/最小化态）")
    _dismiss_dialogs(app)
    open_conversation_by_uin(
        app, str(conv_uin), cache=cache, verify=verify_identity
    )
    item = find_item_node(app, msg_id) or scroll_to_item(app, msg_id)
    if item is None:
        raise SendError(f"消息 {msg_id} 不在消息列表里（上滚下滚都找不到）")
    wrapper = _wrapper_of(item)
    if wrapper is None:
        raise SendError("消息节点上没有可调出的内容菜单")
    if not _in_viewport(wrapper):
        item = scroll_to_item(app, msg_id)
        wrapper = _wrapper_of(item) if item else None
        if wrapper is None or not _in_viewport(wrapper):
            raise SendError("消息无法滚入视口")
    pick = _show_ctx_menu(app, wrapper, ("撤回",))
    if pick is None:
        raise SendError("右键菜单没有「撤回」项（无权撤回该消息）")
    _click_item(pick)
    _confirm_dialog(app)
    return True


def _click_item(item):
    """Activate a context-menu item with a real left-click on its centre.

    invoke("select") 对 QML 菜单是假阳性高发区——返回 True 但 QQ 不执行
    （解禁实测就这样）。真左键按 extents 中心点击，菜单渲染时最可靠；
    节点取不到坐标才退回 invoke。
    """
    import subprocess

    ext = atspi.extents(item)
    if ext:
        subprocess.run(
            ["xdotool", "mousemove",
             str(ext[0] + ext[2] // 2), str(ext[1] + ext[3] // 2), "click", "1"],
            capture_output=True,
        )
        return True
    return atspi.invoke(item, "select")


def _confirm_dialog(app, timeout=1.2):
    """Best-effort: if a confirm dialog popped, press its affirmative button."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for n in atspi.walk(app):
            role = atspi.node_role(n)
            label = atspi.node_name(n) or ""
            if role in ("push_button", "button") and any(k in label for k in ("确定", "撤回", "确认")):
                atspi.invoke(n, "press")
                return True
        time.sleep(0.1)
    return False


def _locate_member_avatar(app, ids, conv_uin, user_uin, limit=6):
    """该成员最近一条消息的头像节点（须已滚入视口）。

    真实 uin 优先；库里只有合成 id 时回落成员显示名（avatar-span 的
    name 就是显示名），命中后再用名字复核头像防同名串号。
    """
    msg_ids = ids.recent_msg_ids_by_sender(conv_uin, user_uin, limit=limit)
    expect_name = None
    if not msg_ids:
        expect_name = ids.member_name(conv_uin, user_uin)
        if expect_name:
            msg_ids = ids.recent_msg_ids_by_sender_name(conv_uin, expect_name, limit=limit)
    if not msg_ids:
        raise SendError(f"成员 {user_uin} 在本服务窗口内没有记录过的消息，无法定位头像")

    def _avatar_for(mid):
        item = find_item_node(app, mid)
        return _avatar_of(item) if item is not None else None

    def _name_ok(avatar):
        return expect_name is None or (atspi.node_name(avatar) or "") == expect_name

    # 每个候选 msg_id：在树但屏外（虚拟化缓冲带）就朝它滚进视口
    for mid in msg_ids:
        avatar = _scroll_into_view(app, lambda mid=mid: _avatar_for(mid))
        if avatar is not None and _in_viewport(avatar) and _name_ok(avatar):
            return avatar

    # 缓冲带里都没有 → 大滚轮定位最近一条，再微调进视口
    item = scroll_to_item(app, msg_ids[0])
    if item is not None:
        avatar = _scroll_into_view(app, lambda: _avatar_of(item))
        if avatar is not None and _in_viewport(avatar) and _name_ok(avatar):
            return avatar
    raise SendError(f"成员 {user_uin} 的消息无法滚入视口")


def poke_member(app, conv_uin, user_uin, ids, cache=None, verify_identity=True, timeout=4.0):
    """戳一戳群成员：右键其头像 → 菜单「戳一戳」。"""
    if not activate_qq(timeout=5):
        raise SendError("QQ 主窗口无法唤回（窗口处于托盘/最小化态）")
    _dismiss_dialogs(app)
    open_conversation_by_uin(
        app, str(conv_uin), cache=cache, verify=verify_identity
    )
    avatar = _locate_member_avatar(app, ids, conv_uin, user_uin)
    pick = _show_ctx_menu(app, avatar, ("戳一戳",))
    if pick is None:
        raise SendError("头像右键菜单没有「戳一戳」项")
    _click_item(pick)
    return True


def duration_to_dhm(seconds):
    """禁言秒数 → (天, 时, 分)。QQ 的下拉只到分钟粒度，秒四舍五入到分。

    返回 (d, h, m)；duration<=0 表示解除禁言，返回 None。
    """
    seconds = int(seconds or 0)
    if seconds <= 0:
        return None
    total_min = seconds // 60 + (1 if seconds % 60 >= 30 else 0)
    if total_min == 0:
        total_min = 1   # 1-29 秒也至少给 1 分钟——QQ 没有秒粒度也没有 0
    d, rem = divmod(total_min, 1440)
    h, m = divmod(rem, 60)
    if d > 30:
        raise SendError("禁言时长超过 30 天上限")
    return d, h, m


def _mouse_xy():
    import subprocess

    out = subprocess.run(
        ["xdotool", "getmouselocation", "--shell"], capture_output=True, timeout=2
    ).stdout.decode()
    vals = dict(kv.split("=") for kv in out.split() if "=" in kv)
    try:
        return int(vals["X"]), int(vals["Y"])
    except (KeyError, ValueError):
        return 0, 0


def _hover_item(node, steps=6, settle=1.2):
    """Hover a menu item so QQ opens its submenu.

    QML 的 hover 依赖鼠标「滑入」轨迹——指针已经停在项上时轨迹为零，
    enter 事件不会触发。先移到项外侧再滑回，保证 hover 序列完整。
    """
    import subprocess

    ext = atspi.extents(node)
    if not ext:
        return False
    tx, ty = ext[0] + ext[2] // 2, ext[1] + ext[3] // 2
    sx, sy = _mouse_xy()
    # 指针已在目标附近 → 先挪出去制造「离开」，否则滑入轨迹为空
    if abs(sx - tx) < ext[2] and abs(sy - ty) < ext[3] + 30:
        sx, sy = tx - ext[2] - 60, ty
        subprocess.run(["xdotool", "mousemove", str(sx), str(sy)], capture_output=True)
        time.sleep(0.15)
    for i in range(1, steps + 1):
        subprocess.run(
            ["xdotool", "mousemove",
             str(sx + (tx - sx) * i // steps), str(sy + (ty - sy) * i // steps)],
            capture_output=True,
        )
        time.sleep(0.1)
    time.sleep(settle)
    return True


def _pick_menu_item(app, names, timeout=2.5):
    """Wait for a q-context-menu-item matching ``names`` to appear; return node."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for n, label in _ctx_items(app):
            if any(k in label for k in names):
                return n
        time.sleep(0.15)
    return None


# 「设置群内禁言」二级菜单的预设档位（秒 → 菜单项名），命中直选；
# 其余秒数走「自定义时长」对话框精确到天/时/分。
BAN_PRESETS = {600: "10分钟", 3600: "1小时", 43200: "12小时", 86400: "1天"}


def _dialog(app):
    """当前弹出的 q-dialog 节点。"""
    for n in atspi.walk(atspi.ui_root(app)):
        if "q-dialog" in atspi.attr(n, "class"):
            return n
    return None


def _pulldown_buttons(dlg):
    """对话框里的下拉按钮，DOM 序即 天/时/分钟。"""
    return [
        n for n in atspi.walk(dlg)
        if "q-pulldown-menu-button" in atspi.attr(n, "class")
    ]


def _set_pulldown(app, dlg, index, value, timeout=3.0):
    """点对话框第 ``index`` 个下拉，选数值 ``value``。

    面板是 q-pulldown-menu-panel，选项 check menu item 名即数字；
    列表高 ~190px 只容 8 项，目标在屏外时滚 q-pulldown-menu-list。
    """
    btns = _pulldown_buttons(dlg)
    if index >= len(btns):
        raise SendError("禁言对话框里少了一个时长下拉")
    # 真左键优先——invoke press 对 QML 按钮同样可能假阳性（返回 True 不真展开）
    _click_item(btns[index])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for n in atspi.walk(app):
            if "q-pulldown-menu-list-item" not in atspi.attr(n, "class"):
                continue
            if (atspi.node_name(n) or "").strip() != str(value):
                continue
            ext = atspi.extents(n)
            if ext and ext[1] + ext[3] > 0 and ext[1] < SCREEN_BOTTOM:
                _click_item(n)
                return True
            # 已渲染但屏外 → 滚列表把它带进来
            panel = atspi.parent(n)
            while panel is not None and "q-pulldown-menu-list" not in atspi.attr(panel, "class"):
                panel = atspi.parent(panel)
            if panel is not None and atspi.invoke(panel, "scrollForward"):
                break
        time.sleep(0.12)
    raise SendError(f"时长下拉没有 {value} 选项")


def ban_member(app, conv_uin, user_uin, seconds, ids, cache=None,
               verify_identity=True, timeout=6.0, deadline=None):
    """设置群成员禁言：右键头像 → 「设置群内禁言」→ 对话框设天/时/分 → 确定。

    seconds<=0 走解禁分支（被禁言成员的菜单项变为解除类文案）。
    ``deadline`` 是整个操作的总超时（monotonic），各步共享——单步卡住
    不会让整条链拖到各步 timeout 叠加的分钟级。
    """
    def _left(cap):
        return cap if deadline is None else max(0.1, min(cap, deadline - time.monotonic()))

    if not activate_qq(timeout=5):
        raise SendError("QQ 主窗口无法唤回（窗口处于托盘/最小化态）")
    _dismiss_dialogs(app)
    open_conversation_by_uin(
        app, str(conv_uin), cache=cache, verify=verify_identity
    )
    avatar = _locate_member_avatar(app, ids, conv_uin, user_uin)

    dhm = duration_to_dhm(seconds)
    if dhm is None:
        pick = _show_ctx_menu(app, avatar, ("解除禁言", "取消禁言", "解禁"),
                              timeout=_left(2.5))
        if pick is None:
            raise SendError("右键菜单没有解除禁言项（该成员可能未被禁言）")
        _click_item(pick)
        _confirm_dialog(app)
        return True

    pick = _show_ctx_menu(app, avatar, ("设置群内禁言",), exclude=("解除", "取消"),
                          timeout=_left(2.5))
    if pick is None:
        raise SendError("右键菜单没有「设置群内禁言」项（需要管理员权限，或该成员是群主/管理员）")

    # 「设置群内禁言」是二级菜单——hover 展开而不是 select（select 可能直触默认档）
    _hover_item(pick, settle=min(1.2, _left(1.2)))
    preset = BAN_PRESETS.get(int(seconds))
    if preset is not None:
        item = _pick_menu_item(app, (preset,), timeout=_left(2.5))
        if item is None:
            _press_escape()
            raise SendError(f"禁言子菜单没有「{preset}」项")
        _click_item(item)
        return True
    custom = _pick_menu_item(app, ("自定义时长", "自定义"), timeout=_left(2.5))
    if custom is None:
        _press_escape()
        raise SendError("禁言子菜单没有「自定义时长」项")
    _click_item(custom)

    d, h, m = dhm
    wait_until = time.monotonic() + _left(timeout)
    dlg = None
    while time.monotonic() < wait_until and dlg is None:
        dlg = _dialog(app)
        if dlg is None:
            time.sleep(0.1)
    if dlg is None:
        raise SendError("「自定义时长」对话框没有弹出")
    _set_pulldown(app, dlg, 0, d, timeout=_left(timeout))
    _set_pulldown(app, dlg, 1, h, timeout=_left(timeout))
    _set_pulldown(app, dlg, 2, m, timeout=_left(timeout))

    wait_until = time.monotonic() + _left(timeout)
    while time.monotonic() < wait_until:
        for n in atspi.walk(dlg):
            if atspi.node_role(n) in ("push_button", "button") and "确定" in (atspi.node_name(n) or ""):
                _click_item(n)
                return True
        time.sleep(0.1)
    raise SendError("禁言对话框里没有「确定」按钮")
