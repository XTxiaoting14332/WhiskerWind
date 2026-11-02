"""Warm up the name -> uin cache by asking QQ itself.

For every conversation in the sidebar we open the context menu and pick
"复制群号" (groups) or "复制QQ号" (private chats). QQ writes the uin to the
clipboard, which is the only place the identifier is exposed: neither the
accessibility tree nor the D-Bus notifications carry it.
"""

import shutil
import subprocess
import time

from loguru import logger

from . import atspi, clipboard
from .store import is_uin

CONVERSATION_LIST = "会话列表"
COPY_LABELS = {"复制群号": "group", "复制QQ号": "c2c"}
SENTINEL = "__qqcache__"


class ScrapeError(RuntimeError):
    pass


def get_app(pid):
    app = atspi.find_app(pid)
    if app is None:
        raise ScrapeError(f"进程 {pid} 不在无障碍树里，确认 QQ 是用 --force-renderer-accessibility 启动的")
    return app


def conversation_list(app):
    node = atspi.find_one(atspi.ui_root(app), role="embedded", name=CONVERSATION_LIST)
    if node is None:
        raise ScrapeError("找不到「会话列表」，无障碍树可能还没填充")
    return node


def _first_static(node):
    for n in atspi.walk(node, max_depth=6):
        if atspi.node_role(n) == "static":
            return atspi.node_name(n)
    return None


def list_conversations(app):
    items = atspi.children(conversation_list(app))
    return [(name, item) for item in items if (name := _first_static(item))]


def find_item(app, name):
    for item_name, item in list_conversations(app):
        if item_name == name:
            return item
    return None


def dismiss_menu():
    """Close any open context menu.

    xdotool delivers the key to whatever window has focus, so the QQ window has
    to be activated first; otherwise the Escape lands in some unrelated app.
    """
    if not shutil.which("xdotool"):
        return False
    try:
        found = subprocess.run(
            ["xdotool", "search", "--class", "qq"], capture_output=True, timeout=2
        ).stdout.split()
        for wid in found:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", wid.decode()], capture_output=True, timeout=2
            )
        subprocess.run(["xdotool", "key", "--clearmodifiers", "Escape"], capture_output=True, timeout=2)
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def _clear_menu(app, timeout=1.0):
    """Make sure no context menu from a previous iteration is still around."""
    if atspi.find_one(app, role="menu item") is None:
        return True
    dismiss_menu()
    return atspi.wait_gone(lambda: app, role="menu item", timeout=timeout)


def _read_uin(before, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = clipboard.read_text()
        if value and value != before and is_uin(value):
            return value.strip()
        time.sleep(0.03)
    return None


def _claim_clipboard(timeout=1.0):
    """Write the sentinel and wait until we actually own the selection.

    wl-copy takes ownership asynchronously; without this wait the sentinel can
    land *after* QQ has written the uin and clobber it.
    """
    clipboard.write_text(SENTINEL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (clipboard.read_text() or "").strip() == SENTINEL:
            return True
        time.sleep(0.02)
    return False


def _wait_menu(app, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for label, kind in COPY_LABELS.items():
            menu_item = atspi.find_one(app, role="menu item", name=label)
            if menu_item is not None:
                return menu_item, kind
        time.sleep(0.03)
    return None, None


def copy_uin(app, item, timeout=2.0, attempts=2, settle=0.2):
    """Right-click a conversation and read back its uin. Returns (uin, kind)."""
    for attempt in range(attempts):
        if attempt:
            time.sleep(settle * 2)
        if not _clear_menu(app):
            return None, None
        if not atspi.invoke(item, "showContextMenu"):
            continue
        menu_item, kind = _wait_menu(app, timeout)
        if menu_item is None:
            dismiss_menu()
            continue
        if not _claim_clipboard():
            dismiss_menu()
            continue
        if not atspi.invoke(menu_item):
            dismiss_menu()
            continue
        uin = _read_uin(SENTINEL, timeout)
        if uin is not None:
            atspi.wait_gone(lambda: app, role="menu item", timeout=timeout)
            return uin, kind
        dismiss_menu()
    return None, None


def sync(cache, pid, ttl=None, force=False, limit=None, verbose=True, settle=0.35):
    """Fill in every conversation whose name is missing or stale.

    Returns (updated, skipped, failed) counts.
    """
    app = get_app(pid)
    updated = 0
    failed = set()
    done = set()

    with clipboard.preserved():
        while True:
            app = atspi.find_app(pid) or app
            names = [n for n, _ in list_conversations(app)]
            todo = [
                n
                for n in names
                if n not in failed
                and n not in done
                and (force or not cache.is_fresh(n, ttl))
            ]
            if not todo or (limit is not None and updated >= limit):
                skipped = len([n for n in names if cache.is_fresh(n, ttl)])
                break

            target = todo[0]
            item = find_item(app, target)
            if item is None:
                failed.add(target)
                continue

            uin, kind = copy_uin(app, item)
            done.add(target)
            if uin is None:
                failed.add(target)
                if verbose:
                    logger.warning(f"{target!r} 复制失败")
            else:
                cache.record(target, uin, kind)
                updated += 1
                if verbose:
                    logger.success(f"{target!r} -> {uin} ({kind})")
            time.sleep(settle)

    return updated, skipped, len(failed)
