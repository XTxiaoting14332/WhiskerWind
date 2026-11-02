import unittest
from unittest import mock

from qqcache import peer
from qqcache.peer import extract_identity


class WaitMemberCardTest(unittest.TestCase):
    """_wait_member_card 的双锚点：名字匹配，或点击后全新出现的卡。

    后者兜住 群名片≠QQ昵称——卡里显示的是 QQ 昵称，名字锚点在这种成员
    身上永远等不到；但点击后新开的卡不在 prev_all 快照里，归属仍可信。
    """

    def _wait(self, cards, texts, name, prev_all=None, timeout=0.3):
        def lookup(c):
            return texts[c]
        with mock.patch.object(peer, "_find_cards", return_value=cards), \
                mock.patch.object(peer, "card_texts", side_effect=lookup):
            return peer._wait_member_card(None, name, timeout, prev_all=prev_all)

    def test_name_anchor_accepts_zombie_same_member(self):
        # 同名僵尸卡里的 uin 本来就是对的——名字锚点不挑新旧
        card = object()
        got = self._wait([card], {card: ["Alice", "QQ号：12345"]}, "Alice")
        self.assertIs(got, card)

    def test_fresh_card_accepted_without_name(self):
        zombie, fresh = object(), object()
        got = self._wait(
            [zombie, fresh],
            {zombie: ["旧成员", "QQ号：11111"], fresh: ["夜风本尊", "QQ 22222"]},
            "群名片名",
            prev_all={("旧成员", "QQ号：11111")},
        )
        self.assertIs(got, fresh)

    def test_zombie_other_member_rejected(self):
        # 别人的僵尸卡在快照里——不新鲜也无名，不能收
        zombie = object()
        got = self._wait(
            [zombie], {zombie: ["旧成员", "QQ号：11111"]}, "甲",
            prev_all={("旧成员", "QQ号：11111")},
        )
        self.assertIsNone(got)

    def test_fresh_card_requires_digits(self):
        # 还在加载的空壳卡（无号码）不算新鲜卡
        fresh = object()
        got = self._wait([fresh], {fresh: ["加载中"]}, "甲", prev_all=set())
        self.assertIsNone(got)


class HarvestMemberCardsTest(unittest.TestCase):
    """harvest_member_cards：捡开着的成员卡，按 '群昵称' 字段归因。

    真机教训：卡可能晚于 3s 等待窗才填充完，但卡上自带群昵称——
    不管它是哪次点击开的，看到了就能认主，白捡真号。
    """

    def _harvest(self, cards_texts, names):
        cards = list(cards_texts)
        with mock.patch.object(peer, "_find_cards", return_value=cards), \
                mock.patch.object(peer, "card_texts", side_effect=cards_texts.get):
            return peer.harvest_member_cards(None, names)

    def test_gnick_field_attributes_card(self):
        card = object()
        found = self._harvest(
            {card: ["9999+", "QQ", "2185726244", "群昵称", "污！血！", "签名", "x"]},
            {"污！血！", "别人"},
        )
        self.assertEqual(found, {"污！血！": "2185726244"})

    def test_gnick_is_authoritative_over_bare_text(self):
        # 卡上别处出现了别的名字（签名/昵称撞名）也不能抢——群昵称说了算
        card = object()
        found = self._harvest(
            {card: ["QQ", "12345", "群昵称", "甲", "签名", "乙"]},
            {"乙"},
        )
        self.assertEqual(found, {})

    def test_no_gnick_falls_back_to_exact_text(self):
        # 没设群名片的成员卡上没有该字段，显示名即 QQ 昵称——精确匹配
        card = object()
        found = self._harvest(
            {card: ["昵称丙", "QQ", "12345"]},
            {"昵称丙"},
        )
        self.assertEqual(found, {"昵称丙": "12345"})

    def test_card_without_uin_skipped(self):
        card = object()
        found = self._harvest({card: ["群昵称", "甲"]}, {"甲"})
        self.assertEqual(found, {})

    def test_no_cards(self):
        self.assertEqual(self._harvest({}, {"甲"}), {})


class ExtractIdentityTest(unittest.TestCase):
    def test_group_card_with_label(self):
        uin, kind = extract_identity(["files", "群号", "100000001", "4人"])
        self.assertEqual(uin, "100000001")
        self.assertEqual(kind, "group")

    def test_group_number_on_the_label_line(self):
        uin, kind = extract_identity(["files", "群号：100000001"])
        self.assertEqual(uin, "100000001")
        self.assertEqual(kind, "group")

    def test_private_card(self):
        uin, kind = extract_identity(["【示例】Alice", "QQ号：1234567890", "在线"])
        self.assertEqual(uin, "1234567890")
        self.assertEqual(kind, "c2c")

    def test_bare_digits_without_label_still_resolve(self):
        uin, kind = extract_identity(["files", "100000001"])
        self.assertEqual(uin, "100000001")
        self.assertEqual(kind, "unknown")

    def test_prefers_labeled_number_over_bare_digits(self):
        # 等级、成员数之类的裸数字不能赢过带标签的号码
        uin, kind = extract_identity(["100", "群号：100000001"])
        self.assertEqual(uin, "100000001")

    def test_message_like_digits_do_not_leak_in(self):
        # 消息正文样式的数字不在资料卡文本里；万一混入，短数字不敌完整 uin
        uin, kind = extract_identity(["123123", "QQ号：1234567890"])
        self.assertEqual(uin, "1234567890")

    def test_no_number(self):
        uin, kind = extract_identity(["临时会话", "对方设置了权限"])
        self.assertIsNone(uin)
        self.assertEqual(kind, "unknown")

    def test_group_number_before_member_count(self):
        # 群卡结构: <群号> (N人) —— 群介绍里出现的其他号码不能赢
        uin, kind = extract_identity(
            ["738677145", "(1876人)", "游戏", "若本群已满请添加四群：749440456"]
        )
        self.assertEqual(uin, "738677145")
        self.assertEqual(kind, "group")

    def test_qq_label_then_number(self):
        # 私聊卡结构: ... QQ 222222222 ... —— 号码在标签之后
        uin, kind = extract_identity(["我不知道", "QQ", "222222222", "女"])
        self.assertEqual(uin, "222222222")
        self.assertEqual(kind, "c2c")


class NameMatchTest(unittest.TestCase):
    def test_truncated_row_matches_full_name(self):
        from qqcache.send import _names_match

        self.assertTrue(
            _names_match("Linux&开源技... (145人)", "Linux&开源技术交流群")
        )
        self.assertTrue(_names_match("files (4人)", "files"))
        self.assertTrue(_names_match("files", "files (4)"))

    def test_unrelated_names_do_not_match(self):
        from qqcache.send import _names_match

        self.assertFalse(_names_match("files (4人)", "CainithmBot游戏群"))
        self.assertFalse(_names_match("", "files"))
        self.assertFalse(_names_match("李廷瑶", "files"))


if __name__ == "__main__":
    unittest.main()
