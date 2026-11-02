"""automatic/1 protocol server — see docs/automatic-protocol.md.

One websocket (forward ``/automatic/v1`` or reverse to configured URLs)
carries both events and calls; ``POST /automatic/v1/<action>`` mirrors the
calls over HTTP; ``GET /automatic/v1/media/<path>`` serves received media.

Design points carried over from the spec:

- string ids everywhere; conversations are ``group:<uin>`` / ``private:<uin>``
- a sender whose QQ number is not resolved yet is ``syn:<n>`` with
  ``resolved: false`` — it can never be mistaken for a QQ number
- events carry a per-session ``seq``; ``hello.resume`` replays what was missed
- messages say ``delivery: live | backlog``
- errors are string codes with ``retryable``; retryable means *no side
  effect happened* (the bridge refuses before typing when it cannot verify
  the target), so a client may simply retry
"""

import collections
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlparse

from loguru import logger

from . import core
from .core import QueuedWsClient
from .ob11 import SYN_BASE, ApiError, parse_cq
from .ratelimit import RATE_LIMITED

PROTOCOL = "automatic/1"
BASE = "/automatic/v1"
HEARTBEAT_EVERY = 30.0

CAPABILITIES = [
    "message.send", "message.send.text", "message.send.mention", "message.send.image",
    "message.send.quote", "message.recall", "message.get", "message.history",
    "conversation.list", "conversation.get",
    "member.list", "member.get", "member.resolve", "member.mute", "poke",
    "self.get", "notification.system",
]

LOGIN_STATES = {"logged_in": "online", "login_required": "login_required",
                "not_running": "offline", "unknown": "unknown"}


class AutomaticError(Exception):
    STATUS = {"unauthorized": 401, "unsupported": 400, "unsupported_segment": 400,
              "bad_params": 400, "not_found": 404, "not_logged_in": 503,
              "target_unverified": 409, "gui_busy": 503, "gui_failed": 500,
              "rate_limited": 429}

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def from_api_error(exc):
    """Map a bridge ApiError onto an automatic error code.

    target_unverified / not_logged_in are only used where the bridge refuses
    *before* touching the composer, which is what makes them retryable."""
    msg = str(exc)
    if exc.retcode == 1404:
        return AutomaticError("not_found", msg)
    if exc.retcode == 1403:
        return AutomaticError("unauthorized", msg)
    if exc.retcode == RATE_LIMITED:
        return AutomaticError("rate_limited", msg, retryable=True)
    if "不支持的消息段" in msg:
        return AutomaticError("unsupported_segment", msg)
    if "缺少" in msg or "无效" in msg:
        return AutomaticError("bad_params", msg)
    if "当前状态" in msg or "不在无障碍树" in msg:
        return AutomaticError("not_logged_in", msg, retryable=True)
    if any(k in msg for k in ("身份验证失败", "发送目标被切换", "拒绝打开", "没有找到会话",
                              "残留内容")):
        return AutomaticError("target_unverified", msg, retryable=True)
    return AutomaticError("gui_failed", msg)


def user_ref(uin, name=None):
    uin = str(uin or "")
    syn = uin.isdigit() and int(uin) >= SYN_BASE
    out = {"id": f"syn:{uin}" if syn else uin, "resolved": not syn}
    if name:
        out["name"] = name
    return out


def bridge_uid(user_id):
    """automatic user id -> what the bridge's OneBot-shaped calls take."""
    uid = str(user_id or "")
    return uid[4:] if uid.startswith("syn:") else uid


def conv_ref(uin, kind, name=None):
    kind = "group" if kind == "group" else "private"
    out = {"id": f"{kind}:{uin}", "kind": kind}
    if name:
        out["name"] = name
    return out


def parse_conv(cid):
    cid = str(cid or "")
    kind, _, uin = cid.partition(":")
    if kind not in ("group", "private") or not uin:
        raise AutomaticError("bad_params", f"conversation 必须形如 group:<号> / private:<号>: {cid!r}")
    return uin, ("group" if kind == "group" else "c2c")


