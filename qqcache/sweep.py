"""Unread sweep: catch conversations whose notification QQ never sent.

QQ stays silent for other conversations while its window has keyboard focus,
and nobody holds the notification bus while serve restarts — messages that
arrive in those windows produce no Notify at all. The conversation list still
shows them (row time, preview, unread badge), so after every such blind
window we read the top rows (read-only, no clicks) and treat a missed row as
if it had notified.

Only conversations that have notified before qualify: groups set to "receive
but don't alert" pile up 99+ unread and must not be opened just for that.
"""

import re
import time
from dataclasses import dataclass

from loguru import logger

from . import atspi

ROW_CLASS = "recent-contact-item"
PINNED_CLASS = "recent-contact-item--top"
_UNREAD_RE = re.compile(r"^(\d+)条未读$")
# 行内时间的几种显示形态；名字里带 emoji 时会被拆成多个文字节点，
# 只能靠「第一个像时间的节点」来切分名字和预览
_TIME_RE = re.compile(
    r"^((昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*(\d{1,2}:\d{2})?"
    r"|\d{1,2}:\d{2}|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}|\d{4}-\d{1,2}-\d{1,2})$")


@dataclass
class Row:
    name: str
    time: str = ""
    preview: str = ""
    unread: int = 0
    muted: bool = False
    pinned: bool = False

    @property
    def signature(self):
        return (self.time, self.preview, self.unread)


def parse_row(nodes):
    """One conversation row from its nodes in DFS order: [(role, name, class)].

    Layout (QQ 3.2.33): static <会话名> > [static <时间>, static <发送者：>?,
    static <预览>]; section 'N条未读' (badge); section '免打扰' (icon)."""
    texts = []
    unread = 0
    muted = False
    badge = False   # 角标之后的文字是角标自己的数字（"1"、"99+"），不是预览
    for role, name, cls in nodes:
        name = name or ""
        m = _UNREAD_RE.match(name)
        if m:
            unread = int(m.group(1))
            badge = True
        elif name == "免打扰" or "no-disturb-icon" in (cls or ""):
            muted = True
        elif role == "static" and name and not badge:
            texts.append(name)
    if not texts:
        return None
    cut = next((i for i, t in enumerate(texts) if i > 0 and _TIME_RE.match(t)), None)
    if cut is None:
        # 认不出时间就不猜名字边界：整行当名字，宁可巡检漏补也不错配
        return Row(name="".join(texts), muted=muted, unread=unread)
    return Row(name="".join(texts[:cut]), time=texts[cut],
               preview="".join(texts[cut + 1:]), unread=unread, muted=muted)


def _first_row_name(app):
    from .send import conversation_items

    return next((n for n, _ in conversation_items(app)), None)


def row_time(text, now=None):
    """行内时间 -> epoch 秒（只认今天/昨天的「HH:MM」；更早的形态返回 0）。"""
    import datetime

    now = now or time.time()
    m = re.match(r"^(昨天)?\s*(\d{1,2}):(\d{2})$", text or "")
    if not m:
        return 0.0
    day = datetime.date.fromtimestamp(now)
    if m.group(1):
        day -= datetime.timedelta(days=1)
    dt = datetime.datetime.combine(day, datetime.time(int(m.group(2)), int(m.group(3))))
    return dt.timestamp()


def _item_row(item):
    nodes = [(atspi.node_role(n), atspi.node_name(n), atspi.attr(n, "class"))
             for n in atspi.walk(item, max_depth=8)]
    row = parse_row(nodes)
    if row is not None:
        row.pinned = atspi.has_class(item, PINNED_CLASS)
    return row


def _visible(lst):
    out = []
    for item in atspi.children(lst):
        if not atspi.has_class(item, ROW_CLASS):
            continue
        row = _item_row(item)
        if row is not None:
            out.append(row)
    return out


def _step(app, lst, action):
    """AT-SPI 滚一步（约 7 行、0.5s）；返回是否真的动了。不动鼠标。"""
    before = _first_row_name(app)
    if not atspi.invoke(lst, action):
        return False
    time.sleep(0.3)
    return _first_row_name(app) != before


def read_rows(app, since=None, regular=20, max_steps=10):
    """读会话列表里「置顶区之后最新的那段」普通会话。

    普通会话按时间排序，新消息总是冒到置顶区下面第一行。所以让列表停在
    置顶/普通的交界处：交界在视野里就直接读（稳态零滚动、~0.2s）；整屏都是
    置顶（置顶多，实测 30 个）就往下翻到交界；整屏都是普通会话（被侧栏
    搜索滚到了下面）就往上翻回交界或顶部。只用 AT-SPI 滚动，不动鼠标；
    读完不回滚——停在交界正好是下次巡检的起点。

    交界之后可见的普通会话不够 ``regular`` 个、又还没碰到比 ``since`` 更早
    的行时，再往下翻几步补读。"""
    from .send import conversation_list

    lst = conversation_list(app)
    rows = _visible(lst)
    steps = 0
    # 整屏置顶：往下找交界
    while rows and all(r.pinned for r in rows) and steps < max_steps:
        if not _step(app, lst, "scrollForward"):
            break
        steps += 1
        rows = _visible(lst)
    # 整屏普通会话：可能在交界下面，往上翻到看见置顶或到顶
    while rows and not any(r.pinned for r in rows) and steps < max_steps:
        if not _step(app, lst, "scrollBackward"):
            break
        steps += 1
        rows = _visible(lst)

    # 交界之后补读：启动/重新登录（since=None）读满 regular 个；平时盲区
    # 只有几秒，漏掉的会话就在交界下面，最多多翻一步。补读后翻回交界，
    # 下次巡检还是零滚动
    extra_budget = max_steps if since is None else 1
    extra = 0
    out, seen, n_regular = [], set(), 0
    while True:
        done = False
        for row in rows:
            key = (row.name, row.time, row.pinned)
            if key in seen:
                continue
            seen.add(key)
            if not row.pinned:
                if since is not None and row_time(row.time) < since:
                    done = True
                    break
                n_regular += 1
            out.append(row)
            if n_regular >= regular:
                done = True
                break
        if done or extra >= extra_budget or not _step(app, lst, "scrollForward"):
            break
        extra += 1
        rows = _visible(lst)
    for _ in range(extra):
        _step(app, lst, "scrollBackward")
    return out


class Sweeper:
    """Decides which rows were missed. Remembers, per conversation, the row
    signature it last acted on — a row that stays unread (open failed, user
    hasn't read it) is not re-triggered until something new arrives."""

    def __init__(self):
        self._acted = {}   # name -> signature

    def pick(self, rows, notifying, current=None):
        out = []
        for row in rows:
            if row.muted or row.unread <= 0 or row.name == current:
                continue
            if row.name not in notifying:
                continue
            if self._acted.get(row.name) == row.signature:
                continue
            self._acted[row.name] = row.signature
            out.append(row)
        return out

    def forget(self, name):
        """补处理失败：下次巡检时即使该行没有变化也重新补。"""
        self._acted.pop(name, None)


def sweep(app, sweeper, notifying, current=None, since=None):
    """漏掉通知的行；读不到会话列表时返回 None。"""
    try:
        rows = read_rows(app, since=since)
    except Exception as exc:  # noqa: BLE001 - 巡检是兜底，失败不影响主流程
        logger.debug(f"未读巡检读列表失败: {exc}")
        return None
    return sweeper.pick(rows, notifying, current=current)
