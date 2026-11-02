"""Resolve the numeric identity of the currently open conversation.

Neither the conversation list nor the notification payload exposes the peer
uin, but the profile card behind the chat header does: pressing the header
button (``chat-header__contact-name``) opens a 资料卡 window whose static
nodes contain the group number (群) or QQ number (好友).

    uin, kind = uin_of(app)   # ("100000001", "group")

The card is closed again before returning; opening a second card while one is
already up replaces it, so callers do not need to pre-clean.
"""

import re
import shutil
import subprocess
import time
from dataclasses import dataclass

from . import atspi, clipboard

CARD_NAME = "资料卡"
HEADER_CLASS = "chat-header__contact-name"
UIN_RE = re.compile(r"\d{5,12}")
# "QQ 3456789012" 同节点内嵌形式——机器人/部分账号卡的布局不同，
# QQ 标签和号码不拆成两个 static。
UIN_LABELED_RE = re.compile(r"(?:QQ|Q\s?号|QQ号)\s*[:：]?\s*(\d{5,12})")
GROUP_LABELS = ("群号", "群介绍", "群公告", "群分类", "群聊")
GROUP_MEMBER_RE = re.compile(r"[（(]\s*\d+\s*人[）)]")
C2C_LABELS = ("QQ号", "Q 号", "Q号")


@dataclass
class Identity:
    uin: str | None
    kind: str = "unknown"
    texts: tuple = ()


class PeerError(RuntimeError):
    pass


def header_button(app):
    return atspi.find_by_class(atspi.ui_root(app), HEADER_CLASS)


def _find_cards(app):
    """All card-shaped nodes — stale ghosts can coexist with the live one."""
    # 注意：资料卡是独立顶层 frame，不在主窗 frame 的子树里，不能按 ui_root 限定
    return [
        n
        for n in atspi.walk(app)
        if atspi.node_name(n) == CARD_NAME
        and atspi.node_role(n) in ("frame", "dialog", "panel", "window")
    ]


def find_card(app):
    cards = _find_cards(app)
    return cards[0] if cards else None


def card_texts(card):
    if card is None:
        return []
    return [atspi.node_name(n).strip() for n in atspi.walk(card) if atspi.node_role(n) == "static"]


def extract_identity(texts):
    """Pick the uin and the peer kind out of the card's static texts.

    Label-bearing lines (群号：xxx / QQ号：xxx) win over bare digit strings:
    cards also carry level numbers, member counts and other digits.
    """
    bare_idx = [
        (i, t.strip())
        for i, t in enumerate(texts)
        if UIN_RE.fullmatch(t.strip())
    ]
    # 群卡结构: <群号> (N人) ... —— 号码紧邻成员数之前
    for i, (idx, digits) in enumerate(bare_idx):
        for t in texts[idx + 1 : idx + 3]:
            if GROUP_MEMBER_RE.search(t):
                return digits, "group"
    # 私聊卡的内嵌形式: "QQ 3456789012" 全在一个节点里
    for t in texts:
        m = UIN_LABELED_RE.search(t)
        if m:
            return m.group(1), "c2c"
    # 私聊卡结构: ... QQ 222222222 ... —— 号码在标签之后
    for i, t in enumerate(texts):
        if t.strip() in ("QQ", "QQ号"):
            for idx, digits in bare_idx:
                if idx > i:
                    return digits, "c2c"
            return None, "c2c"
    labeled_uin = None
    labeled_kind = None
    for text in texts:
        digits = UIN_RE.findall(text)
        for label, kind in [(l, "group") for l in GROUP_LABELS] + [(l, "c2c") for l in C2C_LABELS]:
            if label in text:
                labeled_kind = labeled_kind or kind
                if digits:
                    labeled_uin = labeled_uin or digits[-1]
    uin = labeled_uin or (bare_idx[-1][1] if bare_idx else None)
    return uin, labeled_kind or "unknown"


