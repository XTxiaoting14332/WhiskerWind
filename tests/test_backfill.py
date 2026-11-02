import unittest

from qqcache.backfill import merge_recovered
from qqcache.messages import Message


def msg(msg_id, content=""):
    return Message(
        msg_id=msg_id,
        kind="text",
        sender="s",
        title=None,
        content=content,
        timestamp=None,
        is_self=False,
    )


class MergeRecoveredTest(unittest.TestCase):
    def test_prepends_newer_than_seen(self):
        seen = {"a", "b"}
        emitted = set()
        recovered = merge_recovered([], [msg("a"), msg("b"), msg("c")], seen, emitted)
        self.assertEqual([m.msg_id for m in recovered], ["c"])

    def test_later_frames_go_first(self):
        seen = {"a"}
        emitted = set()
        recovered = merge_recovered([], [msg("b"), msg("c")], seen, emitted)
        recovered = merge_recovered(recovered, [msg("b"), msg("a")], seen, emitted)
        # frame two's only unseen item is none — "a" is seen, "b" already emitted
        self.assertEqual([m.msg_id for m in recovered], ["b", "c"])

    def test_overlap_dedupes(self):
        seen = set()
        emitted = set()
        recovered = merge_recovered([], [msg("x"), msg("y"), msg("z")], seen, emitted)
        recovered = merge_recovered(recovered, [msg("w"), msg("x")], seen, emitted)
        self.assertEqual([m.msg_id for m in recovered], ["w", "x", "y", "z"])


if __name__ == "__main__":
    unittest.main()
