"""login.detect_self_profile / detect_self_uin — auth/login.enc 解析。"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from qqcache import login


class _Node:
    def __init__(self, role, name="", cls="", ext=None, kids=()):
        self.role, self.name, self.cls, self.ext, self.kids = (
            role, name, cls, ext, kids)


def _flat(nodes):
    out = []

    def rec(n):
        out.append(n)
        for k in n.kids:
            rec(k)
    for n in nodes:
        rec(n)
    return out


class LoginStateTest(unittest.TestCase):
    """login_state / _login_frame / _frame_qr_guess 的几何兜底。"""

    def _patch_atspi(self, nodes):
        flat = _flat(nodes)

        def find_one(root, role=None, name=None):
            for n in flat:
                if role is not None and n.role != role:
                    continue
                if name is not None and n.name != name:
                    continue
                return n
            return None

        def find_by_class(root, cls):
            for n in flat:
                if cls and cls in n.cls:
                    return n
            return None

        return [
            patch.object(login.atspi, "ui_root", lambda app: nodes[0]),
            patch.object(login.atspi, "find_one", find_one),
            patch.object(login.atspi, "find_by_class", find_by_class),
            patch.object(login.atspi, "walk", lambda root: flat),
            patch.object(login.atspi, "node_role", lambda n: n.role),
            patch.object(login.atspi, "node_name", lambda n: n.name),
            patch.object(login.atspi, "extents", lambda n: n.ext),
        ]

    def _run(self, nodes):
        patches = self._patch_atspi(nodes)
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        return login.login_state(object())

    def test_not_running(self):
        self.assertEqual(login.login_state(None), "not_running")

    def test_logged_in_markers_win_over_small_frame(self):
        # 登录窗尺寸的主窗 + 会话列表标记 → logged_in 优先于几何兜底
        tree = [
            _Node("application", kids=[
                _Node("frame", "QQ", ext=(620, 320, 680, 440), kids=[
                    _Node("embedded", "会话列表"),
                ]),
            ]),
        ]
        self.assertEqual(self._run(tree), "logged_in")

    def test_contacts_page_is_still_logged_in(self):
        # 系统通知把主界面带到联系人→群通知页：没有会话列表，页面上还有
        # 「登录」字样也不能判成登录页——导航栏「消息」按钮就是已登录
        tree = [
            _Node("application", kids=[
                _Node("frame", "QQ", ext=(480, 220, 960, 640), kids=[
                    _Node("toggle button", "消息"),
                    _Node("static", "登录设备管理"),
                ]),
            ]),
        ]
        self.assertEqual(self._run(tree), "logged_in")

    def test_static_hint(self):
        tree = [
            _Node("application", kids=[
                _Node("frame", "QQ", ext=(0, 0, 1500, 900), kids=[
                    _Node("static", "扫码登录"),
                ]),
            ]),
        ]
        self.assertEqual(self._run(tree), "login_required")

    def test_bare_tree_small_frame_is_login_required(self):
        # 真机观察：账密+QR 页只投影 application>frame>panel——
        # 没有 static/按钮，几何小窗是唯一可判信号
        tree = [
            _Node("application", kids=[
                _Node("frame", "QQ", ext=(620, 320, 680, 440), kids=[
                    _Node("panel", kids=[_Node("panel")]),
                ]),
            ]),
        ]
        self.assertEqual(self._run(tree), "login_required")

    def test_large_frame_without_markers_is_unknown(self):
        tree = [
            _Node("application", kids=[
                _Node("frame", "QQ", ext=(0, 0, 1900, 1030)),
            ]),
        ]
        self.assertEqual(self._run(tree), "unknown")

    def test_frame_qr_guess(self):
        frame = _Node("frame", "QQ", ext=(620, 320, 680, 440))
        tree = [_Node("application", kids=[frame])]
        patches = self._patch_atspi(tree)
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        guess = login._frame_qr_guess(object())
        x, y, w, h = guess
        self.assertGreaterEqual(x, 620 + int(680 * 0.6))   # 右列
        self.assertLessEqual(x + w, 620 + 680 + 10)
        self.assertGreater(w, 150)                          # 够裁出码
        # 非登录窗（大窗）不猜
        big = _Node("frame", "QQ", ext=(0, 0, 1900, 1030))
        patches = self._patch_atspi([_Node("application", kids=[big])])
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        self.assertIsNone(login._frame_qr_guess(object()))


class DetectSelfProfileTest(unittest.TestCase):
    def _config(self, login_json=None, partitions=()):
        root = tempfile.mkdtemp()
        if login_json is not None:
            os.makedirs(os.path.join(root, "auth"))
            with open(os.path.join(root, "auth", "login.enc"), "wb") as fh:
                fh.write(login_json if isinstance(login_json, bytes)
                         else json.dumps(login_json).encode())
        for uin in partitions:
            os.makedirs(os.path.join(root, "Partitions", f"qqnt_{uin}"))
        return root

    def test_reads_nickname_and_face(self):
        root = self._config({"account": "10001", "loginList": [
            {"uin": "10001", "nickName": "Alice", "faceUrl": "https://x/f"},
        ]}, partitions=["10001"])
        prof = login.detect_self_profile(root)
        self.assertEqual(prof["uin"], "10001")
        self.assertEqual(prof["nickname"], "Alice")
        self.assertEqual(prof["face_url"], "https://x/f")

    def test_prefers_partition_uin_over_first_entry(self):
        root = self._config({"account": "10001", "loginList": [
            {"uin": "10001", "nickName": "老号"},
            {"uin": "20002", "nickName": "新号"},
        ]}, partitions=["20002"])
        self.assertEqual(login.detect_self_profile(root)["nickname"], "新号")

    def test_falls_back_to_account_field(self):
        root = self._config({"account": "20002", "loginList": [
            {"uin": "10001", "nickName": "老号"},
            {"uin": "20002", "nickName": "新号"},
        ]})
        self.assertEqual(login.detect_self_profile(root)["uin"], "20002")

    def test_missing_or_broken_file(self):
        self.assertEqual(login.detect_self_profile(self._config()), {})
        self.assertEqual(
            login.detect_self_profile(self._config(b"\xff\xfe not json")), {})

    def test_missing_uin_keys(self):
        root = self._config({"loginList": [{"uin": "10001"}]})
        prof = login.detect_self_profile(root)
        self.assertEqual(prof["nickname"], "")
        self.assertEqual(prof["face_url"], "")


if __name__ == "__main__":
    unittest.main()