def _wait_card(app, timeout, prev_texts=None):
    """Wait for a card node carrying fresh content.

    Dead 资料卡 nodes keep their old texts forever, and a press on a different
    chat's header spawns/refreshes a *different* node. So poll every
    card-named node and take the first whose texts are non-empty and differ
    from the pre-press snapshot; only at the deadline do we accept an
    unchanged node (re-pressing the same chat's card). A stale read can still
    slip through there, but callers verify the uin anyway.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for card in _find_cards(app):
            texts = card_texts(card)
            if texts and texts != prev_texts:
                return card
        time.sleep(0.1)
    if prev_texts is None:
        return None
    for card in _find_cards(app):
        if card_texts(card):
            return card
    return None


def _card_windows():
    """Standalone card windows; in the normal case the card is an in-window
    overlay with no toplevel of its own, so this is usually empty."""
    if not shutil.which("xdotool"):
        return []
    try:
        out = subprocess.run(
            ["xdotool", "search", "--name", CARD_NAME], capture_output=True, timeout=2
        ).stdout
        return [w.decode() for w in out.split()]
    except (subprocess.SubprocessError, OSError):
        return []


def _qq_windows():
    if not shutil.which("xdotool"):
        return []
    try:
        out = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "qq"],
            capture_output=True,
            timeout=2,
        ).stdout
        return [w.decode() for w in out.split()]
    except (subprocess.SubprocessError, OSError):
        return []


def close_card(app, timeout=1.5, click_xy=None):
    """Best-effort dismissal of the profile card.

    The card has no close button and no reliable "is open" signal (its a11y
    node is persistent), so this just fires every plausible dismissal: a close
    button if one exists, a click outside the card, and WM_DELETE on
    standalone card windows. ``click_xy`` overrides the default click point
    (left of the card) — callers use it to aim at a spot that cannot switch
    the open conversation.
    """
    card = find_card(app)
    if card is None and not _card_windows():
        return True
    if card is not None:
        for n in atspi.walk(card, max_depth=8):
            if atspi.node_role(n) == "button" and atspi.node_name(n) in ("关闭", "×", "X"):
                if atspi.invoke(n, "press"):
                    return True
    if click_xy is not None:
        x, y = click_xy
    else:
        # 点在消息列表左上内边距：卡左边/右边那些「看起来空」的位置其实
        # 可能落在表情按钮/工具栏上（点出表情面板的事故就是这么来的），
        # 列表容器边缘的 padding 才是真空白。
        ml = atspi.find_one(atspi.ui_root(app), role="embedded", name="消息列表")
        mext = atspi.extents(ml) if ml is not None else None
        if mext:
            x, y = mext[0] + 8, mext[1] + 8
        else:
            ext = atspi.extents(card) if card is not None else None
            x, y = (ext[0] - 30, ext[1] + ext[3] // 2) if ext else (None, None)
    if x is not None:
        # 卡在主窗内做遮罩：点它旁边的空白处收掉。绝不发 Escape——
        # 主窗收到 Escape 会直接收进托盘。
        try:
            subprocess.run(
                ["xdotool", "mousemove", str(x), str(y), "click", "1"],
                capture_output=True,
                timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            pass
    for wid in _card_windows():
        try:
            subprocess.run(["xdotool", "windowclose", wid], capture_output=True, timeout=2)
        except (subprocess.SubprocessError, OSError):
            pass
    return True


def _name_base(s):
    """Display name minus '（成员数）'/' (N人)' decorations and truncation dots."""
    return re.split(r"[（(…]", s)[0].rstrip(" .…")


def _sidebar_item_for(app, name):
    """The 会话列表 row belonging to the open chat. The selected row wins;
    name matching tolerates the sidebar's truncation."""
    lst = atspi.find_one(atspi.ui_root(app), role="embedded", name="会话列表")
    if lst is None:
        return None
    base = _name_base(name)
    matches = []
    for item in atspi.children(lst):
        label = None
        for n in atspi.walk(item, max_depth=6):
            if atspi.node_role(n) == "static":
                label = atspi.node_name(n)
                break
        if not label:
            continue
        lb = _name_base(label)
        if lb and (lb == base or base.startswith(lb) or lb.startswith(base)):
            matches.append(item)
    for it in matches:
        if "selected" in atspi.attr(it, "class") or "active" in atspi.attr(it, "class"):
            return it
    # 多个同名行又没有选中态：分不出哪行是打开的会话，宁可交给调用方走
    # 资料卡（读的是当前会话本身），也不拿第一行去复制别的群的号
    return matches[0] if len(matches) == 1 else None


def _uin_via_menu(app):
    """Right-click the open chat's sidebar row -> 复制群号/复制QQ号 -> clipboard.

    Same mechanism sync uses; the strongest identity proof available — it
    never touches the flaky card/panel UI.
    """
    # ``from . import sync`` resolves to the re-exported *function* on the
    # package, so import the member directly.
    from .sync import copy_uin

    header = header_button(app)
    name = atspi.node_name(header) if header is not None else ""
    item = _sidebar_item_for(app, name) if name else None
    if item is None:
        return None, None
    with clipboard.preserved():
        return copy_uin(app, item)


def _card_has_name(texts, name):
    """Card static texts carry the member's display name — the disambiguator
    that keeps stale cards of OTHER members from being misattributed."""
    name = (name or "").strip().rstrip("：:")
    if not name:
        return False
    for t in texts:
        t = (t or "").strip()
        if t and (name in t or t in name):
            return True
    return False


