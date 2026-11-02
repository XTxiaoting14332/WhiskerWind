"""OneBot v11 protocol surface.

Two halves: a codec that translates between our parsed ``Message`` objects /
composer ops and OneBot message formats (CQ-code strings and segment arrays),
and the transport — an HTTP API server for actions plus an HTTP POST emitter
for events, the same pair go-cqhttp exposed so nonebot/koishi-style clients
can connect with no custom glue.

Identity is the one thing this layer cannot fully fake: group messages carry
only a display name, not the sender's uin. Until a member is resolved through
some GUI path they get a stable synthetic user_id in the 9e11+ range, which
keeps permission checks and per-user state consistent even if the number is
not a real QQ account.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

SYN_BASE = 900_000_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS ob11_messages (
    ob_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_uin    TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'unknown',
    msg_id      TEXT,
    direction   TEXT NOT NULL DEFAULT 'in',
    sender_uin  TEXT,
    sender_name TEXT,
    role        TEXT,
    message     TEXT NOT NULL,
    raw         TEXT NOT NULL,
    post_time   REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ob11_conv_msg
    ON ob11_messages(conv_uin, msg_id) WHERE msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ob11_time ON ob11_messages(post_time DESC);

CREATE TABLE IF NOT EXISTS ob11_syn (
    scope   TEXT NOT NULL,
    name    TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (scope, name)
);

CREATE TABLE IF NOT EXISTS ob11_members (
    group_uin TEXT NOT NULL,
    name      TEXT NOT NULL,
    user_id   INTEGER NOT NULL,
    real_uin  TEXT,
    role      TEXT,
    -- ambiguous：该名在成员列表里有多个持有人——按名分不出人，保持合成号
    -- 让每条消息都走自己的头像归因。由成员列表 count>1 标记，持久化。
    ambiguous INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (group_uin, name)
);

-- uin 键身份：real_uin 才是成员本体，name 只是它最近被看到的显示名。
-- 同名成员各占一行（uin 区分）；改名成员新旧名都索引到同一 uin。
CREATE TABLE IF NOT EXISTS ob11_member_uins (
    group_uin TEXT NOT NULL,
    real_uin  TEXT NOT NULL,
    name      TEXT NOT NULL,
    role      TEXT,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (group_uin, real_uin)
);
CREATE TABLE IF NOT EXISTS ob11_member_names (
    group_uin TEXT NOT NULL,
    name      TEXT NOT NULL,
    real_uin  TEXT NOT NULL,
    -- verified_at：该 名↔号 绑定最近一次 GUI 验证的时间戳。
    -- NULL = 旧数据/未经新体系验证；member_user_id 的 max_age 窗口按它判鲜
    verified_at REAL,
    PRIMARY KEY (group_uin, name, real_uin)
);
-- #ww 触发器去重：同一会话同一消息只回复一次。崩溃恢复回放/重 poll
-- 会重复看到同一消息（msg_id 可能跨渲染器重生变化），没有这张表就会
-- 把回复连发好几条。
CREATE TABLE IF NOT EXISTS ob11_ww_replied (
    conv_uin TEXT NOT NULL,
    msg_id   TEXT NOT NULL,
    replied_at REAL NOT NULL,
    PRIMARY KEY (conv_uin, msg_id)
);
"""


# ---------------------------------------------------------------------------
# CQ codec

_CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_]+)([^\[\]]*)\]")


def cq_escape(text, in_param=False):
    out = (
        (text or "")
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )
    if in_param:
        out = out.replace(",", "&#44;")
    return out


