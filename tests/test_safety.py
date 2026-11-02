import os
import tempfile
import unittest
from unittest import mock

from qqcache.messages import Message
from qqcache.send import SendError
from qqcache.serve import Bridge


def _bridge():
    tmp = tempfile.mkdtemp()
    return Bridge(os.path.join(tmp, "b.sqlite3"), token="t")


def _msg(content="#ww", msg_id="m1", sender="A", kind="group", is_self=False):
    return Message(
        msg_id=msg_id, kind=kind, sender=sender, is_self=is_self,
        content=content, title="", quoted_sender=None, quoted_content=None,
        timestamp="0",
    )


class NotifyRoutingTest(unittest.TestCase):
    """通知路由：已在目标会话不切；通知活着才 ActionInvoked；撤回了直接主动打开。"""

    def test_live_notification_invoked_under_lock(self):
        b = _bridge()
        b._app = mock.Mock(return_value=object())
        seen = []
        b.invoke_notification = lambda s: seen.append(b.gui_lock._is_owned()) or True
        with b.gui_lock, \
             mock.patch("qqcache.serve.current_conversation", side_effect=["Linux群", "files"]), \
             mock.patch.object(b, "_open_notified") as opn, \
             mock.patch.object(b, "_resolve_uin", return_value=("100000001", "group")), \
             mock.patch.object(b, "poll_emit", return_value=1):
            b._handle_notification("files")
        self.assertEqual(seen, [True])
        opn.assert_not_called()

    def test_invokes_even_when_same_name_already_open(self):
        # 同名的另一个群：只比名字会误以为已经在目标会话上
        b = _bridge()
        b._app = mock.Mock(return_value=object())
        b.invoke_notification = mock.Mock(return_value=True)
        with mock.patch("qqcache.serve.current_conversation", return_value="files"), \
             mock.patch.object(b, "_resolve_uin", return_value=("100000001", "group")), \
             mock.patch.object(b, "poll_emit", return_value=1):
            b._handle_notification("files")
        b.invoke_notification.assert_called_once_with("files")

    def test_non_chat_notification_switches_back(self):
        # 群通知/好友申请：ActionInvoked 把界面带到联系人页——切回消息页，
        # 不等满 4s、不按标题去打开会话
        b = _bridge()
        b._app = mock.Mock(return_value=object())
        b.invoke_notification = mock.Mock(return_value=True)
        with mock.patch("qqcache.serve.on_message_tab", return_value=False), \
             mock.patch("qqcache.serve.current_conversation", return_value="Linux群"), \
             mock.patch("qqcache.serve.ensure_message_tab") as back, \
             mock.patch.object(b, "_open_notified") as opn, \
             mock.patch.object(b, "poll_emit") as poll:
            t = __import__("time").monotonic()
            b._handle_notification("群通知")
            self.assertLess(__import__("time").monotonic() - t, 1.0)
        back.assert_called_once()
        opn.assert_not_called()
        poll.assert_not_called()

    def test_withdrawn_notification_skips_wait(self):
        b = _bridge()
        b._app = mock.Mock(return_value=object())
        b.invoke_notification = mock.Mock(return_value=False)
        with mock.patch("qqcache.serve.current_conversation", return_value="Linux群"), \
             mock.patch.object(b, "_wait_conversation") as wait, \
             mock.patch.object(b, "_open_notified", return_value="files") as opn, \
             mock.patch.object(b, "_resolve_uin", return_value=("100000001", "group")), \
             mock.patch.object(b, "poll_emit", return_value=1):
            b._handle_notification("files")
        wait.assert_not_called()
        opn.assert_called_once()

    def test_notification_for_other_conversation_falls_back_to_open_when_idle(self):
        b = _bridge()
        b._app = mock.Mock(return_value=object())
        b.cache.record("files", "100000001", kind="group")
        with mock.patch("qqcache.serve.current_conversation", return_value="Linux群"), \
             mock.patch.object(b, "_open_notified", return_value="files") as opn, \
             mock.patch.object(b, "_resolve_uin", return_value=("100000001", "group")), \
             mock.patch.object(b, "poll_emit", return_value=1) as poll:
            b._handle_notification("files")
            opn.assert_called_once()
            poll.assert_called_once()

    def test_notification_for_current_conversation_is_processed(self):
        b = _bridge()
        b._app = mock.Mock(return_value=object())
        b.cache.record("files", "100000001", kind="group")
        with mock.patch("qqcache.serve.current_conversation", return_value="files"), \
             mock.patch("qqcache.serve._names_match", side_effect=lambda a, s: a == s), \
             mock.patch.object(b, "_resolve_uin", return_value=("100000001", "group")), \
             mock.patch.object(b, "poll_emit", return_value=1) as poll:
            b._handle_notification("files")
            poll.assert_called_once()
            self.assertTrue(poll.call_args[0][1] == "100000001")


