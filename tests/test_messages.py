import unittest

from qqcache.messages import parse_messages


def el(role="section", cls="", node_id="", name="", children=None):
    return {
        "role": role,
        "name": name,
        "attrs": {"class": cls, "id": node_id},
        "children": children or [],
    }


def static(text):
    return el(role="static", name=text)


def text_item(msg_id, sender, body, others=True, ts=None, title=None, kind_class="", content_nodes=None):
    timestamp = el(cls="message__timestamp no-copy", children=[static(ts)]) if ts else None
    user_name = el(
        cls="user-name no-copy text-ellipsis",
        children=(
            [el(cls="group-member-label q-tag q-tag--grey", children=[static(title)])] if title else []
        )
        + [static(sender)],
    )
    direction = "container--others" if others else "container--self"
    container = el(
        cls=f"msg-content-container {direction} mix-message__container{kind_class}",
        children=[
            el(
                cls="message-content mix-message__inner",
                children=content_nodes if content_nodes is not None else [static(body)],
            )
        ],
    )
    message = el(
        cls="message-container message-container--mix-or-markdown",
        children=[
            el(cls="avatar-span", name=sender, children=[el(cls="avatar-native")]),
            user_name,
            el(cls="message-content__wrapper", children=[container]),
        ],
    )
    parts = [p for p in (timestamp, message) if p]
    return el(cls="ml-item", node_id=msg_id, children=[el(cls="message", children=parts)])


def forward_item(msg_id, sender, lines, others=True):
    body = el(
        cls="forward-msg",
        children=[el(cls="fwd-title", children=[static("群聊的聊天记录")])]
        + [el(cls="fwd-content text-ellipsis", children=[static(line)]) for line in lines]
        + [el(cls="count", children=[static(f"查看{len(lines)}条转发消息")])],
    )
    message = el(
        cls="message-container message-container--others",
        children=[
            el(cls="avatar-span", name=sender),
            el(cls="message-content__wrapper", children=[body]),
        ],
    )
    return el(cls="ml-item", node_id=msg_id, children=[el(cls="message", children=[message])])


def wrap(items):
    return el(cls="ml-area ml-list-area", children=[el(cls="q-scroll-view ml-root", children=[el(cls="ml-list list", children=items)])])


def file_item(msg_id, name, size, others=True):
    direction = "message-container--others" if others else "message-container--self"
    element = el(
        cls="normal-file file-element",
        children=[
            el(role="paragraph", cls="file-name", children=[static(name[:4]), static(name[4:])]),
            el(cls="file-icon"),
            static(size),
        ],
    )
    message = el(
        cls=f"message-container {direction} file-message",
        children=[
            el(cls="avatar-span", name="某人"),
            el(cls="message-content__wrapper", children=[el(cls="file-message--content", children=[element])]),
        ],
    )
    return el(cls="ml-item", node_id=msg_id, children=[el(cls="message", children=[message])])


def reply_item(msg_id, quoted_sender, quoted_text, text, others=True):
    direction = "container--others" if others else "container--self"
    inner = el(
        cls="message-content reply-message__inner",
        children=[
            el(
                cls="reply-element no-copy",
                children=[static(quoted_sender), el(role="image"), static(quoted_text)],
            ),
            static(text),
        ],
    )
    container = el(cls=f"msg-content-container {direction} reply-message__container", children=[inner])
    message = el(
        cls="message-container message-container--self",
        children=[el(cls="avatar-span", name="某人"), el(cls="message-content__wrapper", children=[container])],
    )
    return el(cls="ml-item", node_id=msg_id, children=[el(cls="message", children=[message])])


def system_item(msg_id, who, what):
    tip = el(
        cls="gray-tip-message no-copy",
        children=[
            el(
                cls="gray-tip-message__inner babble",
                children=[
                    el(
                        cls="gray-tip-element gray-tip-content",
                        children=[
                            static(who),
                            static(what),
                            el(role="link", cls="gray-tip-action", children=[static(" 重新编辑")]),
                        ],
                    )
                ],
            )
        ],
    )
    return el(cls="ml-item", node_id=msg_id, children=[el(cls="message", children=[tip])])


