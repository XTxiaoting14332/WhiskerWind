import unittest
from unittest import mock

from qqcache import send
from qqcache.send import SendError


class FakeSidebar:
    """虚拟化侧栏：每次 scrollForward 换一屏，只暴露当前屏的项。"""

    def __init__(self, pages):
        self.pages = pages
        self.idx = 0
        self.current = None
        self.clicked = []

    def invoke(self, node, action):
        if node == "list":
            if action == "scrollForward":
                self.idx = min(self.idx + 1, len(self.pages) - 1)
            elif action == "scrollBackward":
                self.idx = max(self.idx - 1, 0)
            return True
        if action == "click":
            self.clicked.append(node)
            self.current = node
        return True

    def items(self, app):
        return [(n, n) for n in self.pages[self.idx]]


class OpenConversationScrollTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(send, "conversation_list", return_value="list"),
            mock.patch.object(send.time, "sleep", return_value=None),
            mock.patch.object(send.atspi, "extents", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _bind(self, sidebar):
        self.sidebar = sidebar
        p1 = mock.patch.object(send.atspi, "invoke", side_effect=sidebar.invoke)
        p2 = mock.patch.object(send, "conversation_items", side_effect=sidebar.items)
        p3 = mock.patch.object(send, "current_conversation", side_effect=lambda app: sidebar.current)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def test_visible_item_clicked_without_scrolling(self):
        sb = FakeSidebar([["a", "files"]])
        self._bind(sb)
        self.assertTrue(send.open_conversation(None, "files"))
        self.assertEqual(sb.clicked, ["files"])
        self.assertEqual(sb.idx, 0)

    def test_item_below_fold_found_by_scrolling(self):
        sb = FakeSidebar([["a", "b"], ["c", "d"], ["e", "files"]])
        self._bind(sb)
        with mock.patch.object(send, "search_and_open") as search:
            self.assertTrue(send.open_conversation(None, "files"))
            search.assert_not_called()
        self.assertEqual(sb.clicked, ["files"])
        self.assertEqual(sb.idx, 2)

    def test_missing_item_scrolls_back_and_falls_back_to_search(self):
        sb = FakeSidebar([["a", "b"], ["c", "d"]])
        self._bind(sb)
        with mock.patch.object(send, "search_and_open", side_effect=SendError("没有")) as search:
            with self.assertRaises(SendError):
                send.open_conversation(None, "files")
            search.assert_called_once()
        self.assertEqual(sb.idx, 0)
        self.assertEqual(sb.clicked, [])


if __name__ == "__main__":
    unittest.main()


class MessageTabRecoveryTest(unittest.TestCase):
    """会话列表不见了（被带到联系人页）时切回「消息」页再找。"""

    def test_conversation_list_switches_back_to_message_tab(self):
        from unittest import mock
        from qqcache import send

        state = {"tab": "contacts"}

        def find_one(root, role=None, name=None):
            if name == send.CONVERSATION_LIST:
                return "list" if state["tab"] == "messages" else None
            if role == "toggle button" and name == "消息":
                return "msgbtn"
            return None

        def invoke(node, action=None):
            if node == "msgbtn":
                state["tab"] = "messages"
                return True
            return False

        with mock.patch.object(send.atspi, "ui_root"), \
             mock.patch.object(send.atspi, "find_one", side_effect=find_one), \
             mock.patch.object(send.atspi, "invoke", side_effect=invoke):
            self.assertEqual(send.conversation_list(object()), "list")

    def test_login_page_left_alone(self):
        from unittest import mock
        from qqcache import send

        with mock.patch.object(send.atspi, "ui_root"), \
             mock.patch.object(send.atspi, "find_one", return_value=None), \
             mock.patch.object(send.atspi, "invoke") as inv:
            self.assertFalse(send.ensure_message_tab(object()))
            inv.assert_not_called()


class MessageTabStateTest(unittest.TestCase):
    def test_toggle_actions_tell_active_tab(self):
        from unittest import mock
        from qqcache import send

        with mock.patch.object(send, "message_tab_button", return_value="btn"), \
             mock.patch.object(send.atspi, "actions", return_value=["check", "showContextMenu"]):
            self.assertFalse(send.on_message_tab(object()))
        with mock.patch.object(send, "message_tab_button", return_value="btn"), \
             mock.patch.object(send.atspi, "actions", return_value=["uncheck", "showContextMenu"]):
            self.assertTrue(send.on_message_tab(object()))
        with mock.patch.object(send, "message_tab_button", return_value=None):
            self.assertIsNone(send.on_message_tab(object()))
