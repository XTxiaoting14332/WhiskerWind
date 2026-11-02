"""In-memory log buffer + live tail over websocket.

The WebUI needs eyes on the daemon without shell access: Loguru records are
tee'd into a bounded deque (``LogBuffer.sink`` is installed as a loguru sink),
read back through the ``get_logs`` action, or tailed live over
``GET /logs/stream`` — a ws upgrade that replays the backlog then pushes new
records as they arrive.
"""

import json
import queue
import threading
import time
from collections import deque

LEVELS = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


class LogBuffer:
    """Bounded store of recent log records; doubles as a loguru sink."""

    def __init__(self, capacity=2000):
        self._items = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()
        self._subs = set()

    # -- writing ------------------------------------------------------------

    def sink(self, message):
        """Loguru sink entry point; receives a Message, stores its record."""
        rec = message.record
        self.push(
            level=rec["level"].name,
            message=rec["message"],
            module=rec["name"] or "",
            levelno=rec["level"].no,
            ts=rec["time"].timestamp(),
        )

    def push(self, level, message, module="", levelno=None, ts=None):
        item = {
            "seq": 0,
            "time": ts or time.time(),
            "level": level,
            "levelno": levelno if levelno is not None else LEVELS.get(level, 20),
            "module": module,
            "message": message,
        }
        with self._lock:
            self._seq += 1
            item["seq"] = self._seq
            self._items.append(item)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(dict(item))
            except queue.Full:
                pass
        return item

    # -- reading ------------------------------------------------------------

    @property
    def latest_seq(self):
        return self._seq

    def snapshot(self, after=0, tail=None, level=None):
        """Buffered records with seq > ``after``, optionally min-level/tailed."""
        min_no = LEVELS.get(str(level or "").upper(), 0)
        with self._lock:
            items = [
                dict(i)
                for i in self._items
                if i["seq"] > after and i["levelno"] >= min_no
            ]
        if tail is not None:
            items = items[-int(tail):]
        return items

    # -- live subscribers -----------------------------------------------------

    def subscribe(self, maxsize=2000):
        q = queue.Queue(maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)


def serve_log_stream(conn, logbuf, tail=200):
    """Replay ``tail`` buffered records then stream new ones until close.

    A reader thread just consumes incoming frames (close/ping); the caller's
    thread drains the subscriber queue. Exits when the peer goes away.
    """

    def reader():
        for _ in conn.messages():
            pass

    # 先订阅再回放：反过来的话，两动作之间 push 的记录两边都不沾，直接丢
    q = logbuf.subscribe()
    last = 0
    try:
        for rec in logbuf.snapshot(tail=tail):
            conn.send_text(json.dumps({"type": "log", "data": rec}, ensure_ascii=False))
            last = rec["seq"]
        threading.Thread(target=reader, daemon=True).start()
        while conn.alive:
            try:
                rec = q.get(timeout=25)
            except queue.Empty:
                try:
                    conn.ping()
                except OSError:
                    break
                continue
            if rec["seq"] <= last:
                continue  # 订阅后、回放前到达的记录已在快照里发过
            try:
                conn.send_text(
                    json.dumps({"type": "log", "data": rec}, ensure_ascii=False)
                )
            except OSError:
                break
    finally:
        logbuf.unsubscribe(q)
        conn.close()
