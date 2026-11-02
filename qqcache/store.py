"""Persistent name -> uin binding cache.

A conversation's display name is only an index, never an identity: groups get
renamed, and two different groups can share a name. The real key is the uin,
which we only learn by asking QQ itself (context menu -> copy group number).
"""

import os
import re
import sqlite3
import time
import unicodedata

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
    uin           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'unknown',
    name          TEXT NOT NULL,
    name_clean    TEXT NOT NULL,
    first_seen    REAL NOT NULL,
    last_verified REAL NOT NULL,
    fetch_count   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS aliases (
    name      TEXT NOT NULL,
    uin       TEXT NOT NULL REFERENCES conversations(uin) ON DELETE CASCADE,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (name, uin)
);

CREATE INDEX IF NOT EXISTS idx_aliases_uin ON aliases(uin);
"""

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_UIN = re.compile(r"^\d{5,12}$")


def is_uin(text):
    return bool(text and _UIN.match(text.strip()))


def sanitize(name):
    """Strip the control characters QQ leaks into accessible names."""
    if not name:
        return ""
    return _CONTROL.sub("", name).strip()


def display_name(name):
    return unicodedata.normalize("NFC", sanitize(name))


class Cache:
    def __init__(self, path):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 通知守护的工作线程也要读写缓存（归因回填）
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def resolve(self, name):
        """Return every uin that has ever been seen under this display name."""
        rows = self.db.execute(
            "SELECT uin FROM aliases WHERE name = ? ORDER BY last_seen DESC", (name,)
        ).fetchall()
        return [r["uin"] for r in rows]

    def lookup(self, uin):
        row = self.db.execute("SELECT * FROM conversations WHERE uin = ?", (uin,)).fetchone()
        return dict(row) if row else None

    def get(self, name):
        """Resolve a name to a single uin, or None when absent or ambiguous."""
        hits = self.resolve(name)
        return hits[0] if len(hits) == 1 else None

    def ambiguous(self, name):
        return len(self.resolve(name)) > 1

    def is_fresh(self, name, ttl=None):
        hits = self.resolve(name)
        if len(hits) != 1:
            return False
        if ttl is None:
            return True
        row = self.db.execute(
            "SELECT last_seen FROM aliases WHERE name = ? AND uin = ?", (name, hits[0])
        ).fetchone()
        return bool(row) and (time.time() - row["last_seen"]) < ttl

    def record(self, name, uin, kind="unknown", now=None):
        now = now or time.time()
        clean = display_name(name)
        cur = self.db.execute("SELECT fetch_count FROM conversations WHERE uin = ?", (uin,))
        existing = cur.fetchone()
        if existing:
            self.db.execute(
                "UPDATE conversations SET name = ?, name_clean = ?, last_verified = ?,"
                " fetch_count = fetch_count + 1 WHERE uin = ?",
                (name, clean, now, uin),
            )
        else:
            self.db.execute(
                "INSERT INTO conversations (uin, kind, name, name_clean, first_seen,"
                " last_verified, fetch_count) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (uin, kind, name, clean, now, now),
            )
        self.db.execute(
            "INSERT INTO aliases (name, uin, first_seen, last_seen) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name, uin) DO UPDATE SET last_seen = excluded.last_seen",
            (name, uin, now, now),
        )
        self.db.commit()

    def conversations(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM conversations ORDER BY last_verified DESC"
            ).fetchall()
        ]

    def aliases(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM aliases ORDER BY name").fetchall()]

    def stats(self):
        convs = self.db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        alias = self.db.execute("SELECT COUNT(*) AS n FROM aliases").fetchone()["n"]
        dup = self.db.execute(
            "SELECT COUNT(*) AS n FROM (SELECT name FROM aliases GROUP BY name HAVING COUNT(*) > 1)"
        ).fetchone()["n"]
        return {"conversations": convs, "aliases": alias, "ambiguous_names": dup}
