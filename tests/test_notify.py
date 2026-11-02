import os
import tempfile
import unittest

from qqcache.notify import Store, parse

# Real payloads captured from QQ 3.2.33 via dbus-monitor.
CAPTURED = [
    ("QQ", "files", "【示例】Alice：[图片]"),
    ("QQ", "files", "【示例】Alice：12312"),
    ("QQ", "【示例】Alice", "12312"),
    (
        "QQ",
        "files",
        "[有人@我] 【示例】Alice：@【示例】BotName ssh -L 9876:10.0.0.2:9876 <你的账号>@mail.example.com",
    ),
]


class ParseTest(unittest.TestCase):
    def test_group_message(self):
        event = parse(*CAPTURED[0])
        self.assertTrue(event.is_group)
        self.assertEqual(event.summary, "files")
        self.assertIsNone(event.sender_card)
        self.assertEqual(event.sender_name, "【示例】Alice")
        self.assertEqual(event.content, "[图片]")
        self.assertFalse(event.mention)
        self.assertFalse(event.truncated)

    def test_sender_display_is_the_whole_prefix(self):
        # 群发送者前缀就是显示名（有名片显示名片），不做复合拆分；
        # NT QQ 只发显示名；显示名里的【】是名字的一部分。
        event = parse(*CAPTURED[0])
        self.assertEqual(event.sender_display, "【示例】Alice")
        no_card = parse("QQ", "群", "昵称：正文", kind="group")
        self.assertEqual(no_card.sender_display, "昵称")

    def test_private_message_has_no_sender_prefix(self):
        event = parse(*CAPTURED[2])
        self.assertFalse(event.is_group)
        self.assertEqual(event.summary, "【示例】Alice")
        self.assertIsNone(event.sender_card)
        self.assertEqual(event.content, "12312")

    def test_mention_prefix(self):
        event = parse(*CAPTURED[3])
        self.assertTrue(event.mention)
        self.assertTrue(event.is_group)
        self.assertEqual(event.sender_name, "【示例】Alice")
        self.assertTrue(event.content.startswith("@【示例】BotName "))

    def test_truncation_flag(self):
        long_body = "【某人】昵称：" + "啊" * 128
        self.assertTrue(parse("QQ", "群", long_body).truncated)
        short_body = "【某人】昵称：" + "啊" * 127
        self.assertFalse(parse("QQ", "群", short_body).truncated)

    def test_private_body_starting_with_brackets(self):
        event = parse("QQ", "某人", "【不是群名】也不是：前缀")
        self.assertTrue(event.is_group)

    def test_group_without_card_uses_kind_hint(self):
        event = parse("QQ", "NoneBot 用户交流", "ai的逻辑很优美啊：@我 哦不", kind="group")
        self.assertTrue(event.is_group)
        self.assertEqual(event.sender_name, "ai的逻辑很优美啊")
        self.assertIsNone(event.sender_card)
        self.assertEqual(event.content, "@我 哦不")

    def test_private_with_colon_is_not_mistaken_for_group(self):
        event = parse("QQ", "某人", "注意：明天开会", kind="c2c")
        self.assertFalse(event.is_group)
        self.assertIsNone(event.sender_name)
        self.assertEqual(event.content, "注意：明天开会")

    def test_ambiguous_body_without_kind_stays_private(self):
        event = parse("QQ", "某人", "注意：明天开会")
        self.assertFalse(event.is_group)
        self.assertEqual(event.content, "注意：明天开会")

    def test_plain_private_body(self):
        event = parse("QQ", "某人", "在吗")
        self.assertFalse(event.is_group)
        self.assertEqual(event.content, "在吗")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "n.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_persist_and_read_back(self):
        for payload in CAPTURED:
            self.store.add(parse(*payload))
        self.assertEqual(self.store.count(), 4)
        rows = self.store.recent(10)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["summary"], "files")
        self.assertTrue(rows[0]["mention"])

    def test_since_cursor(self):
        first = self.store.add(parse(*CAPTURED[0]))
        self.store.add(parse(*CAPTURED[2]))
        newer = self.store.recent(10, since=first.row_id)
        self.assertEqual(len(newer), 1)
        self.assertEqual(newer[0]["content"], "12312")


if __name__ == "__main__":
    unittest.main()
