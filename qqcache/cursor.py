"""Incremental reading of a conversation's message list.

QQ's message ids are identities, not an ordering key: messages sent from this
client use a 777-prefixed id space while synced ones use 770, so ids interleave
out of order. Order always comes from the DOM, and de-duplication uses the id
set, which still gives exactly-once delivery.

The list is virtualised, so only the rendered window exists in the tree. A gap
is detected when the oldest visible message was never seen: that means more
messages arrived between two polls than the viewport can hold.

The gap flag is a heuristic and can false-positive: closing and reopening a
conversation (which the send path does) reloads the list at a new scroll
position, so the reloaded window may not overlap what was committed. Treat a
gap as "scroll up and re-read", not as an error.
"""

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_messages (
    uin     TEXT NOT NULL,
    msg_id  TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (uin, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_seen_uin_time ON seen_messages(uin, seen_at DESC);
"""

MAX_SEEN_PER_CONVERSATION = 2000


class Result:
    __slots__ = ("new", "gap", "first_run")

    def __init__(self, new, gap, first_run):
        self.new = new
        self.gap = gap
        self.first_run = first_run

    def __repr__(self):
        return f"Result(new={len(self.new)}, gap={self.gap}, first_run={self.first_run})"


class Cursor:
    def __init__(self, path):
        # 服务模式下 commit/filter 会跨线程发生（通知 worker、HTTP 动作线程）
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def seen(self, uin, msg_ids=None):
        if msg_ids is None:
            rows = self.db.execute(
                "SELECT msg_id FROM seen_messages WHERE uin = ?", (uin,)
            ).fetchall()
            return {r[0] for r in rows}
        known = set()
        for chunk_start in range(0, len(msg_ids), 500):
            chunk = msg_ids[chunk_start : chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT msg_id FROM seen_messages WHERE uin = ? AND msg_id IN ({marks})",
                (uin, *chunk),
            ).fetchall()
            known.update(r[0] for r in rows)
        return known

    def has_history(self, uin):
        row = self.db.execute(
            "SELECT 1 FROM seen_messages WHERE uin = ? LIMIT 1", (uin,)
        ).fetchone()
        return row is not None

    def filter_new(self, uin, messages):
        """Return the messages we have never emitted, plus a gap flag.

        On the very first read of a conversation nothing is new: replaying the
        whole visible history is almost never what a bot wants.
        """
        if not messages:
            return Result([], False, False)

        ids = [m.msg_id for m in messages]
        known = self.seen(uin, ids)
        first_run = not self.has_history(uin)
        new = [m for m in messages if m.msg_id not in known]

        gap = not first_run and messages[0].msg_id not in known
        return Result(new, gap, first_run)

    def commit(self, uin, messages):
        if not messages:
            return
        now = time.time()
        self.db.executemany(
            "INSERT OR IGNORE INTO seen_messages (uin, msg_id, seen_at) VALUES (?, ?, ?)",
            [(uin, m.msg_id, now) for m in messages],
        )
        self.db.commit()
        self._prune(uin)

    def reset(self, uin):
        self.db.execute("DELETE FROM seen_messages WHERE uin = ?", (uin,))
        self.db.commit()

    def _prune(self, uin):
        total = self.db.execute(
            "SELECT COUNT(*) FROM seen_messages WHERE uin = ?", (uin,)
        ).fetchone()[0]
        excess = total - MAX_SEEN_PER_CONVERSATION
        if excess <= 0:
            return
        self.db.execute(
            "DELETE FROM seen_messages WHERE uin = ? AND msg_id IN ("
            " SELECT msg_id FROM seen_messages WHERE uin = ? ORDER BY seen_at ASC LIMIT ?)",
            (uin, uin, excess),
        )
        self.db.commit()
