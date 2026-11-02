import unittest
from unittest import mock

from qqcache import login


class N:
    def __init__(self, role="section", name="", cls="", kids=()):
        self.role, self.name, self.cls, self.kids = role, name, cls, list(kids)


def _walk(n, max_depth=30):
    yield n
    for k in n.kids:
        yield from _walk(k)


def _dialog(title, body, cls="q-dialog-main", button="确定"):
    return N(cls=cls, kids=[
        N(cls="q-dialog-title", kids=[N("static", title)]),
        N(kids=[N("static", body)]),
        N("button", button),
    ])


class NoticeTest(unittest.TestCase):
    def _run(self, root, fn):
        pressed = []
        with mock.patch.object(login.atspi, "ui_root", return_value=root), \
             mock.patch.object(login.atspi, "walk", side_effect=_walk), \
             mock.patch.object(login.atspi, "find_all_by_class",
                               side_effect=lambda r, c, max_depth=30: [n for n in _walk(r) if c in n.cls.split()]), \
             mock.patch.object(login.atspi, "find_by_class",
                               side_effect=lambda r, c, max_depth=30: next((n for n in _walk(r) if c in n.cls.split()), None)), \
             mock.patch.object(login.atspi, "has_class", side_effect=lambda n, c: c in n.cls.split()), \
             mock.patch.object(login.atspi, "node_role", side_effect=lambda n: n.role), \
             mock.patch.object(login.atspi, "node_name", side_effect=lambda n: n.name), \
             mock.patch.object(login.atspi, "text_of",
                               side_effect=lambda n, max_depth=30: "".join(x.name for x in _walk(n) if x.role == "static")), \
             mock.patch.object(login.atspi, "extents", return_value=(1041, 590, 78, 32)), \
             mock.patch.object(login, "primary_button_on_screen",
                               side_effect=lambda ext: self.on_screen.pop(0) if self.on_screen else False), \
             mock.patch.object(login.subprocess, "run",
                               side_effect=lambda cmd, **k: pressed.append("click@" + cmd[2] + "," + cmd[3])):
            return fn(object()), pressed

    on_screen = []

    def test_kickoff_notice_dismissed(self):
        root = N(kids=[_dialog("下线通知", "您的账号已在另一台终端登录。", cls="kickoff-tips q-dialog-main")])
        self.on_screen = [True, False]   # 点之前在屏幕上，点完消失
        out, pressed = self._run(root, login.dismiss_notice)
        self.assertEqual(out, ("下线通知", "您的账号已在另一台终端登录。"))
        self.assertEqual(pressed, ["click@1080,606"])

    def test_stale_node_after_close_is_not_clicked(self):
        # 登录窗 a11y 树冻住：对话框关掉后节点还在，但屏幕上已经没有按钮
        root = N(kids=[_dialog("下线通知", "x", cls="kickoff-tips q-dialog-main")])
        self.on_screen = [False]
        out, pressed = self._run(root, login.dismiss_notice)
        self.assertIsNone(out)
        self.assertEqual(pressed, [])

    def test_unknown_confirm_dialog_left_alone(self):
        root = N(kids=[_dialog("删除聊天记录", "确定删除？")])
        self.on_screen = [True, True]
        out, pressed = self._run(root, login.dismiss_notice)
        self.assertIsNone(out)
        self.assertEqual(pressed, [])

    def test_no_dialog(self):
        out, pressed = self._run(N(kids=[N("button", "确定")]), login.dismiss_notice)
        self.assertIsNone(out)
        self.assertEqual(pressed, [])


class PrimaryButtonPixelTest(unittest.TestCase):
    def test_blue_button_vs_background(self):
        import struct
        import zlib

        def png(color, w=10, h=4):
            raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
            def chunk(t, d):
                return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

        with mock.patch.object(login, "screenshot", return_value=png((0, 153, 255))):
            self.assertTrue(login.primary_button_on_screen((0, 0, 10, 4)))
        with mock.patch.object(login, "screenshot", return_value=png((244, 247, 255))):
            self.assertFalse(login.primary_button_on_screen((0, 0, 10, 4)))