def _card_text_set(app):
    """全部资料卡文本的快照——点击前的基线，用来判一张卡是不是新开的。"""
    return {tuple(card_texts(c)) for c in _find_cards(app)}


def _wait_member_card(app, name, timeout, prev_all=None):
    """Wait for a card attributable to ``name``.

    两个锚点任一命中即收：
    - 卡文本含显示名——同名成员的僵尸卡里的 uin 本来就是对的，新旧无所谓；
    - 卡文本整体全新（不在点击前快照 prev_all 里）且带号码——只可能是
      这次点击刚开的那张。兜住 群名片≠QQ昵称 的情形：卡里显示的是 QQ
      昵称，名字锚点在这种成员身上永远等不到，会白掉进成员列表兜底。
    """
    def acceptable(c):
        texts = card_texts(c)
        if not texts:
            return False
        if _card_has_name(texts, name):
            return True
        return (
            prev_all is not None
            and tuple(texts) not in prev_all
            and any(UIN_RE.search(t) for t in texts)
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for c in _find_cards(app):
            if acceptable(c):
                return c
        time.sleep(0.1)
    for c in _find_cards(app):
        if acceptable(c):
            return c
    return None


def _card_group_nick(texts):
    """'群昵称' field of a member card — the authoritative attribution:
    which group member this card belongs to, regardless of what QQ nickname
    the card title displays."""
    for i, t in enumerate(texts):
        if (t or "").strip() == "群昵称" and i + 1 < len(texts):
            return (texts[i + 1] or "").strip() or None
    return None


def harvest_member_cards(app, names):
    """Resolve any pending member whose card happens to be open.

    成员卡可能比等待窗晚到（僵尸卡吞掉第一次点击、QQ 迟开才填文本），
    但卡上自带 '群昵称' 字段——不管是哪次点击开的，按字段归因白捡真号。
    成员无群名片时该字段缺失，退而按整卡文本精确匹配显示名。
    Returns {name: uin}.
    """
    wanted = {str(n) for n in names}
    found = {}
    for card in _find_cards(app):
        texts = card_texts(card)
        uin, _kind = extract_identity(texts)
        if not uin:
            continue
        gnick = _card_group_nick(texts)
        bare = {t.strip() for t in texts if t and t.strip()}
        for name in wanted - set(found):
            if name == gnick or (gnick is None and name in bare):
                found[name] = uin
    return found


def member_uin(app, item_node, timeout=3.0):
    """uin of a group-message sender via its avatar's profile card.

    The ``avatar-span`` inside an ``ml-item`` opens the same 资料卡 layout as
    the conversation header card; ``extract_identity`` reads the ``QQ <uin>``
    line off it.
    """
    avatar = None
    for n in atspi.walk(item_node, max_depth=5):
        if "avatar-span" in atspi.attr(n, "class"):
            avatar = n
            break
    if avatar is None:
        raise PeerError("消息节点里没有头像")
    sender = atspi.node_name(avatar)
    prev_all = _card_text_set(app)
    # QML invoke click 有假阳性：真左键优先，没 extents 才退 invoke
    ext = atspi.extents(avatar)
    # 滚出视口的虚拟化节点 extents 是负坐标——坐标点击打空气，直接交给调用方兜底
    if ext is not None and (ext[0] < 0 or ext[1] < 0):
        raise PeerError("头像在视口外（虚拟化未渲染）")
    if ext is not None:
        try:
            subprocess.run(
                ["xdotool", "mousemove", str(ext[0] + ext[2] // 2),
                 str(ext[1] + ext[3] // 2), "click", "1"],
                capture_output=True, timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            pass
    elif not atspi.invoke(avatar, "click"):
        raise PeerError("点击头像失败")
    card = _wait_member_card(app, sender, timeout, prev_all=prev_all)
    try:
        if card is None:
            raise PeerError("成员资料卡没有出现")
        uin, _kind = extract_identity(card_texts(card))
        if not uin:
            raise PeerError("成员资料卡未解析出 uin")
        return uin
    finally:
        # 成员卡实测不吃任何常规收卡动作（空白点击/输入框/滚动都无效），
        # 侧栏点击会折叠会话，(0,0) 点桌面是安全空操作——收不掉就留着，
        # overlay 遮挡不影响 DOM 级 invoke，下次靠卡内名字区分
        close_card(app, click_xy=(0, 0))


def uin_of(app, timeout=3.0):
    """Identity of the currently open chat.

    Preferred: the sidebar context-menu copy — same mechanism sync proves
    itself on, no overlay UI involved. Fallback: the floating 资料卡 behind
    the chat header (its layout is inconsistent across group kinds).
    Returns Identity; uin is None when nothing exposed a number.
    """
    header = header_button(app)
    if header is None:
        raise PeerError("当前没有打开的会话")
    uin, kind = _uin_via_menu(app)
    if uin:
        return Identity(uin=uin, kind=kind, texts=("menu",))
    expected = (atspi.node_name(header) or "").strip()
    # 没有名字锚点的卡一律不能收：会话刚切换时 header 可能短暂读空，
    # 此时 _wait_card 不设防会把别会话僵尸卡的 uin 记到本会话名下
    # （files 被记成私聊 uin 的中毒事故就是这么来的）
    if not expected:
        raise PeerError("会话名读不出，无法验证资料卡归属")
    prev_all = _card_text_set(app)
    close_card(app)  # 上次可能留了卡（或 a11y 死节点），先清场
    if not atspi.invoke(header, "press"):
        raise PeerError("点击会话标题失败")
    card = _wait_member_card(app, expected, timeout, prev_all=prev_all)
    try:
        if card is None:
            raise PeerError("资料卡没有出现")
        uin, kind = extract_identity(card_texts(card))
        return Identity(uin=uin, kind=kind, texts=tuple(card_texts(card)))
    finally:
        close_card(app)


# -- member panel: enumerate rows & resolve uin without a message anchor ------

MEMBER_LIST_NAME = "群成员列表"


def _member_rows(app):
    """(scroll_view, [(row_node, display_name)]) from the member panel."""
    lst = atspi.find_one(atspi.ui_root(app), role="embedded", name=MEMBER_LIST_NAME)
    if lst is None:
        return None, []
    pairs = []
    for n in atspi.walk(lst):
        if "group-user" not in atspi.attr(n, "class"):
            continue
        label = None
        for c in atspi.walk(n, max_depth=6):
            if atspi.node_role(c) == "static" and (atspi.node_name(c) or "").strip():
                label = atspi.node_name(c).strip()
                break
        pairs.append((n, label))
    return lst, pairs


def find_member_row(app, name, max_steps=60, stall_limit=3):
    """Locate the ``group-user`` row for ``name``, scrolling the virtualised
    member list until it materialises. Returns (row, match_count) — a count
    above 1 means the display name is ambiguous inside this group."""
    lst, pairs = _member_rows(app)
    if lst is None:
        raise PeerError("群成员列表面板不可见")
    seen = set()
    stall = 0
    for _ in range(max_steps):
        matches = [r for r, label in pairs if label == name]
        if matches:
            return matches[0], len(matches)
        new = {label for _, label in pairs if label}
        if new <= seen:
            stall += 1
            if stall >= stall_limit:
                break
        else:
            stall = 0
            seen |= new
        if not atspi.invoke(lst, "scrollForward"):
            break
        time.sleep(0.35)
        lst, pairs = _member_rows(app)
        if lst is None:
            raise PeerError("群成员列表面板不可见")
    raise PeerError(f"成员列表里找不到 {name!r}")


def _wait_ctx_item(app, names, timeout=2.5):
    """First visible q-context-menu item whose name is in ``names``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        menu = atspi.find_by_class(app, "q-context-menu")
        if menu is not None:
            for n in atspi.walk(menu):
                if atspi.node_name(n) in names and "item" in atspi.attr(n, "class"):
                    return n
        time.sleep(0.15)
    return None


def member_uin_via_list(app, name, timeout=4.0):
    """Resolve a member's real uin through the member panel — no message needed.

    成员行 showContextMenu → 「发送消息」→ 私聊 → 侧栏右键复制 QQ 号
    （uin_of 的最可靠路径）。本版 Linux QQ 里 xdotool 右键唤不出成员
    菜单，「查看资料」的 a11y select 又是空壳不产卡；「发送消息」是
    真实跳转。全链 DOM 级，不吃坐标/窗口映射状态。
    返回后当前会话停在跟该成员的私聊上——调用方须自行切回群。
    """
    row, count = find_member_row(app, name)
    if count > 1:
        raise PeerError(f"成员列表里有 {count} 个 {name!r}，无法唯一归因")
    if not atspi.invoke(row, "showContextMenu"):
        raise PeerError("成员行上下文菜单唤不出")
    item = _wait_ctx_item(app, ("发送消息",), timeout=2.5)
    if item is None:
        raise PeerError("成员菜单没有「发送消息」项")
    header = header_button(app)
    old = (atspi.node_name(header) or "").strip() if header is not None else ""
    if not atspi.invoke(item, "select"):
        raise PeerError("成员菜单「发送消息」激活失败")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        header = header_button(app)
        now = (atspi.node_name(header) or "").strip() if header is not None else ""
        if now and now != old:
            break
        time.sleep(0.2)
    else:
        raise PeerError("私聊会话没有打开")
    ident = uin_of(app)
    if not ident.uin:
        raise PeerError("私聊会话没有解析出 uin")
    return ident.uin
