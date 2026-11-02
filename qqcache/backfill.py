"""Scroll-up recovery for messages that left the virtualised viewport.

QQ's message list only materialises a sliding window of ml-item nodes. When a
burst pushes messages out of the window between two polls, ``Cursor.filter_new``
reports ``gap`` — the newest visible item was never seen, so an unknown number
of messages were skipped.

Recovery scrolls the list upward one step at a time. Each scroll makes the
virtual list materialise the next chunk of older history (QQ lazy-loads it from
the local store), which is parsed and committed. The walk stops when a frame
contains a msg_id already in the cursor's seen set — that proves the recovered
chain is contiguous with previously committed history.

Everything operates through AT-SPI scroll actions on the ``ml-root`` scroll
view. The window must be mapped: iconified windows keep their last DOM but the
scroller stops producing new materialisation.
"""

import time

from . import atspi
from .messages import parse_messages

SCROLL_UP = "scrollUp"
SCROLL_DOWN = "scrollForward"


def message_list(app):
    return atspi.find_one(atspi.ui_root(app), role="embedded", name="消息列表")


def _scroller(app):
    return atspi.find_by_class(atspi.ui_root(app), "ml-root")


def _frame(app):
    """Messages in the currently materialised window, DOM order."""
    ml = message_list(app)
    if ml is None:
        return []
    return parse_messages(atspi.dump_tree(ml))


def _ids(messages):
    return {m.msg_id for m in messages}


def _scrollbar(app):
    """(thumb_y, thumb_bottom, track_top, track_bottom) of the message list's
    custom v-scrollbar, or None."""
    ml = message_list(app)
    if ml is None:
        return None
    thumb = track = None
    for n in atspi.walk(ml):
        cls = atspi.attr(n, "class")
        if "v-scrollbar-thumb" in cls:
            thumb = atspi.extents(n)
        elif "v-scrollbar-track" in cls:
            track = atspi.extents(n)
    if thumb is None or track is None:
        return None
    return thumb[1], thumb[1] + thumb[3], track[1], track[1] + track[3]


def _position(app):
    """Motion signal: (top msg_id, thumb_y). The materialised buffer can be
    several viewports deep, so a scroll that doesn't change the id set still
    shows up here as thumb movement — a true stall means neither moved."""
    msgs = _frame(app)
    thumb = _scrollbar(app)
    return msgs, (msgs[0].msg_id if msgs else None, thumb[0] if thumb else None)


def _wait_position_change(app, previous, timeout=2.0, interval=0.1):
    deadline = time.monotonic() + timeout
    msgs, pos = _position(app)
    while pos == previous and time.monotonic() < deadline:
        time.sleep(interval)
        msgs, pos = _position(app)
    return msgs, pos


def merge_recovered(recovered, messages, seen, emitted):
    """Prepend the frame's previously-unseen items, keeping DOM order.

    Each scrolled-up frame is strictly older than what ``recovered`` already
    holds (any overlap sits at the frame's bottom and is filtered out via
    ``emitted``), so its fresh items go in front.
    """
    fresh = [
        m for m in messages if m.msg_id not in emitted and m.msg_id not in seen
    ]
    emitted.update(m.msg_id for m in fresh)
    return fresh + recovered


