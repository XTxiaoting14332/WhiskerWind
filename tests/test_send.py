import unittest

from qqcache.send import parse_segments


class ParseSegmentsTest(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(parse_segments("hello"), [("text", "hello")])

    def test_empty(self):
        self.assertEqual(parse_segments(""), [])

    def test_marker_only(self):
        self.assertEqual(parse_segments("{at:2345678901}"), [("at", "2345678901")])

    def test_inline_marker(self):
        self.assertEqual(
            parse_segments("回复 {at:12345} 看一下"),
            [("text", "回复 "), ("at", "12345"), ("text", " 看一下")],
        )

    def test_adjacent_markers(self):
        self.assertEqual(
            parse_segments("{at:1}{at:2}hi"),
            [("at", "1"), ("at", "2"), ("text", "hi")],
        )

    def test_trailing_marker(self):
        self.assertEqual(
            parse_segments("看{at:9}"),
            [("text", "看"), ("at", "9")],
        )


if __name__ == "__main__":
    unittest.main()