class AutomaticServer:
    name = "automatic"

    def __init__(self, bridge, host="0.0.0.0", port=5600, token=None, reverse_urls=None,
                 buffer=1000):
        self.bridge = bridge
        self.host = host
        self.port = int(port)
        self.token = token or None
        self.reverse_urls = list(reverse_urls or [])
        self.session = "s_" + secrets.token_hex(6)
        self._seq = 0
        self._lock = threading.Lock()
        self._backlog = collections.deque(maxlen=buffer)   # (seq, frame text)
        self._clients = set()
        self._server = None
        self._stop = threading.Event()
        self._last_heartbeat = 0.0

    # -- lifecycle --------------------------------------------------------------

    def start(self):
        self._server = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        for url in self.reverse_urls:
            threading.Thread(target=self._reverse_loop, args=(url,), daemon=True).start()
        logger.info(f"automatic: ws://{self.host}:{self.port}{BASE}"
                    + (f"  反向 {', '.join(self.reverse_urls)}" if self.reverse_urls else ""))
        return self

    def stop(self):
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._lock:
            clients, self._clients = list(self._clients), set()
        for c in clients:
            c.close()

    def info(self):
        return {"port": self.port, "clients": len(self._clients), "seq": self._seq,
                "session": self.session}

    # -- entities ---------------------------------------------------------------

    def _conv_name(self, uin):
        row = self.bridge.cache.lookup(str(uin))
        return ((row or {}).get("name_clean") or (row or {}).get("name") or "").strip() or None

    def _media_url(self, ref):
        if not ref:
            return None
        path = urlparse(ref).path if ref.startswith("file://") else ref
        root = self.bridge.nt_data
        if root and os.path.isabs(path):
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
            if not rel.startswith(".."):
                return f"{BASE}/media/{rel.replace(os.sep, '/')}"
        return ref if ref.startswith(("http://", "https://")) else None

    def segments_from_cq(self, cq):
        """(segments, quote_ob_id) from the stored CQ rendering."""
        segs, quote = [], None
        for seg_type, data in parse_cq(cq or ""):
            if seg_type == "text":
                if segs and segs[-1]["type"] == "text":
                    segs[-1]["text"] += data.get("text", "")
                else:
                    segs.append({"type": "text", "text": data.get("text", "")})
            elif seg_type == "at":
                qq = data.get("qq", "")
                segs.append({"type": "mention", "all": True} if qq == "all"
                            else {"type": "mention", "user_id": user_ref(qq)["id"]})
            elif seg_type == "reply":
                quote = data.get("id")
            elif seg_type in ("image", "record", "video"):
                kind = {"image": "image", "record": "voice", "video": "video"}[seg_type]
                seg = {"type": kind}
                url = self._media_url(data.get("url") or data.get("file") or "")
                if url:
                    seg["url"] = url
                segs.append(seg)
            else:
                segs.append({"type": "unknown", "text": f"[{seg_type}]"})
        return segs, quote

    def message_from_event(self, ev):
        msg = ev.msg
        if msg.kind == "file":
            segs = [{"type": "file", "name": msg.file_name or msg.content or "文件"}]
            if msg.file_size:
                segs[0]["size"] = msg.file_size
        elif msg.kind in ("forward", "card"):
            segs = [{"type": msg.kind, "text": msg.content or ""}]
        else:
            segs, _ = self.segments_from_cq(ev.cq)
        out = {
            "id": msg.msg_id,
            "conversation": conv_ref(ev.conv_uin, ev.kind, ev.conv_name or self._conv_name(ev.conv_uin)),
            "sender": user_ref(self.bridge.self_id if msg.is_self else ev.sender_uin, msg.sender),
            "self": bool(msg.is_self),
            "time": int(ev.time * 1000),
            "segments": segs,
            "delivery": "backlog" if ev.backlog else "live",
        }
        if ev.kind == "group":
            out["role"] = ev.role
        if ev.reply_msg_id:
            out["quote"] = {"id": ev.reply_msg_id}
        return out

    def message_from_row(self, row):
        conv = row["conv_uin"]
        kind = row.get("kind")
        if kind not in ("group", "c2c"):
            kind = (self.bridge.cache.lookup(str(conv)) or {}).get("kind") or "c2c"
        segs, quote_ob = self.segments_from_cq(row["message"])
        out = {
            "id": _msg_id(row),
            "conversation": conv_ref(conv, kind, self._conv_name(conv)),
            "sender": user_ref(row.get("sender_uin"), row.get("sender_name")),
            "self": row.get("direction") == "out",
            "time": int(float(row["post_time"]) * 1000),
            "segments": segs,
            "delivery": "backlog",   # 从存档里取的都不是实时到达
        }
        if kind == "group" and row.get("role"):
            out["role"] = row["role"]
        if quote_ob and str(quote_ob).isdigit():
            q = self.bridge.ids.get(int(quote_ob))
            if q:
                out["quote"] = {"id": _msg_id(q)}
        return out

    def self_info(self):
        return {"id": str(self.bridge.self_id or ""), "name": self.bridge.self_name or None,
                "avatar": self.bridge.self_face_url or None}

    def login_state(self):
        try:
            return LOGIN_STATES.get(self.bridge._login_state_cached(ttl=5.0), "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"

    # -- events -----------------------------------------------------------------

    def handle(self, event):
        if isinstance(event, core.Heartbeat):
            now = time.monotonic()
            if now - self._last_heartbeat >= HEARTBEAT_EVERY:
                self._last_heartbeat = now
                self._broadcast({"type": "event", "event": {
                    "type": "status.heartbeat", "time": int(event.time * 1000),
                    "login": LOGIN_STATES.get(event.state, "unknown")}}, sequenced=False)
            return
        body = self.render(event)
        if body is not None:
            self._broadcast({"type": "event", "event": body}, sequenced=True)

    def render(self, event):
        t = int(getattr(event, "time", time.time()) * 1000)
        if isinstance(event, core.MessageCreated):
            return {"type": "message.created", "time": t, "message": self.message_from_event(event)}
        if isinstance(event, core.MemberResolved):
            return {"type": "member.resolved", "time": t,
                    "conversation": conv_ref(event.group_uin, "group", self._conv_name(event.group_uin)),
                    "name": event.name, "from": user_ref(event.was_user_id)["id"],
                    "to": str(event.user_id)}
        if isinstance(event, core.LoginChanged) and event.prev is not None:
            return {"type": "login.changed", "time": t,
                    "prev": LOGIN_STATES.get(event.prev, "unknown"),
                    "state": LOGIN_STATES.get(event.state, "unknown")}
        if isinstance(event, core.SystemNotification):
            return {"type": "notification.system", "time": t, "title": event.title,
                    "text": event.text}
        return None

    def _broadcast(self, frame, sequenced):
        with self._lock:
            if sequenced:
                self._seq += 1
                frame = {"type": "event", "seq": self._seq, "event": frame["event"]}
            text = json.dumps(frame, ensure_ascii=False)
            if sequenced:
                self._backlog.append((self._seq, text))
            clients = list(self._clients)
        for c in clients:
            c.send(text)

    # -- connections ------------------------------------------------------------

    def serve_conn(self, conn):
        """Shared by forward and reverse ws: hello -> ready (+replay) -> calls."""
        client = QueuedWsClient(conn)
        state = {"ready": False}

        def deadline():
            if not state["ready"]:
                client.send(json.dumps({"type": "error", "error": {
                    "code": "unauthorized", "message": "10s 内没有 hello", "retryable": True}}))
                client.close()

        timer = threading.Timer(10.0, deadline)
        timer.daemon = True
        timer.start()
        try:
            for text in conn.messages():
                try:
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    continue
                kind = frame.get("type")
                if kind == "hello" and not state["ready"]:
                    if not self._hello(client, frame):
                        break
                    state["ready"] = True
                elif kind == "call" and state["ready"]:
                    threading.Thread(target=self._ws_call, args=(client, frame), daemon=True).start()
        finally:
            timer.cancel()
            with self._lock:
                self._clients.discard(client)
            client.close()

    def _hello(self, client, frame):
        def refuse(code, message):
            client.send(json.dumps({"type": "error", "error": {
                "code": code, "message": message, "retryable": False}}, ensure_ascii=False))
            return False

        if not str(frame.get("protocol") or "").startswith(PROTOCOL):
            return refuse("unsupported", f"只支持 {PROTOCOL}")
        if self.token and frame.get("token") != self.token:
            return refuse("unauthorized", "令牌错误")
        resume = frame.get("resume") or {}
        resumed = resume.get("session") == self.session and isinstance(resume.get("seq"), int)
        with self._lock:
            ready = {
                "type": "ready", "session": self.session, "seq": self._seq,
                "resumed": resumed, "self": self.self_info(), "login": self.login_state(),
                "capabilities": CAPABILITIES,
                "limits": {"replay_window": self._backlog.maxlen},
                "options": {"wait_resolve": bool(getattr(self.bridge, "resolve_first", False))},
            }
            client.send(json.dumps(ready, ensure_ascii=False))
            if resumed:
                for seq, text in self._backlog:
                    if seq > resume["seq"]:
                        client.send(text)
            self._clients.add(client)
        return True

    def _ws_call(self, client, frame):
        cid = frame.get("id")
        try:
            data = self.call(str(frame.get("action") or ""), frame.get("params") or {})
            out = {"type": "result", "id": cid, "ok": True, "data": data}
        except AutomaticError as exc:
            out = {"type": "result", "id": cid, "ok": False, "error": exc.as_dict()}
        client.send(json.dumps(out, ensure_ascii=False))

    def _reverse_loop(self, url):
        from . import ws11

        while not self._stop.is_set():
            try:
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
                conn = ws11.connect(url, headers=headers)
                logger.info(f"automatic 反向 ws 已连接: {url}")
                self.serve_conn(conn)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"automatic 反向 ws {url}: {exc}")
            if self._stop.wait(3.0):
                return

    # -- actions ----------------------------------------------------------------

    def call(self, action, params):
        fn = ACTIONS.get(action)
        if fn is None:
            raise AutomaticError("unsupported", f"不支持的 action: {action}")
        if not isinstance(params, dict):
            raise AutomaticError("bad_params", "params 必须是对象")
        try:
            return fn(self, params)
        except ApiError as exc:
            raise from_api_error(exc) from exc
        except AutomaticError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"automatic {action} 失败: {exc}")
            raise AutomaticError("gui_failed", str(exc)) from exc

    def _row(self, uin, mid):
        mid = str(mid or "")
        if mid.startswith("local:") and mid[6:].isdigit():
            row = self.bridge.ids.get(int(mid[6:]))
        else:
            row = self.bridge.ids.by_msg_id(uin, mid)
        if row is None or str(row["conv_uin"]) != str(uin):
            raise AutomaticError("not_found", f"消息 {mid} 不存在（只有协议端见过的消息）")
        return row

    def act_self_get(self, p):
        return {"self": self.self_info(), "login": self.login_state()}

    def act_message_send(self, p):
        uin, kind = parse_conv(p.get("conversation"))
        segs = p.get("segments")
        if not isinstance(segs, list) or not segs:
            raise AutomaticError("bad_params", "segments 必须是非空数组")
        ob = []
        if p.get("quote"):
            ob.append({"type": "reply", "data": {"id": str(self._row(uin, p["quote"])["ob_id"])}})
        for seg in segs:
            t = (seg or {}).get("type")
            if t == "text":
                ob.append({"type": "text", "data": {"text": str(seg.get("text", ""))}})
            elif t == "mention":
                if seg.get("all"):
                    ob.append({"type": "at", "data": {"qq": "all"}})
                else:
                    uid = str(seg.get("user_id") or "")
                    if not uid.isdigit():
                        raise AutomaticError("bad_params", f"mention 需要真实 QQ 号（未解析的 {uid} 先 member.resolve）")
                    ob.append({"type": "at", "data": {"qq": uid}})
            elif t == "image":
                src = seg.get("file") or seg.get("url") or (
                    "base64://" + seg["base64"] if seg.get("base64") else None)
                if not src:
                    raise AutomaticError("bad_params", "image 需要 url / file / base64 之一")
                ob.append({"type": "image", "data": {"file": str(src)}})
            elif t == "face":
                ob.append({"type": "text", "data": {"text": str(seg.get("text") or "[表情]")}})
            else:
                raise AutomaticError("unsupported_segment", f"不支持发送的消息段: {t!r}")
        res = self.bridge.api_send(uin, ob)
        row = self.bridge.ids.get(res["message_id"])
        if row and kind == "group" and row.get("kind") == "unknown":
            row = dict(row, kind="group")
        return {"message": self.message_from_row(row) if row else {"id": f"local:{res['message_id']}"}}

    def act_message_recall(self, p):
        uin, _ = parse_conv(p.get("conversation"))
        row = self._row(uin, p.get("id"))
        self.bridge.act_delete_msg({"message_id": row["ob_id"]})
        return {}

    def act_message_get(self, p):
        uin, _ = parse_conv(p.get("conversation"))
        return {"message": self.message_from_row(self._row(uin, p.get("id")))}

    def act_message_history(self, p):
        uin, _ = parse_conv(p.get("conversation"))
        limit = max(1, min(int(p.get("limit") or 20), 100))
        before = int(self._row(uin, p["before"])["ob_id"]) if p.get("before") else 0
        rows = self.bridge.ids.history(uin, before_id=before, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        out = {"messages": [self.message_from_row(r) for r in reversed(rows)]}
        if more and rows:
            out["next"] = _msg_id(rows[-1])
        return out

    def _conversations(self):
        from .store import is_uin

        out = []
        for r in self.bridge.cache.conversations():
            if r["kind"] in ("group", "c2c") and is_uin(r["uin"]):
                out.append(conv_ref(r["uin"], r["kind"], self._conv_name(r["uin"])))
        return out

    def act_conversation_list(self, p):
        kind = p.get("kind")
        convs = self._conversations()
        if kind:
            convs = [c for c in convs if c["kind"] == kind]
        return {"conversations": convs}

    def act_conversation_get(self, p):
        uin, kind = parse_conv(p.get("id"))
        for c in self._conversations():
            if c["id"] == f"{'group' if kind == 'group' else 'private'}:{uin}":
                return {"conversation": c}
        raise AutomaticError("not_found", f"会话 {p.get('id')} 不在缓存里")

    def _member(self, m):
        return {"user": user_ref(m["user_id"], m.get("nickname")), "role": m.get("role") or "member"}

    def _group(self, p):
        uin, kind = parse_conv(p.get("conversation"))
        if kind != "group":
            raise AutomaticError("bad_params", "成员相关动作只用于群会话")
        return uin

    def act_member_list(self, p):
        gid = self._group(p)
        return {"members": [self._member(m) for m in
                            self.bridge.act_get_group_member_list({"group_id": gid})]}

    def act_member_get(self, p):
        gid = self._group(p)
        m = self.bridge.act_get_group_member_info(
            {"group_id": gid, "user_id": bridge_uid(p.get("user_id"))})
        return {"member": self._member(m)}

    def act_member_resolve(self, p):
        gid = self._group(p)
        r = self.bridge.act_resolve_member({"group_id": gid, "user_id": bridge_uid(p.get("user_id"))})
        return {"user": user_ref(r["user_id"], r.get("nickname"))}

    def act_member_mute(self, p):
        gid = self._group(p)
        ms = int(p.get("duration_ms") or 0)
        self.bridge.act_set_group_ban({"group_id": gid, "user_id": bridge_uid(p.get("user_id")),
                                       "duration": (ms + 999) // 1000})
        return {}

    def act_poke(self, p):
        gid = self._group(p)
        self.bridge.act_send_poke({"group_id": gid, "user_id": bridge_uid(p.get("user_id"))})
        return {}


ACTIONS = {
    "self.get": AutomaticServer.act_self_get,
    "message.send": AutomaticServer.act_message_send,
    "message.recall": AutomaticServer.act_message_recall,
    "message.get": AutomaticServer.act_message_get,
    "message.history": AutomaticServer.act_message_history,
    "conversation.list": AutomaticServer.act_conversation_list,
    "conversation.get": AutomaticServer.act_conversation_get,
    "member.list": AutomaticServer.act_member_list,
    "member.get": AutomaticServer.act_member_get,
    "member.resolve": AutomaticServer.act_member_resolve,
    "member.mute": AutomaticServer.act_member_mute,
    "poke": AutomaticServer.act_poke,
}


def _msg_id(row):
    return str(row["msg_id"]) if row.get("msg_id") else f"local:{row['ob_id']}"


def _make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "whiskerwind-automatic/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            logger.debug("automatic http: " + (fmt % a))

        def _auth(self):
            if not server.token:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {server.token}":
                return True
            return dict(parse_qsl(urlparse(self.path).query)).get("token") == server.token

        def _send(self, status, payload=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, exc):
            self._send(AutomaticError.STATUS.get(exc.code, 500), {"ok": False, "error": exc.as_dict()})

        def do_GET(self):
            path = urlparse(self.path).path
            if path == BASE and "websocket" in (self.headers.get("upgrade") or "").lower():
                from . import ws11
                key = self.headers.get("sec-websocket-key")
                if not key:
                    return self._fail(AutomaticError("bad_params", "缺少 Sec-WebSocket-Key"))
                self.connection.sendall(ws11.accept_response(key))
                self.close_connection = True
                server.serve_conn(ws11.WsConn(self.connection))
                return
            if path.startswith(BASE + "/media/"):
                if not self._auth():
                    return self._fail(AutomaticError("unauthorized", "令牌错误"))
                root = server.bridge.nt_data
                rel = unquote(path[len(BASE + "/media/"):])
                if not root:
                    return self._fail(AutomaticError("not_found", "没有媒体目录"))
                base = os.path.realpath(root)
                full = os.path.realpath(os.path.join(base, rel))
                if not full.startswith(base + os.sep) or not os.path.isfile(full):
                    return self._fail(AutomaticError("not_found", "文件不存在"))
                import mimetypes
                with open(full, "rb") as fh:
                    data = fh.read()
                return self._send(200, raw=data,
                                  ctype=mimetypes.guess_type(full)[0] or "application/octet-stream")
            return self._fail(AutomaticError("not_found", "不存在"))

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if not path.startswith(BASE + "/"):
                return self._fail(AutomaticError("not_found", "不存在"))
            if not self._auth():
                return self._fail(AutomaticError("unauthorized", "令牌错误"))
            try:
                params = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._fail(AutomaticError("bad_params", "请求体不是合法 JSON"))
            try:
                data = server.call(path[len(BASE) + 1:], params)
            except AutomaticError as exc:
                return self._fail(exc)
            return self._send(200, {"ok": True, "data": data})

    return Handler
