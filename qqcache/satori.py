"""Satori protocol server (SDK side): https://satori.chat

One login (the QQ account) exposed over

- HTTP API   ``POST /v1/{resource}.{method}`` (JSON in/out, Bearer token)
- events     ``GET /v1/events`` websocket: IDENTIFY -> READY, PING/PONG,
             EVENT frames with a per-process ``sn``; IDENTIFY{sn} replays what
             the client missed from a ring buffer
- webhooks   ``POST /v1/meta/webhook.create|delete`` (optional in the spec)
- meta       ``POST /v1/meta``
- proxy      ``GET /v1/proxy/internal:{platform}/{self}/media/{rel}`` serves
             received media from QQ's nt_data dir

QQ mapping: a group is both the guild and its only channel (same id); a
private chat is channel ``private:{uin}``. Message ids are QQ's native
msg_id strings (``ob:{n}`` for a sent message whose echo was not captured).
A sender whose QQ number is not resolved yet carries the bridge's synthetic
id (>= 900000000000); the ``internal`` event ``qqcache/member-resolved``
announces the upgrade, like OneBot's ``qqcache_member_resolved`` notice.

Capability is bounded by GUI automation: methods QQ itself lacks (edit,
channel management) answer 404 per spec; ones QQ has but this bridge cannot
drive yet answer 501.
"""

import collections
import email
import json
import os
import queue
import re
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from loguru import logger

from . import core
from .core import QueuedWsClient
from .ob11 import SYN_BASE, ApiError, parse_cq

ADAPTER = "whiskerwind"
LOGIN_SN = 1

OP_EVENT, OP_PING, OP_PONG, OP_IDENTIFY, OP_READY, OP_META = range(6)
STATUS_OFFLINE, STATUS_ONLINE = 0, 1
CHANNEL_TEXT, CHANNEL_DIRECT = 0, 1

FEATURES = [
    "login.get", "message.create", "message.get", "message.delete", "message.list",
    "guild.get", "guild.list", "channel.get", "channel.list",
    "guild.member.get", "guild.member.list", "guild.member.mute",
    "user.get", "user.channel.create", "friend.list", "upload.create",
    "guild.plain",
]
# 平台本身没有的能力：规范要求 404
UNSUPPORTED = {
    "message.update", "channel.create", "channel.update", "channel.delete",
    "channel.mute", "reaction.create", "reaction.delete", "reaction.clear",
    "reaction.list", "guild.role.list", "guild.role.create", "guild.role.update",
    "guild.role.delete", "guild.member.role.set", "guild.member.role.unset",
}
# QQ 有、桥还驱动不了：501
UNIMPLEMENTED = {
    "guild.member.kick", "guild.member.approve", "guild.approve",
    "friend.approve", "friend.delete",
}

PRIVATE_PREFIX = "private:"
MEMBER_COUNT_RE = re.compile(r"[（(]\s*\d+\s*(?:人)?[）)]\s*$")
TMP_TTL = 300.0


class SatoriError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# message elements


def escape(text, attr=False):
    out = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out.replace('"', "&quot;") if attr else out


def _attr(name, value):
    return f' {name}="{escape(str(value), attr=True)}"'


def _el(tag, **attrs):
    return "<" + tag + "".join(_attr(k, v) for k, v in attrs.items() if v not in (None, "")) + "/>"


_ENTITY = re.compile(r"&(#x[0-9a-fA-F]+|#\d+|quot|amp|lt|gt|apos);")


def unescape(text):
    def sub(m):
        e = m.group(1)
        if e.startswith("#x"):
            return chr(int(e[2:], 16))
        if e.startswith("#"):
            return chr(int(e[1:]))
        return {"quot": '"', "amp": "&", "lt": "<", "gt": ">", "apos": "'"}[e]
    return _ENTITY.sub(sub, text)


_TAG = re.compile(
    r"<!--.*?-->"
    r"|<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s=/>]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'))?)*)\s*(/?)>",
    re.S,
)
_ATTR = re.compile(r"([^\s=/>]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'))?")