def voice_item(msg_id, others=True):
    direction = "container--others" if others else "container--self"
    container = el(
        cls=f"msg-content-container {direction} ptt-message__container ptt-message",
        children=[static("[暂不支持该功能，请用手机 QQ 查看]")],
    )
    message = el(
        cls="message-container message-container--self ptt-message",
        children=[el(cls="avatar-span", name="某人"), el(cls="message-content__wrapper", children=[container])],
    )
    return el(cls="ml-item", node_id=msg_id, children=[el(cls="message", children=[message])])


class ParseMessagesTest(unittest.TestCase):
    def test_text_message(self):
        msgs = parse_messages(wrap([text_item("1001", "北风", "/help", ts="00:31", title="管理员")]))
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m.msg_id, "1001")
        self.assertEqual(m.kind, "text")
        self.assertEqual(m.sender, "北风")
        self.assertEqual(m.title, "管理员")
        self.assertEqual(m.content, "/help")
        self.assertEqual(m.timestamp, "00:31")
        self.assertFalse(m.is_self)

    def test_timestamp_is_inherited(self):
        msgs = parse_messages(
            wrap(
                [
                    text_item("1001", "a", "一", ts="昨天 01:01"),
                    text_item("1002", "b", "二"),
                    text_item("1003", "c", "三", ts="昨天 02:00"),
                    text_item("1004", "d", "四"),
                ]
            )
        )
        self.assertEqual([m.timestamp for m in msgs], ["昨天 01:01", "昨天 01:01", "昨天 02:00", "昨天 02:00"])

    def test_direction(self):
        msgs = parse_messages(
            wrap([text_item("1001", "他", "hi", others=True), text_item("1002", "我", "yo", others=False)])
        )
        self.assertFalse(msgs[0].is_self)
        self.assertTrue(msgs[1].is_self)

    def test_image_message(self):
        item = text_item(
            "1001",
            "妖狐",
            "",
            kind_class=" mix-message__container--pic",
            content_nodes=[el(role="image", cls="pic-element", name="图片")],
        )
        msgs = parse_messages(wrap([item]))
        self.assertEqual(msgs[0].kind, "image")
        self.assertEqual(msgs[0].content, "")

    def test_forward_card(self):
        msgs = parse_messages(wrap([forward_item("2001", "某人", ["QQ用户: [图片]", "QQ用户: 你好"])]))
        m = msgs[0]
        self.assertEqual(m.kind, "forward")
        self.assertIn("群聊的聊天记录", m.content)
        self.assertIn("QQ用户: 你好", m.content)

    def test_skips_nodes_without_id(self):
        items = [el(cls="ml-item", children=[static("新消息")]), text_item("1001", "a", "x")]
        msgs = parse_messages(wrap(items))
        self.assertEqual([m.msg_id for m in msgs], ["1001"])

    def test_empty_tree(self):
        self.assertEqual(parse_messages(None), [])
        self.assertEqual(parse_messages({}), [])

    def test_file_message(self):
        msgs = parse_messages(wrap([file_item("3001", "实验一 实验箱的使用.docx", "658.74 KB   已发送")]))
        m = msgs[0]
        self.assertEqual(m.kind, "file")
        self.assertEqual(m.file_name, "实验一 实验箱的使用.docx")
        self.assertEqual(m.content, "实验一 实验箱的使用.docx")
        self.assertIn("658.74 KB", m.file_size)

    def test_reply_splits_quote_from_body(self):
        msgs = parse_messages(wrap([reply_item("3002", "夜风", "test123123", "456456")]))
        m = msgs[0]
        self.assertEqual(m.kind, "reply")
        self.assertEqual(m.content, "456456")
        self.assertEqual(m.quoted_sender, "夜风")
        self.assertEqual(m.quoted_content, "test123123")

    def test_system_message_drops_action_link(self):
        msgs = parse_messages(wrap([system_item("3003", "你", "撤回了一条消息")]))
        m = msgs[0]
        self.assertEqual(m.kind, "system")
        self.assertEqual(m.content, "你撤回了一条消息")
        self.assertIsNone(m.is_self)

    def test_voice_message(self):
        msgs = parse_messages(wrap([voice_item("3004", others=False)]))
        m = msgs[0]
        self.assertEqual(m.kind, "voice")
        self.assertIn("暂不支持", m.content)
        self.assertTrue(m.is_self)


if __name__ == "__main__":
    unittest.main()