class WwDedupeTest(unittest.TestCase):
    def test_same_message_only_replies_once(self):
        b = _bridge()
        b.test_trigger = True
        b.api_send = mock.Mock(return_value={"message_id": 1})
        m = _msg()
        b._nw_trigger(m, "100000002")
        self.assertEqual(b.api_send.call_count, 1)
        b._nw_trigger(m, "100000002")
        self.assertEqual(b.api_send.call_count, 1)
        self.assertTrue(b.ids.ww_replied("100000002", "m1"))

    def test_non_ww_not_answered(self):
        b = _bridge()
        b.test_trigger = True
        b.api_send = mock.Mock()
        b._nw_trigger(_msg(content="hello"), "100000002")
        b.api_send.assert_not_called()


class WwBatchTest(unittest.TestCase):
    """视口里躺着的旧 #ww 和新 #ww 同批到达：只回最新一条（连发事故）。"""

    def test_only_newest_ww_in_batch_answered(self):
        b = _bridge()
        b.test_trigger = True
        b.api_send = mock.Mock(return_value={"message_id": 1})
        batch = [_msg(msg_id=f"m{i}") for i in range(4)] + [_msg("hi", msg_id="m9")]
        b._nw_triggers(batch, "100000001")
        self.assertEqual(b.api_send.call_count, 1)
        for i in range(4):
            self.assertTrue(b.ids.ww_replied("100000001", f"m{i}"))
        # 重读同一批不再回复
        b._nw_triggers(batch, "100000001")
        self.assertEqual(b.api_send.call_count, 1)


class ComposerResidueTest(unittest.TestCase):
    """失败的发送留下的半成品不能和下一次粘贴叠成一条消息。"""

    def _send(self, empty_states, clear_ok=True, press_exc=None):
        from qqcache import send as sendmod

        empties = iter(empty_states)
        with mock.patch.object(sendmod, "activate_qq"), \
             mock.patch.object(sendmod, "open_conversation"), \
             mock.patch.object(sendmod, "snapshot_ids", return_value=set()), \
             mock.patch.object(sendmod, "editor", return_value=object()), \
             mock.patch.object(sendmod, "is_empty", side_effect=lambda app: next(empties)), \
             mock.patch.object(sendmod, "clear", return_value=clear_ok) as clr, \
             mock.patch.object(sendmod, "paste") as pst, \
             mock.patch.object(sendmod, "send_enabled", return_value=True), \
             mock.patch.object(sendmod, "press_send", side_effect=press_exc) as prs, \
             mock.patch.object(sendmod.clipboard, "preserved", return_value=mock.MagicMock()):
            try:
                sendmod.send(object(), name="files", text="x", verify=False)
                err = None
            except SendError as exc:
                err = exc
        return err, clr, pst, prs

    def test_residue_cleared_before_paste(self):
        err, clr, pst, prs = self._send([False])
        self.assertIsNone(err)
        clr.assert_called_once()
        pst.assert_called_once()
        prs.assert_called_once()

    def test_uncleared_residue_refuses(self):
        err, clr, pst, prs = self._send([False], clear_ok=False)
        self.assertIn("残留", str(err))
        pst.assert_not_called()
        prs.assert_not_called()

    def test_failure_before_press_clears_composer(self):
        err, clr, pst, prs = self._send([True], press_exc=SendError("找不到发送按钮"))
        self.assertIsNotNone(err)
        clr.assert_called_once()


class SendTargetGuardTest(unittest.TestCase):
    def test_mismatched_conversation_refuses_to_send(self):
        from qqcache import send as sendmod
        b = _bridge()
        b.cache.record("files", "100000001", kind="group")
        with mock.patch.object(sendmod, "current_conversation", return_value="Linux群"), \
             mock.patch.object(sendmod, "_verify_uin", return_value=True):
            with self.assertRaises(SendError):
                sendmod._assert_send_target(object(), "100000001", b.cache)

    def test_failed_identity_verification_refuses_to_send(self):
        from qqcache import send as sendmod
        b = _bridge()
        b.cache.record("files", "100000001", kind="group")
        with mock.patch.object(sendmod, "current_conversation", return_value="files"), \
             mock.patch.object(sendmod, "_verify_uin", return_value=False):
            with self.assertRaises(SendError):
                sendmod._assert_send_target(object(), "100000001", b.cache)

    def test_matching_conversation_and_identity_pass(self):
        from qqcache import send as sendmod
        b = _bridge()
        b.cache.record("files", "100000001", kind="group")
        with mock.patch.object(sendmod, "current_conversation", return_value="files"), \
             mock.patch.object(sendmod, "_verify_uin", return_value=True):
            sendmod._assert_send_target(object(), "100000001", b.cache)


