"""Correlate media messages with the files QQ writes into nt_data.

QQ auto-downloads images and voice notes into plain files on disk; the original
bytes of a *file* or *video* message are only fetched when the user clicks
download, so those stay unavailable without extra UI work.

    nt_data/Pic/<YYYY-MM>/Ori/<md5>.<ext>     images, original
    nt_data/Ptt/<YYYY-MM>/Ori/<md5>.amr       voice, SILK payload
    nt_data/Video/<YYYY-MM>/Thumb/*.png       thumbnails only
    nt_data/File/Thumb/*.jpg                  thumbnails only

There is no id linking a file to a message, so pairing is positional: new files
sorted by mtime are matched to media messages in DOM order.
"""

import os
import sqlite3
import time
from dataclasses import dataclass

KIND_CATEGORY = {
    "image": "image",
    "voice": "voice",
    "video": "video",
    "file": "file",
}

WATCHED = {
    "Pic": ("Ori",),
    "Ptt": ("Ori",),
    "Video": ("Thumb",),
    "File": ("Thumb",),
}

CATEGORY_OF_DIR = {"Pic": "image", "Ptt": "voice", "Video": "video", "File": "file"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_files (
    path       TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    first_seen REAL NOT NULL
);
"""


@dataclass
class Media:
    path: str
    category: str
    mtime: float
    size: int

    def as_dict(self):
        return {"path": self.path, "category": self.category, "size": self.size}


def find_nt_data(root=None):
    root = root or os.path.expanduser("~/.config/QQ")
    if not os.path.isdir(root):
        return None
    candidates = [
        os.path.join(root, name, "nt_data")
        for name in sorted(os.listdir(root))
        if name.startswith("nt_qq_")
    ]
    candidates = [c for c in candidates if os.path.isdir(c)]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


class MediaIndex:
    def __init__(self, nt_data, db_path):
        self.nt_data = nt_data
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.last_scan_was_initial = False

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _iter_files(self):
        for folder, subdirs in WATCHED.items():
            base = os.path.join(self.nt_data, folder)
            if not os.path.isdir(base):
                continue
            category = CATEGORY_OF_DIR[folder]
            for name in sorted(os.listdir(base)):
                month = os.path.join(base, name)
                if not os.path.isdir(month):
                    continue
                for sub in subdirs:
                    target = os.path.join(month, sub)
                    if not os.path.isdir(target):
                        continue
                    for entry in os.scandir(target):
                        if entry.is_file():
                            yield Media(entry.path, category, entry.stat().st_mtime, entry.stat().st_size)

    def scan(self):
        """Record everything on disk, returning only the files never seen before."""
        now = time.time()
        initial = self.db.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 0
        self.last_scan_was_initial = initial
        fresh = []
        for media in self._iter_files():
            row = self.db.execute(
                "SELECT mtime FROM media_files WHERE path = ?", (media.path,)
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO media_files (path, category, mtime, size, first_seen)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (media.path, media.category, media.mtime, media.size, now),
                )
                fresh.append(media)
            elif row[0] != media.mtime:
                self.db.execute(
                    "UPDATE media_files SET mtime = ?, size = ? WHERE path = ?",
                    (media.mtime, media.size, media.path),
                )
        self.db.commit()
        fresh.sort(key=lambda m: m.mtime)
        return fresh


def correlate(messages, media):
    """Pair media files to media messages, positionally within each category.

    Returns (links, unmatched_files). ``links`` maps msg_id -> Media.
    """
    buckets = {}
    for item in media:
        buckets.setdefault(item.category, []).append(item)
    for items in buckets.values():
        items.sort(key=lambda m: m.mtime)

    links = {}
    for message in messages:
        category = KIND_CATEGORY.get(message.kind)
        if not category:
            continue
        queue = buckets.get(category)
        if queue:
            links[message.msg_id] = queue.pop(0)

    leftover = sum(len(items) for items in buckets.values())
    return links, leftover