def backfill(app, cursor, uin, anchor=None, max_steps=80, stall_limit=3,
             budget=None):
    """Scroll up and commit frames until reaching ``anchor`` ids.

    ``anchor`` is the set of msg_ids considered "already committed" — normally
    ``cursor.seen(uin)`` snapshotted *before* the current poll's frame was
    committed. Reaching any anchor id proves contiguity with earlier history.

    ``budget`` caps the wall-clock time (seconds) the walk may hold the GUI —
    a deep history walk otherwise monopolises the window for minutes while
    other conversations' notifications queue behind it.

    Returns a dict: ``stitched`` (anchor reached), ``steps``, ``chain_broken``
    (a frame shared no id with its predecessor — a sub-gap may remain), and
    ``recovered`` (newly committed messages, oldest first).
    """
    if anchor is None:
        anchor = cursor.seen(uin)

    deadline = time.monotonic() + budget if budget else None
    emitted = set()
    recovered = []
    msgs, prev_pos = _position(app)
    prev_ids = _ids(msgs)
    stitched = bool(prev_ids & anchor)
    chain_broken = False
    steps = stall = 0

    while (not stitched and steps < max_steps and stall < stall_limit
           and (deadline is None or time.monotonic() < deadline)):
        scroller = _scroller(app)
        if scroller is None or not atspi.invoke(scroller, SCROLL_UP):
            break
        steps += 1
        msgs, pos = _wait_position_change(app, prev_pos)
        ids = _ids(msgs)
        if pos == prev_pos:
            stall += 1
            continue
        stall = 0
        if prev_ids and not (ids & prev_ids):
            chain_broken = True
        cursor.commit(uin, msgs)
        recovered = merge_recovered(recovered, msgs, anchor, emitted)
        if ids & anchor:
            stitched = True
            break
        prev_ids, prev_pos = ids, pos

    return {
        "stitched": stitched,
        "steps": steps,
        "chain_broken": chain_broken,
        "recovered": recovered,
    }


def find_item_node(app, msg_id):
    """The materialised ``ml-item`` accessible for ``msg_id``, or None."""
    ml = message_list(app)
    if ml is None:
        return None
    for n in atspi.walk(ml):
        if "ml-item" in atspi.attr(n, "class") and atspi.attr(n, "id") == str(msg_id):
            return n
    return None


def scroll_to_item(app, msg_id, max_steps=120, stall_limit=4):
    """Scroll until the ``ml-item`` for ``msg_id`` is materialised; return it.

    Searches upward first (the usual case for replying to a scrolled-out
    message), then downward — a parked list may sit above the target.
    """
    node = find_item_node(app, msg_id)
    if node is not None:
        return node
    for direction, at_edge in ((SCROLL_UP, "top"), (SCROLL_DOWN, "bottom")):
        _, prev_pos = _position(app)
        stall = 0
        for _ in range(max_steps):
            bar = _scrollbar(app)
            if bar is not None:
                if at_edge == "top" and bar[0] - bar[2] <= 6:
                    break
                if at_edge == "bottom" and bar[3] - bar[1] <= 6:
                    break
            scroller = _scroller(app)
            if scroller is None or not atspi.invoke(scroller, direction):
                break
            _, pos = _wait_position_change(app, prev_pos)
            node = find_item_node(app, msg_id)
            if node is not None:
                return node
            if pos == prev_pos:
                stall += 1
                if stall >= stall_limit:
                    break
            else:
                stall = 0
            prev_pos = pos
    return None


def scroll_to_bottom(app, max_steps=300, stall_limit=4, edge_px=6,
                     budget=None):
    """Scroll forward until the scrollbar thumb reaches the track's bottom —
    the live tail. Falls back to a motion stall when no scrollbar exists.

    ``budget`` caps wall-clock seconds so a deep-history walk home can't hold
    the GUI indefinitely.

    Returns the final frame's messages (which may contain messages that
    arrived while the list was parked in history).
    """
    deadline = time.monotonic() + budget if budget else None
    msgs, prev_pos = _position(app)
    steps = stall = 0
    while (steps < max_steps and stall < stall_limit
           and (deadline is None or time.monotonic() < deadline)):
        bar = _scrollbar(app)
        if bar is not None and bar[3] - bar[1] <= edge_px:
            break
        scroller = _scroller(app)
        if scroller is None or not atspi.invoke(scroller, SCROLL_DOWN):
            break
        steps += 1
        msgs, pos = _wait_position_change(app, prev_pos)
        if pos == prev_pos:
            stall += 1
        else:
            stall = 0
        prev_pos = pos
    return msgs