class UnchangedRecheckTest(unittest.TestCase):
    """粘贴前复核：会话名 + 消息 ID 快照，不再点第二次资料卡。"""

    def _check(self, cur, now_ids, known=frozenset({"a", "b", "c"}), verify=True):
        from qqcache import send as sendmod
        b = _bridge()
        b.cache.record("files", "100000001", kind="group")
        with mock.patch.object(sendmod, "current_conversation", return_value=cur), \
             mock.patch.object(sendmod, "snapshot_ids", return_value=set(now_ids)), \
             mock.patch.object(sendmod, "_verify_uin", return_value=verify) as card:
            try:
                sendmod._assert_unchanged(object(), "100000001", b.cache, "files", set(known))
                return None, card
            except SendError as exc:
                return exc, card

    def test_same_conversation_passes_without_card(self):
        err, card = self._check("files", {"b", "c", "d"})  # 新来一条消息也算同一会话
        self.assertIsNone(err)
        card.assert_not_called()

    def test_same_name_other_group_refused_by_card(self):
        # 同名的另一个群：快照无交集 → 资料卡验号 → 号不对，拒发
        err, card = self._check("files", {"x", "y", "z"}, verify=False)
        self.assertIsNotNone(err)
        card.assert_called()

    def test_flooded_viewport_not_falsely_refused(self):
        # 刷屏把可见消息整屏顶掉：快照无交集，但资料卡验号通过 → 照发
        err, card = self._check("files", {"x", "y", "z"}, verify=True)
        self.assertIsNone(err)
        card.assert_called()

    def test_renamed_conversation_refused(self):
        err, _ = self._check("Linux群", {"a", "b", "c"})
        self.assertIsNotNone(err)

    def test_empty_snapshot_falls_back_to_card(self):
        err, card = self._check("files", set(), known=frozenset(), verify=False)
        self.assertIsNotNone(err)
        card.assert_called()



class SameNameIdentityTest(unittest.TestCase):
    """同名的两个群：读到的树必须证明自己属于缓存给出的号。"""

    def _setup(self):
        from qqcache import peer
        b = _bridge()
        b.cache.record("files", "100000001", kind="group")
        b.cursor.commit("100000001", [_msg(msg_id="seenA")])
        return b, peer

    def test_seen_msg_id_proves_identity_without_card(self):
        b, peer = self._setup()
        with mock.patch("qqcache.serve.current_conversation", return_value="files"), \
             mock.patch.object(peer, "uin_of") as card:
            self.assertTrue(b._identity_ok(object(), "100000001",
                                           [_msg(msg_id="seenA"), _msg(msg_id="new1")]))
        card.assert_not_called()

    def test_same_name_other_group_rejected_and_learned(self):
        b, peer = self._setup()
        ident = peer.Identity(uin="999000111", kind="group", texts=())
        with mock.patch("qqcache.serve.current_conversation", return_value="files"), \
             mock.patch.object(peer, "uin_of", return_value=ident):
            self.assertFalse(b._identity_ok(object(), "100000001", [_msg(msg_id="b1")]))
        # 一名多号：名字不再单独决定号码
        self.assertIsNone(b.cache.get("files"))
        self.assertTrue(b.cache.ambiguous("files"))
        # 下一轮候选号就是刚验出来的那个
        with mock.patch("qqcache.serve.current_conversation", return_value="files"):
            self.assertEqual(b._resolve_uin(object(), "files", "files")[0], "999000111")

    def test_unverifiable_identity_not_emitted(self):
        b, peer = self._setup()
        with mock.patch("qqcache.serve.current_conversation", return_value="files"), \
             mock.patch.object(peer, "uin_of", side_effect=peer.PeerError("x")):
            self.assertFalse(b._identity_ok(object(), "100000001", [_msg(msg_id="b1")]))


class SidebarSameNameTest(unittest.TestCase):
    def test_duplicate_names_without_selection_give_none(self):
        from qqcache import peer
        rows = {"r1": ("files", "recent-contact-item"), "r2": ("files", "recent-contact-item")}
        with mock.patch.object(peer.atspi, "find_one", return_value="lst"), \
             mock.patch.object(peer.atspi, "ui_root"), \
             mock.patch.object(peer.atspi, "children", return_value=list(rows)), \
             mock.patch.object(peer.atspi, "walk", side_effect=lambda n, max_depth=0: [n]), \
             mock.patch.object(peer.atspi, "node_role", return_value="static"), \
             mock.patch.object(peer.atspi, "node_name", side_effect=lambda n: rows[n][0]), \
             mock.patch.object(peer.atspi, "attr", side_effect=lambda n, k, d="": rows[n][1]):
            self.assertIsNone(peer._sidebar_item_for(object(), "files"))
            rows["r2"] = ("files", "recent-contact-item recent-contact-item--selected")
            self.assertEqual(peer._sidebar_item_for(object(), "files"), "r2")

if __name__ == "__main__":
    unittest.main()