def parse_elements(content):
    """Satori content -> flat list of (type, attrs) tokens in document order.

    Text comes out as ("text", {"text": ...}); tags as their lowercase name
    with attrs, plus ("/name", {}) for closing tags. Unpaired tags are text,
    as the spec says, which a flat tokenizer gets for free by never failing.
    """
    tokens = []
    pos = 0
    for m in _TAG.finditer(content or ""):
        if m.start() > pos:
            tokens.append(("text", {"text": unescape(content[pos:m.start()])}))
        pos = m.end()
        if m.group(0).startswith("<!--"):
            continue
        closing, name, raw_attrs, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if closing:
            tokens.append(("/" + name, {}))
            continue
        attrs = {}
        for am in _ATTR.finditer(raw_attrs or ""):
            val = am.group(2) if am.group(2) is not None else am.group(3)
            attrs[am.group(1)] = unescape(val) if val is not None else True
        tokens.append((name, attrs))
        if selfclose:
            tokens.append(("/" + name, {}))
    if pos < len(content or ""):
        tokens.append(("text", {"text": unescape(content[pos:])}))
    return tokens


# ---------------------------------------------------------------------------
# the server


class SatoriServer:
    """Satori SDK endpoint for the bridge; also its protocol adapter."""

    name = "satori"

    def __init__(self, bridge, host="0.0.0.0", port=5500, token=None, platform="qq",
                 webhooks=None, buffer=1000):
        self.bridge = bridge
        self.host = host
        self.port = int(port)
        self.token = token or None
        self.platform = platform or "qq"
        self.webhooks = list(webhooks or [])   # [{"url", "token"}]
        self._sn = 0
        self._sn_lock = threading.Lock()
        self._backlog = collections.deque(maxlen=buffer)   # (sn, body)
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._webhook_q = queue.Queue()
        self._server = None
        self._tmp_dir = tempfile.mkdtemp(prefix="satori-up-")
        self._tmp = {}   # name -> (path, expires)

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._webhook_loop, daemon=True).start()
        logger.info(f"Satori: http://{self.host}:{self.port}/v1  (platform={self.platform})")
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._clients_lock:
            clients, self._clients = list(self._clients), set()
        for c in clients:
            c.close()
        self._webhook_q.put(None)

    def info(self):
        return {"port": self.port, "clients": len(self._clients), "webhooks": len(self.webhooks),
                "sn": self._sn}

    # -- resources ------------------------------------------------------------

    @property
    def self_uin(self):
        return str(self.bridge.self_id or "")

    def login(self, full=True):
        user = {"id": self.self_uin, "name": self.bridge.self_name or None,
                "avatar": self.bridge.self_face_url or _qq_avatar(self.self_uin)}
        if not full:
            return {"sn": LOGIN_SN, "platform": self.platform, "user": {"id": self.self_uin}}
        try:
            state = self.bridge._login_state_cached(ttl=5.0)
        except Exception:  # noqa: BLE001
            state = "unknown"
        return {
            "sn": LOGIN_SN, "platform": self.platform, "user": _strip(user),
            "status": STATUS_ONLINE if state == "logged_in" else STATUS_OFFLINE,
            "adapter": ADAPTER, "features": FEATURES,
        }

    def _conv_name(self, uin):
        row = self.bridge.cache.lookup(str(uin))
        name = (row or {}).get("name_clean") or (row or {}).get("name") or ""
        return MEMBER_COUNT_RE.sub("", name).strip()

    def channel_of(self, conv_uin, kind):
        if kind == "group":
            return _strip({"id": str(conv_uin), "type": CHANNEL_TEXT,
                           "name": self._conv_name(conv_uin) or None})
        return {"id": PRIVATE_PREFIX + str(conv_uin), "type": CHANNEL_DIRECT}

    def guild_of(self, group_uin):
        return _strip({"id": str(group_uin), "name": self._conv_name(group_uin) or None,
                       "avatar": f"https://p.qlogo.cn/gh/{group_uin}/{group_uin}/640"})

    def parse_channel(self, channel_id):
        """channel_id -> (conv_uin, kind)."""
        cid = str(channel_id or "")
        if cid.startswith(PRIVATE_PREFIX):
            return cid[len(PRIVATE_PREFIX):], "c2c"
        if not cid:
            raise SatoriError(400, "缺少 channel_id")
        return cid, "group"

    def _internal(self, path):
        return f"internal:{self.platform}/{self.self_uin}/{path}"

    def cq_to_content(self, cq, conv_uin):
        """Stored/emitted CQ rendering -> Satori message elements."""
        out = []
        for seg_type, data in parse_cq(cq or ""):
            if seg_type == "text":
                out.append(escape(data.get("text", "")))
            elif seg_type == "at":
                qq = data.get("qq", "")
                out.append(_el("at", type="all") if qq == "all" else _el("at", id=qq))
            elif seg_type == "reply":
                row = self.bridge.ids.get(data.get("id") or 0) if str(data.get("id", "")).isdigit() else None
                if row:
                    out.append(_el("quote", id=_message_id(row)))
            elif seg_type in ("image", "record", "video"):
                tag = {"image": "img", "record": "audio", "video": "video"}[seg_type]
                src = self._media_url(data.get("url") or data.get("file") or "")
                out.append(_el(tag, src=src) if src else {"img": "[图片]", "audio": "[语音]",
                                                          "video": "[视频]"}[tag])
            else:
                out.append(escape(f"[{seg_type}]"))
        return "".join(out)

    def _media_url(self, ref):
        if not ref:
            return ""
        path = urlparse(ref).path if ref.startswith("file://") else ref
        root = self.bridge.nt_data
        if root and os.path.isabs(path):
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
            if not rel.startswith(".."):
                return self._internal("media/" + rel.replace(os.sep, "/"))
        return ref if ref.startswith(("http://", "https://")) else ""

    def message_obj(self, row):
        """Stored ob11_messages row -> Satori Message (+ user/member hoisted)."""
        conv, kind = row["conv_uin"], row.get("kind")
        if kind not in ("group", "c2c"):
            cached = self.bridge.cache.lookup(str(conv))
            kind = (cached or {}).get("kind") or ("group" if kind == "group" else "c2c")
        msg = {
            "id": _message_id(row),
            "content": self.cq_to_content(row["message"], conv),
            "channel": self.channel_of(conv, kind),
            "user": self._user(row.get("sender_uin"), row.get("sender_name")),
            "created_at": int(float(row["post_time"]) * 1000),
        }
        if kind == "group":
            msg["guild"] = self.guild_of(conv)
            msg["member"] = _strip({"nick": row.get("sender_name") or None})
        return msg

    def _user(self, uin, name=None):
        uin = str(uin or "")
        return _strip({"id": uin, "name": name or None,
                       "avatar": _qq_avatar(uin) if _real(uin) else None})

    # -- events -----------------------------------------------------------------

    def handle(self, event):
        body = self.render(event)
        if body is not None:
            self._dispatch(body)

    def render(self, event):
        """Core event -> Satori Event body without ``sn`` (None = not mapped)."""
        if isinstance(event, core.MessageCreated):
            ts = int(event.time * 1000)
            msg = event.msg
            body = {
                "type": "message-created", "timestamp": ts, "login": self.login(full=False),
                "channel": self.channel_of(event.conv_uin, event.kind),
                "user": self._user(self.self_uin if msg.is_self else event.sender_uin,
                                   msg.sender),
                "message": {"id": msg.msg_id, "content": self.cq_to_content(event.cq, event.conv_uin),
                            "created_at": ts},
            }
            if event.kind == "group":
                body["guild"] = self.guild_of(event.conv_uin)
                body["member"] = _strip({"nick": msg.sender or None})
            return body
        if isinstance(event, core.MemberResolved):
            return {
                "type": "internal", "timestamp": int(event.time * 1000),
                "login": self.login(full=False),
                "_type": "qqcache/member-resolved",
                "_data": {"guild_id": event.group_uin, "user_id": event.user_id,
                          "was_user_id": event.was_user_id, "nick": event.name},
            }
        if isinstance(event, core.LoginChanged) and event.prev is not None:
            return {"type": "login-updated", "timestamp": int(event.time * 1000),
                    "login": self.login(full=True)}
        return None

    def _dispatch(self, body):
        login_event = body["type"].startswith("login-")
        # 分配 sn、进 backlog、取客户端快照在同一把锁里：serve_ws 注册新
        # 客户端也持这把锁，补发和实时推送之间不会重也不会漏
        with self._sn_lock:
            self._sn += 1
            body = {"sn": self._sn, **body}
            if not login_event:   # 登录事件不参与会话恢复
                self._backlog.append((self._sn, body))
            with self._clients_lock:
                clients = list(self._clients)
        frame = json.dumps({"op": OP_EVENT, "body": body}, ensure_ascii=False)
        for c in clients:
            c.send(frame)
        if self.webhooks:
            self._webhook_q.put((OP_EVENT, body))

    def _webhook_loop(self):
        while True:
            item = self._webhook_q.get()
            if item is None:
                return
            op, body = item
            data = json.dumps(body, ensure_ascii=False).encode()
            for hook in list(self.webhooks):
                headers = {"Content-Type": "application/json", "Satori-Opcode": str(op)}
                if hook.get("token"):
                    headers["Authorization"] = f"Bearer {hook['token']}"
                try:
                    req = urllib.request.Request(hook["url"], data=data, headers=headers,
                                                 method="POST")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        resp.read()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Satori webhook {hook['url']} 推送失败: {exc}")

    def serve_ws(self, conn):
        """One /v1/events connection: IDENTIFY within 10s, then events."""
        client = QueuedWsClient(conn)
        identified = False
        deadline = threading.Timer(10.0, lambda: None if identified else client.close())
        deadline.daemon = True
        deadline.start()
        try:
            for text in conn.messages():
                try:
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    continue
                op = frame.get("op")
                body = frame.get("body") or {}
                if op == OP_PING:
                    client.send(json.dumps({"op": OP_PONG, "body": {}}))
                elif op == OP_IDENTIFY and not identified:
                    if self.token and body.get("token") != self.token:
                        logger.warning("Satori: IDENTIFY 令牌错误，断开")
                        break
                    identified = True
                    client.send(json.dumps({"op": OP_READY, "body": self.meta()},
                                           ensure_ascii=False))
                    resume = body.get("sn")
                    with self._sn_lock:
                        missed = [b for sn, b in self._backlog
                                  if isinstance(resume, int) and sn > resume]
                        with self._clients_lock:
                            self._clients.add(client)
                    for b in missed:
                        client.send(json.dumps({"op": OP_EVENT, "body": b}, ensure_ascii=False))
        finally:
            deadline.cancel()
            with self._clients_lock:
                self._clients.discard(client)
            client.close()

    def meta(self):
        return {"logins": [self.login(full=True)], "proxy_urls": []}

    # -- API --------------------------------------------------------------------

    def call(self, method, params):
        if method in UNSUPPORTED:
            raise SatoriError(404, f"QQ 不支持 {method}")
        if method in UNIMPLEMENTED:
            raise SatoriError(501, f"{method} 尚未实现")
        fn = API.get(method)
        if fn is None:
            raise SatoriError(404, f"未知 API: {method}")
        try:
            return fn(self, params or {})
        except ApiError as exc:
            status = {1404: 404, 1403: 403, 1429: 429}.get(exc.retcode, 500)
            if exc.retcode == 1400 and "缺少" in str(exc):
                status = 400
            raise SatoriError(status, str(exc)) from exc

    def api_login_get(self, p):
        return self.login(full=True)

    def api_message_create(self, p):
        conv, kind = self.parse_channel(p.get("channel_id"))
        content = p.get("content")
        if content is None:
            raise SatoriError(400, "缺少 content")
        out = []
        for segments in self.content_to_segments(content, conv):
            if not segments:
                continue
            res = self.bridge.api_send(conv, segments)
            row = self.bridge.ids.get(res["message_id"])
            out.append(self.message_obj(row) if row else {"id": f"ob:{res['message_id']}"})
        return out

    def _row(self, conv, message_id):
        mid = str(message_id or "")
        if mid.startswith("ob:") and mid[3:].isdigit():
            row = self.bridge.ids.get(int(mid[3:]))
        else:
            row = self.bridge.ids.by_msg_id(conv, mid)
        if row is None or str(row["conv_uin"]) != str(conv):
            raise SatoriError(404, f"消息 {mid} 不存在（只记录服务启动以来收发过的消息）")
        return row

    def api_message_get(self, p):
        conv, _ = self.parse_channel(p.get("channel_id"))
        return self.message_obj(self._row(conv, p.get("message_id")))

    def api_message_delete(self, p):
        conv, _ = self.parse_channel(p.get("channel_id"))
        row = self._row(conv, p.get("message_id"))
        self.bridge.act_delete_msg({"message_id": row["ob_id"]})
        return {}

    def api_message_list(self, p):
        conv, _ = self.parse_channel(p.get("channel_id"))
        direction = p.get("direction") or "before"
        if direction != "before":
            raise SatoriError(400, "只支持 direction=before（只有服务启动以来的记录）")
        limit = max(1, min(int(p.get("limit") or 50), 100))
        before = 0
        if p.get("next"):
            before = int(self._row(conv, p["next"])["ob_id"])
        rows = self.bridge.ids.history(conv, before_id=before, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        data = [self.message_obj(r) for r in reversed(rows)]   # asc by default
        if (p.get("order") or "asc") == "desc":
            data.reverse()
        out = {"data": data}
        if more and rows:
            token = _message_id(rows[-1])
            out["prev"] = out["next"] = token
        return out

    def api_guild_get(self, p):
        gid = str(p.get("guild_id") or "")
        if not self.bridge.cache.lookup(gid):
            raise SatoriError(404, f"群 {gid} 不在缓存里")
        return self.guild_of(gid)

    def api_guild_list(self, p):
        return {"data": [self.guild_of(g["group_id"]) for g in self.bridge.act_get_group_list({})]}

    def api_channel_get(self, p):
        conv, kind = self.parse_channel(p.get("channel_id"))
        return self.channel_of(conv, kind)

    def api_channel_list(self, p):
        gid = str(p.get("guild_id") or "")
        if not gid:
            raise SatoriError(400, "缺少 guild_id")
        return {"data": [self.channel_of(gid, "group")]}

    def _member(self, m):
        return _strip({"user": self._user(m["user_id"], m.get("nickname")),
                       "nick": m.get("nickname") or None,
                       "joined_at": (m.get("join_time") or 0) * 1000 or None})

    def api_guild_member_get(self, p):
        m = self.bridge.act_get_group_member_info(
            {"group_id": p.get("guild_id"), "user_id": p.get("user_id")})
        return self._member(m)

    def api_guild_member_list(self, p):
        ms = self.bridge.act_get_group_member_list({"group_id": p.get("guild_id")})
        return {"data": [self._member(m) for m in ms]}

    def api_guild_member_mute(self, p):
        ms = int(p.get("duration") or 0)
        self.bridge.act_set_group_ban({"group_id": p.get("guild_id"), "user_id": p.get("user_id"),
                                       "duration": (ms + 999) // 1000})
        return {}

    def api_user_get(self, p):
        uid = str(p.get("user_id") or "")
        if not uid:
            raise SatoriError(400, "缺少 user_id")
        row = self.bridge.cache.lookup(uid)
        return self._user(uid, (row or {}).get("name"))

    def api_user_channel_create(self, p):
        uid = str(p.get("user_id") or "")
        if not uid:
            raise SatoriError(400, "缺少 user_id")
        return self.channel_of(uid, "c2c")

    def api_friend_list(self, p):
        return {"data": [_strip({"user": self._user(f["user_id"], f.get("nickname")),
                                 "nick": f.get("remark") or None})
                         for f in self.bridge.act_get_friend_list({})]}

    # -- outbound content -------------------------------------------------------

    def content_to_segments(self, content, conv):
        """Satori content -> list of OneBot-style segment lists, one per
        message to send (``<message>`` splits; nesting/forward is flattened)."""
        messages, cur = [], []
        skipping = []   # 子元素不进正文的元素栈（quote 的被引用内容、button、author）
        href = []

        def text(t):
            if t:
                cur.append({"type": "text", "data": {"text": t}})

        for tag, attrs in parse_elements(content):
            if skipping:
                if tag == "/" + skipping[-1]:
                    skipping.pop()
                elif tag == skipping[-1]:
                    skipping.append(tag)   # 同名嵌套
                continue
            if tag == "text":
                # 首尾含换行的空白按规范忽略
                t = attrs["text"]
                if "\n" in t and not t.strip():
                    continue
                text(t)
            elif tag == "at":
                if attrs.get("type") in ("all", "here"):
                    cur.append({"type": "at", "data": {"qq": "all"}})
                elif attrs.get("id"):
                    cur.append({"type": "at", "data": {"qq": str(attrs["id"])}})
                elif attrs.get("name"):
                    text("@" + str(attrs["name"]))
            elif tag == "sharp":
                text("#" + str(attrs.get("name") or attrs.get("id") or ""))
            elif tag in ("img", "image"):
                src = attrs.get("src") or attrs.get("url")
                if not src:
                    raise SatoriError(400, "img 缺少 src")
                cur.append({"type": "image", "data": {"file": self.resolve_src(str(src))}})
            elif tag in ("audio", "video", "file"):
                text({"audio": "[语音]", "video": "[视频]", "file": "[文件]"}[tag])
            elif tag == "quote":
                if attrs.get("id"):
                    row = self._row(conv, attrs["id"])
                    cur.insert(0, {"type": "reply", "data": {"id": str(row["ob_id"])}})
                skipping.append(tag)   # 引用的子元素是被引用内容，不是本条正文
            elif tag in ("button", "author"):
                skipping.append(tag)
            elif tag == "a":
                href.append(attrs.get("href"))
            elif tag == "/a":
                h = href.pop() if href else None
                if h:
                    text(f" ({h})")
            elif tag in ("br", "/p"):
                text("\n")
            elif tag == "p":
                if cur:
                    text("\n")
            elif tag == "message":
                if cur:
                    messages.append(cur)
                cur = []
            elif tag == "/message":
                if cur:
                    messages.append(cur)
                cur = []
            # b/strong/i/em/u/ins/s/del/spl/code/sup/sub/emoji：只保留内容
        if cur:
            messages.append(cur)
        return messages

    def resolve_src(self, src):
        """img src -> something api_send/resolve_file_ref understands."""
        if src.startswith("internal:"):
            path = self.internal_path(src)
            if path is None:
                raise SatoriError(404, f"内部链接不存在: {src}")
            return "file://" + path
        if src.startswith("data:"):
            head, _, payload = src.partition(",")
            if ";base64" not in head:
                raise SatoriError(400, "data: 只支持 base64")
            return "base64://" + payload
        return src   # http(s):// / file:// / base64:// 交给 resolve_file_ref

    def internal_path(self, url):
        """internal:{platform}/{self}/{path} -> local file, or None."""
        rest = url[len("internal:"):]
        parts = rest.split("/", 2)
        if len(parts) != 3 or parts[0] != self.platform or parts[1] != self.self_uin:
            return None
        path = parts[2]
        if path.startswith("_tmp/"):
            entry = self._tmp.get(path[5:])
            if entry and entry[1] > time.time() and os.path.isfile(entry[0]):
                return entry[0]
            return None
        if path.startswith("media/") and self.bridge.nt_data:
            root = os.path.realpath(self.bridge.nt_data)
            full = os.path.realpath(os.path.join(root, unquote(path[6:])))
            if full.startswith(root + os.sep) and os.path.isfile(full):
                return full
        return None

    def upload(self, content_type, body):
        msg = email.message_from_bytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
        if not msg.is_multipart():
            raise SatoriError(400, "upload.create 需要 multipart/form-data")
        out = {}
        now = time.time()
        for k in [k for k, (_, exp) in self._tmp.items() if exp < now]:
            entry = self._tmp.pop(k)
            try:
                os.unlink(entry[0])
            except OSError:
                pass
        for part in msg.get_payload():
            key = part.get_param("name", header="content-disposition")
            if not key:
                continue
            fname = os.path.basename(part.get_filename() or "file")
            ident = f"{os.urandom(4).hex()}-{fname}"
            path = os.path.join(self._tmp_dir, ident)
            with open(path, "wb") as fh:
                fh.write(part.get_payload(decode=True) or b"")
            self._tmp[ident] = (path, now + TMP_TTL)
            out[key] = self._internal("_tmp/" + ident)
        return out


API = {
    "login.get": SatoriServer.api_login_get,
    "message.create": SatoriServer.api_message_create,
    "message.get": SatoriServer.api_message_get,
    "message.delete": SatoriServer.api_message_delete,
    "message.list": SatoriServer.api_message_list,
    "guild.get": SatoriServer.api_guild_get,
    "guild.list": SatoriServer.api_guild_list,
    "channel.get": SatoriServer.api_channel_get,
    "channel.list": SatoriServer.api_channel_list,
    "guild.member.get": SatoriServer.api_guild_member_get,
    "guild.member.list": SatoriServer.api_guild_member_list,
    "guild.member.mute": SatoriServer.api_guild_member_mute,
    "user.get": SatoriServer.api_user_get,
    "user.channel.create": SatoriServer.api_user_channel_create,
    "friend.list": SatoriServer.api_friend_list,
}


def _message_id(row):
    return str(row["msg_id"]) if row.get("msg_id") else f"ob:{row['ob_id']}"


def _real(uin):
    return str(uin).isdigit() and int(uin) < SYN_BASE


def _qq_avatar(uin):
    return f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640" if _real(uin) else None


def _strip(d):
    return {k: v for k, v in d.items() if v is not None}


def _make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "whiskerwind-satori/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            logger.debug("satori http: " + (fmt % a))

        def _auth(self):
            if not server.token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {server.token}"

        def _send(self, status, payload=None, ctype="application/json", raw=None):
            body = raw if raw is not None else (
                b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message):
            self._send(status, {"message": message})

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/v1/events":
                if "websocket" not in (self.headers.get("upgrade") or "").lower():
                    return self._error(400, "需要 websocket upgrade")
                from . import ws11
                key = self.headers.get("sec-websocket-key")
                if not key:
                    return self._error(400, "缺少 Sec-WebSocket-Key")
                self.connection.sendall(ws11.accept_response(key))
                self.close_connection = True
                server.serve_ws(ws11.WsConn(self.connection))
                return
            if path.startswith("/v1/proxy/"):
                if not self._auth():
                    return self._error(401, "鉴权失败")
                url = unquote(self.path[len("/v1/proxy/"):])
                if not url.startswith("internal:"):
                    return self._error(403, "只代理 internal: 链接")
                local = server.internal_path(url)
                if local is None:
                    return self._error(404, "资源不存在")
                import mimetypes
                with open(local, "rb") as fh:
                    data = fh.read()
                return self._send(200, raw=data,
                                  ctype=mimetypes.guess_type(local)[0] or "application/octet-stream")
            return self._error(405 if path.startswith("/v1/") else 404, "不支持")

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if not path.startswith("/v1/"):
                return self._error(404, "不存在")
            if not self._auth():
                return self._error(401, "鉴权失败")
            method = path[len("/v1/"):]
            try:
                if method == "meta":
                    return self._send(200, server.meta())
                if method in ("meta/webhook.create", "meta/webhook.delete"):
                    p = json.loads(body or b"{}")
                    if not p.get("url"):
                        return self._error(400, "缺少 url")
                    server.webhooks = [w for w in server.webhooks if w["url"] != p["url"]]
                    if method.endswith("create"):
                        server.webhooks.append({"url": p["url"], "token": p.get("token")})
                    on_change = getattr(server, "on_webhooks_changed", None)
                    if on_change:
                        on_change(server.webhooks)
                    return self._send(200, {})
                if method == "upload.create":
                    return self._send(200, server.upload(self.headers.get("Content-Type", ""), body))
                platform = self.headers.get("Satori-Platform")
                user_id = self.headers.get("Satori-User-ID") or self.headers.get("X-Self-ID")
                if (platform and platform != server.platform) or (
                        user_id and server.self_uin and user_id != server.self_uin):
                    return self._error(404, "没有这个登录号")
                try:
                    params = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    return self._error(400, "请求体不是合法 JSON")
                result = server.call(method, params)
                return self._send(200, result)
            except SatoriError as exc:
                return self._error(exc.status, str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Satori {method} 失败: {exc}")
                return self._error(500, str(exc))

    return Handler