def cq_unescape(text):
    return (
        (text or "")
        .replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def cq_segment(seg_type, **params):
    kv = ",".join(f"{k}={cq_escape(str(v), in_param=True)}" for k, v in params.items())
    return f"[CQ:{seg_type}{',' + kv if kv else ''}]"


def parse_cq(message):
    """CQ string -> [(type, {param: value}), ...]; text parts as ("text", {"text": s})."""
    out = []
    last = 0
    for m in _CQ_RE.finditer(message or ""):
        if m.start() > last:
            out.append(("text", {"text": cq_unescape(message[last : m.start()])}))
        params = {}
        tail = m.group(2).lstrip(",")
        for pair in tail.split(",") if tail else []:
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = cq_unescape(v)
        out.append((m.group(1), params))
        last = m.end()
    if last < len(message or ""):
        out.append(("text", {"text": cq_unescape(message[last:])}))
    return out


def normalize_segments(message):
    """Accept either a CQ string or a OneBot segment array; emit (type, data)."""
    if message is None:
        return []
    if isinstance(message, str):
        return parse_cq(message)
    if isinstance(message, dict):
        message = [message]
    out = []
    for seg in message:
        seg_type = seg.get("type")
        data = {k: str(v) for k, v in (seg.get("data") or {}).items()}
        if seg_type:
            out.append((seg_type, data))
    return out


def segments_to_cq(segments):
    """(type, data) list -> CQ string, for archiving what we sent. Inline
    base64 payloads are dropped (the archive only needs to know it was an
    image, not carry megabytes of it)."""
    out = []
    for seg_type, data in segments:
        if seg_type == "text":
            out.append(cq_escape(data.get("text", "")))
            continue
        data = {k: ("base64://" if str(v).startswith("base64://") else v)
                for k, v in data.items()}
        out.append(cq_segment(seg_type, **data))
    return "".join(out)


# Segments we can only degrade to a text placeholder — sending them as text is
# friendlier than failing the whole message for a cosmetic emoji.
DEGRADED = {
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "share": "[链接分享]",
    "location": "[位置]",
    "contact": "[名片]",
    "music": "[音乐]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "bface": "[表情]",
    "sface": "[表情]",
}


def ops_from_segments(segments, resolve_file):
    """OneBot segments -> composer ops for send.send().

    Returns (ops, reply_ob_id_or_None). ``resolve_file`` maps an image ``file``
    value to a local path; unsupported-but-structural types raise ApiError.
    """
    ops = []
    reply_id = None
    for seg_type, data in segments:
        if seg_type == "text":
            ops.append(("text", data.get("text", "")))
        elif seg_type == "at":
            qq = data.get("qq", "")
            if qq and qq != "all" and not qq.lstrip("-").isdigit():
                raise ApiError(1400, f"@ 目标的 qq 参数无效: {qq!r}")
            ops.append(("at", qq or "all"))
        elif seg_type == "reply":
            reply_id = data.get("id")
        elif seg_type == "image":
            f = data.get("file") or data.get("url")
            if not f:
                raise ApiError(1400, "image 段缺少 file/url")
            ops.append(("image", resolve_file(f)))
        elif seg_type in DEGRADED:
            ops.append(("text", DEGRADED[seg_type]))
        else:
            raise ApiError(1400, f"不支持的消息段类型: {seg_type!r}")
    return ops, reply_id


# ---------------------------------------------------------------------------
# Message -> CQ string


def _title_role(title):
    if not title:
        return "member"
    if "群主" in title:
        return "owner"
    if "管理员" in title:
        return "admin"
    return "member"


def message_to_cq(msg, reply_ob_id=None, media_path=None):
    """Render a parsed Message as a CQ-code string; '' means 'don't emit'."""
    parts = []
    if msg.kind == "reply" and reply_ob_id is not None:
        parts.append(cq_segment("reply", id=reply_ob_id))

    if msg.kind == "system" or msg.kind == "unknown" and not msg.content.strip():
        return ""
    if msg.kind == "image":
        if media_path:
            parts.append(
                cq_segment(
                    "image",
                    file=os.path.basename(media_path),
                    url="file://" + media_path,
                )
            )
        else:
            parts.append("[CQ:image]")
    elif msg.kind == "voice":
        parts.append(
            cq_segment("record", file="file://" + media_path) if media_path else "[语音]"
        )
    elif msg.kind == "video":
        parts.append(
            cq_segment("video", file="file://" + media_path) if media_path else "[视频]"
        )
    elif msg.kind == "file":
        label = msg.file_name or msg.content or "文件"
        parts.append(cq_escape(f"[文件] {label}" + (f" ({msg.file_size})" if msg.file_size else "")))
    elif msg.kind == "forward":
        parts.append(cq_escape("[聊天记录]\n" + msg.content))
    elif msg.kind == "card":
        parts.append(cq_escape(msg.content or "[卡片消息]"))
    else:
        parts.append(cq_escape(msg.content))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Id map: OneBot int message_id <-> (conv_uin, msg_id); synthetic user ids


class IdMap:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:  # 旧库迁移：已有表没这列，补列；新库 CREATE 已带列则跳过
            self.db.execute(
                "ALTER TABLE ob11_members ADD COLUMN ambiguous INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:  # 旧库迁移：名↔号绑定补验证时间戳（缓存窗口判鲜用）
            self.db.execute(
                "ALTER TABLE ob11_member_names ADD COLUMN verified_at REAL"
            )
        except sqlite3.OperationalError:
            pass
        self.db.commit()
        self._lock = threading.Lock()

    def close(self):
        self.db.close()

    def _syn_id(self, scope, name):
        row = self.db.execute(
            "SELECT user_id FROM ob11_syn WHERE scope = ? AND name = ?", (scope, name)
        ).fetchone()
        if row:
            return int(row["user_id"])
        row = self.db.execute("SELECT MAX(user_id) AS m FROM ob11_syn").fetchone()
        user_id = max(int(row["m"] or SYN_BASE), SYN_BASE) + 1
        self.db.execute(
            "INSERT INTO ob11_syn (scope, name, user_id) VALUES (?, ?, ?)",
            (scope, name, user_id),
        )
        self.db.commit()
        return user_id

    def mark_member_ambiguous(self, group_uin, name):
        """打歧义标记：成员列表里这个名字有多个持有人。"""
        row = self.db.execute(
            "SELECT user_id FROM ob11_members WHERE group_uin = ? AND name = ?",
            (group_uin, name),
        ).fetchone()
        if row and int(row["user_id"]) < SYN_BASE:
            # 历史数据：user_id 被真号占着，分配合成号还给它（槽位必须合成）
            syn = self._syn_id("gm", f"{group_uin}:{name}")
            self.db.execute(
                "UPDATE ob11_members SET user_id = ? WHERE group_uin = ? AND name = ?",
                (syn, group_uin, name),
            )
        self.db.execute(
            "UPDATE ob11_members SET ambiguous = 1 WHERE group_uin = ? AND name = ?",
            (group_uin, name),
        )
        self.db.commit()

    def member_is_ambiguous(self, group_uin, name):
        """歧义名判定：成员列表多持有人（flag）或历史上解出过多个 uin。"""
        row = self.db.execute(
            "SELECT ambiguous FROM ob11_members WHERE group_uin = ? AND name = ?",
            (group_uin, name),
        ).fetchone()
        if row and row["ambiguous"]:
            return True
        return len(self.member_uins_for_name(group_uin, name)) > 1

    def member_uins_for_name(self, group_uin, name):
        """Distinct real uins ever attributed to this display name in the group.

        同名成员返回多个——名字在这种成员身上是歧义的，不能据此定号。"""
        rows = self.db.execute(
            "SELECT real_uin FROM ob11_member_names WHERE group_uin = ? AND name = ?",
            (group_uin, name),
        ).fetchall()
        return [r["real_uin"] for r in rows]

    def record_member_uin(self, group_uin, name, real_uin, role=None):
        """Attribute a real uin to a display name.

        名↔号多对多：同名成员各记一条（名字从此歧义），改名成员新旧名同指
        一个 uin（uin 表里的 name 字段更新为最新看到的显示名）。
        """
        now = time.time()
        # 旧库单号绑定播种进索引，否则同名第二号解出时歧义检测会漏掉第一个
        legacy = self.db.execute(
            "SELECT real_uin FROM ob11_members WHERE group_uin = ? AND name = ?",
            (group_uin, name),
        ).fetchone()
        if legacy and legacy["real_uin"]:
            self.db.execute(
                "INSERT OR IGNORE INTO ob11_member_names (group_uin, name, real_uin)"
                " VALUES (?, ?, ?)",
                (group_uin, name, legacy["real_uin"]),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO ob11_member_uins"
                " (group_uin, real_uin, name, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?)",
                (group_uin, legacy["real_uin"], name, now, now),
            )
        self.db.execute(
            "INSERT INTO ob11_member_names (group_uin, name, real_uin, verified_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(group_uin, name, real_uin) DO UPDATE SET"
            " verified_at = excluded.verified_at",
            (group_uin, name, str(real_uin), now),
        )
        self.db.execute(
            "INSERT INTO ob11_member_uins (group_uin, real_uin, name, role, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(group_uin, real_uin) DO UPDATE SET"
            " name = excluded.name, role = COALESCE(excluded.role, role),"
            " last_seen = excluded.last_seen",
            (group_uin, str(real_uin), name, role, now, now),
        )
        # 名字行只在该名无歧义时落单号；歧义时留 NULL 防止按名错归
        uins = self.member_uins_for_name(group_uin, name)
        self.db.execute(
            "UPDATE ob11_members SET real_uin = ?, role = COALESCE(?, role), last_seen = ?"
            " WHERE group_uin = ? AND name = ?",
            (str(real_uin) if len(uins) == 1 else None, role, now, group_uin, name),
        )
        self.db.commit()

    def _member_binding_fresh(self, group_uin, name, real_uin, max_age, now):
        """名↔号绑定在 max_age 窗口内才算新鲜；max_age=None 永远新鲜。"""
        if max_age is None:
            return True
        row = self.db.execute(
            "SELECT verified_at FROM ob11_member_names"
            " WHERE group_uin = ? AND name = ? AND real_uin = ?",
            (group_uin, name, str(real_uin)),
        ).fetchone()
        return bool(
            row and row["verified_at"] and row["verified_at"] >= now - max_age
        )

    def member_user_id(self, group_uin, name, role=None, real_uin=None,
                       max_age=None):
        """Stable user_id for a group member. Real uins replace synthetics.

        歧义名（解出过多个不同 uin）返回该名的稳定合成号——按名分不出人，
        每条消息的发送者得靠它自己的消息锚（头像卡）单独归因。

        max_age（秒）：名↔号绑定的缓存窗口。绑定的 verified_at 超出窗口
        视为过期——返回合成号，调用方据此把名字放进 pending 重新 GUI 验证。
        None = 窗口关闭（成员列表/历史等非归因路径照旧认最后已知绑定）。"""
        now = time.time()
        row = self.db.execute(
            "SELECT * FROM ob11_members WHERE group_uin = ? AND name = ?",
            (group_uin, name),
        ).fetchone()
        if row is None:
            # user_id 永远是合成号槽位——歧义/未解时退回它；真号经索引返回
            user_id = self._syn_id("gm", f"{group_uin}:{name}")
            self.db.execute(
                "INSERT INTO ob11_members (group_uin, name, user_id, real_uin, role, first_seen, last_seen)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (group_uin, name, user_id, role, now, now),
            )
            self.db.commit()
            row = {"user_id": user_id, "real_uin": None, "role": role, "ambiguous": 0}
        if real_uin is not None:
            self.record_member_uin(group_uin, name, real_uin, role)
        if row["ambiguous"]:
            return int(row["user_id"])  # 歧义名：永保合成号，逐条归因
        uins = self.member_uins_for_name(group_uin, name)
        if len(uins) == 1 and self._member_binding_fresh(
            group_uin, name, uins[0], max_age, now
        ):
            return int(uins[0])
        if not uins and row["real_uin"] and max_age is None:
            # 旧库数据：uin 索引还没建起来的单号绑定；窗口模式下视为过期
            return int(row["real_uin"])
        if role and role != row["role"]:
            self.db.execute(
                "UPDATE ob11_members SET role = ?, last_seen = ?"
                " WHERE group_uin = ? AND name = ?",
                (role, now, group_uin, name),
            )
            self.db.commit()
        user_id = int(row["user_id"])
        if user_id < SYN_BASE:
            # 旧数据真号占着槽位——没走到真号返回（歧义/过期/无索引）时它是
            # 未验证声明，换成合成槽，别把过时绑定当身份发出去
            user_id = self._syn_id("gm", f"{group_uin}:{name}")
            self.db.execute(
                "UPDATE ob11_members SET user_id = ?"
                " WHERE group_uin = ? AND name = ?",
                (user_id, group_uin, name),
            )
            self.db.commit()
        return user_id

    def conv_user_id(self, conv_uin, name):
        """user_id for a private peer: the real uin when known, else synthetic."""
        if conv_uin and str(conv_uin).isdigit():
            return int(conv_uin)
        return self._syn_id("conv", name or str(conv_uin))

    def members(self, group_uin):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM ob11_members WHERE group_uin = ? ORDER BY last_seen DESC",
                (group_uin,),
            ).fetchall()
        ]

    def member_info(self, group_uin, user_id):
        row = self.db.execute(
            "SELECT * FROM ob11_members WHERE group_uin = ? AND (user_id = ? OR real_uin = ?)",
            (group_uin, int(user_id), str(user_id)),
        ).fetchone()
        return dict(row) if row else None

    def record_message(self, conv_uin, kind, msg_id, direction, sender_uin,
                       sender_name, role, message, raw):
        """Register an emitted/sent message; returns its ob_id (deduped)."""
        with self._lock:
            if msg_id is not None:
                row = self.db.execute(
                    "SELECT ob_id FROM ob11_messages WHERE conv_uin = ? AND msg_id = ?",
                    (conv_uin, msg_id),
                ).fetchone()
                if row:
                    return int(row["ob_id"])
            cur = self.db.execute(
                "INSERT INTO ob11_messages (conv_uin, kind, msg_id, direction, sender_uin,"
                " sender_name, role, message, raw, post_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conv_uin, kind, msg_id, direction, sender_uin, sender_name, role,
                 message, raw, time.time()),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def get(self, ob_id):
        row = self.db.execute(
            "SELECT * FROM ob11_messages WHERE ob_id = ?", (int(ob_id),)
        ).fetchone()
        return dict(row) if row else None

    def by_msg_id(self, conv_uin, msg_id):
        """Row for QQ's native msg_id within a conversation (None if unknown)."""
        row = self.db.execute(
            "SELECT * FROM ob11_messages WHERE conv_uin = ? AND msg_id = ?",
            (str(conv_uin), str(msg_id)),
        ).fetchone()
        return dict(row) if row else None

    def history(self, conv_uin, before_id=0, limit=20):
        """Stored messages for a conversation, newest first.

        Covers only what the service emitted or sent since startup — it is a
        window into the event stream, not the QQ history DB.
        """
        rows = self.db.execute(
            "SELECT * FROM ob11_messages WHERE conv_uin = ? AND (? <= 0 OR ob_id < ?)"
            " ORDER BY ob_id DESC LIMIT ?",
            (conv_uin, int(before_id), int(before_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_msg_ids_by_sender(self, conv_uin, sender_uin, limit=5):
        """该成员在会话里最近几条消息的 QQ msg_id（ml-item 定位用），新→旧。"""
        rows = self.db.execute(
            "SELECT msg_id FROM ob11_messages WHERE conv_uin = ? AND sender_uin = ?"
            " AND msg_id IS NOT NULL ORDER BY ob_id DESC LIMIT ?",
            (str(conv_uin), str(sender_uin), int(limit)),
        ).fetchall()
        return [r["msg_id"] for r in rows]

    def member_name(self, conv_uin, uin):
        """real_uin 或合成 user_id → 成员显示名（uin 映射的兜底锚）。"""
        row = self.db.execute(
            "SELECT name FROM ob11_members WHERE group_uin = ? AND (real_uin = ? OR user_id = ?)",
            (str(conv_uin), str(uin), str(uin)),
        ).fetchone()
        return row["name"] if row else None

    def recent_msg_ids_by_sender_name(self, conv_uin, sender_name, limit=5):
        rows = self.db.execute(
            "SELECT msg_id FROM ob11_messages WHERE conv_uin = ? AND sender_name = ?"
            " AND msg_id IS NOT NULL ORDER BY ob_id DESC LIMIT ?",
            (str(conv_uin), sender_name, int(limit)),
        ).fetchall()
        return [r["msg_id"] for r in rows]

    def latest_ob_id(self, conv_uin=None):
        row = self.db.execute(
            "SELECT MAX(ob_id) AS m FROM ob11_messages" + (" WHERE conv_uin = ?" if conv_uin else ""),
            (conv_uin,) if conv_uin else (),
        ).fetchone()
        return int(row["m"] or 0)

    def guess_reply_id(self, conv_uin, quoted_sender, quoted_content, window=300):
        """Best-effort: which emitted message does this quote point at?

        QQ's reply chip exposes only the sender's display name and a text
        excerpt — no msg_id — so match the most recent same-sender message
        whose raw text starts with the excerpt.
        """
        if not quoted_content:
            return None
        rows = self.db.execute(
            "SELECT ob_id, sender_name, raw FROM ob11_messages WHERE conv_uin = ?"
            " ORDER BY ob_id DESC LIMIT ?",
            (conv_uin, window),
        ).fetchall()
        for r in rows:
            if quoted_sender and r["sender_name"] and r["sender_name"] != quoted_sender:
                continue
            raw = r["raw"] or ""
            if raw.startswith(quoted_content) or quoted_content.startswith(raw.rstrip()):
                return int(r["ob_id"])
        return None

    def ww_replied(self, conv_uin, msg_id):
        """#ww 去重：该会话该消息是否已回复过。"""
        if not msg_id:
            return False
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM ob11_ww_replied WHERE conv_uin = ? AND msg_id = ?",
                (str(conv_uin), str(msg_id)),
            ).fetchone()
            return row is not None

    def mark_ww_replied(self, conv_uin, msg_id):
        if not msg_id:
            return
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO ob11_ww_replied(conv_uin, msg_id, replied_at)"
                " VALUES (?, ?, ?)",
                (str(conv_uin), str(msg_id), time.time()),
            )
            self.db.commit()


# ---------------------------------------------------------------------------
# Event construction


class ApiError(RuntimeError):
    def __init__(self, retcode, message):
        super().__init__(message)
        self.retcode = retcode


def build_message_event(msg, conv_uin, kind, self_id, ids, message_cq, ob_id,
                        user_id=None, now=None):
    is_group = kind == "group"
    sender_name = msg.sender or ""
    role = _title_role(msg.title)
    if msg.is_self:
        user_id = int(self_id or 0)
    elif user_id is not None:
        # 调用方已归因（_msg_uins 逐条锚定 / 缓存窗口判鲜）——别再按名重查
        user_id = int(user_id)
    elif is_group:
        user_id = ids.member_user_id(str(conv_uin), sender_name or "?", role=role)
    else:
        user_id = ids.conv_user_id(str(conv_uin), sender_name)

    sender = {
        "user_id": user_id,
        "nickname": sender_name,
        "sex": "unknown",
        "age": 0,
    }
    if is_group:
        sender.update({"card": "", "area": "", "level": "", "role": role, "title": msg.title or ""})

    event = {
        "time": int(now or time.time()),
        "self_id": int(self_id or 0),
        "post_type": "message_sent" if msg.is_self else "message",
        "message_type": "group" if is_group else "private",
        "sub_type": "normal" if is_group else "friend",
        "message_id": ob_id,
        "user_id": user_id,
        "message": message_cq,
        "raw_message": msg.content,
        "font": 0,
        "sender": sender,
        # qqcache extras: harmless to standard clients, useful for debugging
        "qqcache_msg_id": msg.msg_id,
        "qqcache_kind": msg.kind,
    }
    if is_group:
        event["group_id"] = int(conv_uin) if str(conv_uin).isdigit() else ids._syn_id("conv", str(conv_uin))
    if msg.is_self:
        # messages we sent: the "sender" is the account, target is the peer
        event["target_id"] = int(conv_uin) if str(conv_uin).isdigit() else user_id
        event["sender"]["nickname"] = sender_name
    return event


# ---------------------------------------------------------------------------
# File resolution for outbound image segments


def resolve_file_ref(ref, workdir=None):
    """Map an OneBot file value to a local path the clipboard can read."""
    if ref.startswith("base64://"):
        data = base64.b64decode(ref[len("base64://"):])
        fd, path = tempfile.mkstemp(suffix=".png", dir=workdir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path
    if ref.startswith("file://"):
        return urlparse(ref).path
    if ref.startswith("http://") or ref.startswith("https://"):
        req = urllib.request.Request(ref, headers={"User-Agent": "qqcache/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        suffix = os.path.splitext(urlparse(ref).path)[1] or ".png"
        fd, path = tempfile.mkstemp(suffix=suffix, dir=workdir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path
    return ref


# ---------------------------------------------------------------------------
# Transport: HTTP API server + event POST


def ok(data=None, echo=None):
    resp = {"status": "ok", "retcode": 0, "data": data}
    if echo is not None:
        resp["echo"] = echo
    return resp


def failed(retcode, message, echo=None):
    resp = {"status": "failed", "retcode": retcode, "data": None, "message": message}
    if echo is not None:
        resp["echo"] = echo
    return resp


class Poster:
    """HTTP POST event emitter (go-cqhttp's post_url mechanism)."""

    def __init__(self, urls, self_id=0, token=None, secret=None, timeout=5):
        self.urls = list(urls)
        self.self_id = int(self_id or 0)
        self.token = token
        self.secret = secret
        self.timeout = timeout

    def post(self, event):
        if not self.urls:
            return
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Self-ID": str(self.self_id),
            "User-Agent": "qqcache-ob11/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.secret:
            headers["X-Signature"] = "sha1=" + hmac.new(
                self.secret.encode(), body, hashlib.sha1
            ).hexdigest()
        for url in self.urls:
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp.read()
            except Exception as exc:
                from loguru import logger

                logger.warning(f"事件上报 {url} 失败: {exc}")


def ws_request_dispatch(dispatch, text):
    """OneBot ws api call: {"action","params","echo"} -> response dict."""
    try:
        req = json.loads(text)
    except json.JSONDecodeError:
        return failed(1400, "请求不是合法 JSON")
    action = (req.get("action") or "").rstrip()
    for suffix in ("_async", "_rate_limited"):
        if action.endswith(suffix):
            action = action[: -len(suffix)]
    params = req.get("params") or {}
    echo = req.get("echo")
    try:
        return ok(dispatch(action, params), echo)
    except ApiError as exc:
        return failed(exc.retcode, str(exc), echo)
    except Exception as exc:  # noqa: BLE001
        return failed(1400, f"{type(exc).__name__}: {exc}", echo)


class WsHub:
    """Registry of live ws conns; events broadcast, api calls dispatched."""

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.conns = set()
        self._lock = threading.Lock()

    def add(self, conn):
        with self._lock:
            self.conns.add(conn)

    def discard(self, conn):
        with self._lock:
            self.conns.discard(conn)

    def broadcast(self, event):
        text = json.dumps(event, ensure_ascii=False)
        with self._lock:
            conns = list(self.conns)
        for conn in conns:
            try:
                conn.send_text(text)
            except OSError:
                self.discard(conn)

    def serve_conn(self, conn):
        """Reader loop: answer api requests until the peer goes away."""
        self.add(conn)
        try:
            for text in conn.messages():
                resp = ws_request_dispatch(self.dispatch, text)
                try:
                    conn.send_text(json.dumps(resp, ensure_ascii=False))
                except OSError:
                    break
        finally:
            conn.alive = False
            self.discard(conn)
            try:
                conn.sock.close()
            except OSError:
                pass


def reverse_ws_loop(url, hub, self_id=0, token=None, stop=None, retry_delay=3.0,
                    on_conn=None):
    """Reverse-WS client: connect out to the framework's universal endpoint.

    go-cqhttp convention: one conn carries both events (us -> them) and api
    calls (them -> us), announced via the X-Client-Role: Universal header.
    Reconnects forever until ``stop`` is set. ``on_conn`` is invoked with the
    live WsConn after each connect and with None after it drops, so callers
    can track (and force-close) the current connection.
    """
    from . import ws11
    from loguru import logger

    headers = {
        "X-Self-ID": str(self_id),
        "X-Client-Role": "Universal",
        "User-Agent": "qqcache-ob11/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    host = (urlparse(url).hostname or "").lower()
    hint = ("（bridge 网络下 127.0.0.1 指容器自己，宿主机上的框架请用 "
            "host.docker.internal 并监听 0.0.0.0；host 网络不受影响）"
            if host in ("127.0.0.1", "localhost", "::1") else "")
    fails = 0
    while stop is None or not stop.is_set():
        try:
            conn = ws11.connect(url, headers=headers)
        except Exception as exc:
            # 下游没开时每 3s 一次失败——只在第一次和之后每 20 次记一条，
            # 不然日志被刷满（实测几小时上千行）
            if fails % 20 == 0:
                logger.warning(f"反向 WS 连接 {url} 失败: {exc}，每 {retry_delay}s 重试"
                               + (f"（已连续失败 {fails + 1} 次）" if fails else "") + hint)
            fails += 1
            if stop is not None and stop.wait(retry_delay):
                return
            continue
        fails = 0
        logger.info(f"反向 WS 已连接 {url}")
        if on_conn is not None:
            on_conn(conn)
        try:
            hub.serve_conn(conn)
        except Exception as exc:
            logger.warning(f"反向 WS {url} 断开: {exc}")
        finally:
            if on_conn is not None:
                on_conn(None)
        if stop is not None and stop.wait(retry_delay):
            return


def _safe_media_path(media_root, path):
    """Resolve ``path`` only if it lands inside ``media_root`` (else None)."""
    if not media_root or not path:
        return None
    root = os.path.realpath(media_root)
    real = os.path.realpath(path)
    if real != root and not real.startswith(root + os.sep):
        return None
    return real if os.path.isfile(real) else None


def make_api_handler(dispatch, access_token=None, ws_hub=None, media_root=None,
                     routes=None, log_buffer=None):
    """Build a BaseHTTPRequestHandler bound to ``dispatch(action, params)``.

    With ``ws_hub``, GET requests carrying a websocket Upgrade are handed to
    the hub instead — one port serves HTTP api and forward ws both.
    ``GET /logs/stream`` + Upgrade tails ``log_buffer`` live (token required).
    With ``media_root``, ``GET /media?path=...`` streams files under it.
    ``routes`` maps additional GET paths to callables taking the query dict
    and returning ``(status, content_type, body_bytes)`` or None.
    """

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "qqcache-ob11/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            from loguru import logger

            logger.debug("http: " + (fmt % a))

        def _auth_ok(self, query):
            if not access_token:
                return True
            token = dict(parse_qsl(query)).get("access_token")
            if token == access_token:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {access_token}"

        def _maybe_ws(self):
            if "websocket" not in (self.headers.get("upgrade") or "").lower():
                return False
            from . import ws11

            parsed = urlparse(self.path)
            if parsed.path == "/logs/stream":
                if log_buffer is None:
                    self._respond(404, failed(1404, "日志流未启用"))
                    return True
                if not access_token:
                    self._respond(403, failed(1403, "日志流需要配置 --access-token"))
                    return True
                target = "logs"
            elif ws_hub is not None:
                target = "hub"
            else:
                return False
            if not self._auth_ok(parsed.query):
                self._respond(401, failed(1403, "access_token 校验失败"))
                return True
            key = self.headers.get("sec-websocket-key")
            if not key:
                self._respond(400, failed(1400, "缺少 Sec-WebSocket-Key"))
                return True
            self.connection.sendall(ws11.accept_response(key))
            buffered = b""
            try:
                import select

                # peek() 在缓冲为空时会阻塞读——只听不发的客户端会卡死在这；
                # 先 select 确认有已缓冲/在途字节再 peek
                if select.select([self.connection], [], [], 0)[0]:
                    buffered = self.rfile.peek(4096) or b""
            except (AttributeError, OSError):
                pass
            self.close_connection = True
            conn = ws11.WsConn(self.connection, buffered=buffered)
            if target == "logs":
                from .logbuf import serve_log_stream

                tail = int(dict(parse_qsl(parsed.query)).get("tail") or 200)
                serve_log_stream(conn, log_buffer, tail=tail)
            else:
                ws_hub.serve_conn(conn)
            return True

        def _maybe_media(self):
            parsed = urlparse(self.path)
            if parsed.path != "/media" or media_root is None:
                return False
            if not self._auth_ok(parsed.query):
                self._respond(401, failed(1403, "access_token 校验失败"))
                return True
            path = _safe_media_path(media_root, dict(parse_qsl(parsed.query)).get("path"))
            if path is None:
                self._respond(404, failed(1404, "文件不存在或不在媒体目录内"))
                return True
            import mimetypes

            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._respond(404, failed(1404, "文件不可读"))
                return True
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def _maybe_route(self):
            parsed = urlparse(self.path)
            handler = (routes or {}).get(parsed.path)
            if handler is None:
                return False
            if not self._auth_ok(parsed.query):
                self._respond(401, failed(1403, "access_token 校验失败"))
                return True
            result = handler(dict(parse_qsl(parsed.query)))
            if result is None:
                self._respond(404, failed(1404, "不可用"))
                return True
            status, ctype, body = result
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def _handle(self):
            if self.command == "GET" and self._maybe_ws():
                return
            parsed = urlparse(self.path)
            if self.command == "GET" and self._maybe_media():
                return
            if self.command == "GET" and self._maybe_route():
                return
            if not self._auth_ok(parsed.query):
                return self._respond(401, failed(1403, "access_token 校验失败"))
            action = parsed.path.lstrip("/")
            params = dict(parse_qsl(parsed.query))
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if body:
                ctype = self.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                        if isinstance(payload, dict):
                            params.update(payload)
                    except json.JSONDecodeError:
                        return self._respond(400, failed(1400, "JSON 请求体解析失败"))
                else:
                    params.update(dict(parse_qsl(body.decode("utf-8", "replace"))))

            echo = params.pop("echo", None)
            if action.endswith("_async"):
                action = action[: -len("_async")]
            elif action.endswith("_rate_limited"):
                action = action[: -len("_rate_limited")]

            try:
                data = dispatch(action, params)
                resp = ok(data, echo)
            except ApiError as exc:
                resp = failed(exc.retcode, str(exc), echo)
            except Exception as exc:  # noqa: BLE001 - report, don't die
                resp = failed(1400, f"{type(exc).__name__}: {exc}", echo)
            self._respond(200, resp)

        do_GET = _handle
        do_POST = _handle

        def _respond(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ApiHandler


def serve_http(dispatch, host, port, access_token=None, ws_hub=None, media_root=None,
               routes=None, log_buffer=None):
    """Blocking HTTP server; call from a thread."""
    handler = make_api_handler(dispatch, access_token, ws_hub=ws_hub,
                               media_root=media_root, routes=routes,
                               log_buffer=log_buffer)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.serve_forever()


def meta_event(self_id, meta_type, sub_type=None, **extra):
    event = {
        "time": int(time.time()),
        "self_id": int(self_id or 0),
        "post_type": "meta_event",
        "meta_event_type": meta_type,
    }
    if sub_type:
        event["sub_type"] = sub_type
    event.update(extra)
    return event
