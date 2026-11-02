"""Runtime-managed channels for every outward protocol.

Each channel is one endpoint the WebUI can add, edit or remove without a
restart:

==================== ========= =================================================
type                 protocol  what it is
==================== ========= =================================================
``http``             OneBot    API listener (HTTP actions + forward ws on one port)
``ws_reverse``       OneBot    outbound ws client to a framework's universal endpoint
``http_post``        OneBot    event POST url
``satori``           Satori    Satori server (HTTP API + ``/v1/events``)
``automatic``        automatic automatic server (forward ws + HTTP calls)
``automatic_reverse`` automatic outbound automatic ws client
==================== ========= =================================================

Satori and automatic channels each run their own adapter instance (own event
sequence, own token). Runtime channels persist to a JSON file next to the
database. Startup channels (CLI flags / env) are marked ``source: "startup"``:
they are never persisted and cannot be changed through the API — deleting the
channel you're talking through is a foot-gun.
"""

import json
import os
import secrets
import threading
import time
from http.server import ThreadingHTTPServer

from loguru import logger

from .ob11 import ApiError, make_api_handler, reverse_ws_loop

# type -> (protocol, 显示名)
TYPES = {
    "http": ("onebot11", "HTTP 监听"),
    "ws_reverse": ("onebot11", "反向 WebSocket"),
    "http_post": ("onebot11", "HTTP POST 上报"),
    "satori": ("satori", "Satori 服务端"),
    "automatic": ("automatic", "automatic 服务端"),
    "automatic_reverse": ("automatic", "automatic 反向 WebSocket"),
}
# 每种类型可写的字段（regenerate_token 另算）
FIELDS = {
    "http": {"host", "port"},
    "ws_reverse": {"url", "token"},
    "http_post": {"url"},
    "satori": {"host", "port", "token", "platform"},
    "automatic": {"host", "port", "token"},
    "automatic_reverse": {"url", "token"},
}
LISTENERS = {"http", "satori", "automatic"}
# 自带令牌、且令牌可在 WebUI 查看/重新生成的类型
TOKEN_TYPES = {"ws_reverse", "satori", "automatic", "automatic_reverse"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORTS = {"satori": 5500, "automatic": 5600}


def _new_token():
    return secrets.token_urlsafe(24)


def normalize(spec, explicit_token=False):
    """校验并补全一份通道配置；返回新 dict。``explicit_token``：调用方显式
    给了 token 字段（显式清空令牌与「没填」要区别对待）。"""
    t = spec.get("type")
    if t not in TYPES:
        raise ApiError(1400, f"未知通道类型: {t!r}（{' / '.join(TYPES)}）")
    out = {"type": t}
    for key in FIELDS[t]:
        if key in spec and spec[key] is not None:
            out[key] = spec[key]
    if t in LISTENERS:
        out["host"] = str(out.get("host") or "").strip() or (
            "127.0.0.1" if t == "http" else "0.0.0.0")
        port = out.get("port", DEFAULT_PORTS.get(t))
        if port in (None, ""):
            raise ApiError(1400, f"{TYPES[t][1]}需要 port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ApiError(1400, "port 必须是整数") from None
        if not 0 <= port <= 65535:
            raise ApiError(1400, "port 超出范围")
        out["port"] = port
    else:
        url = str(out.get("url") or "").strip()
        if not url:
            raise ApiError(1400, f"{TYPES[t][1]}需要 url")
        prefix = ("http://", "https://") if t == "http_post" else ("ws://",)
        if not url.startswith(prefix):
            raise ApiError(1400, f"{TYPES[t][1]}地址必须以 {' 或 '.join(prefix)} 开头")
        out["url"] = url
    if "token" in out:
        tok = str(out["token"] or "").strip()
        if any(c.isspace() for c in tok):
            raise ApiError(1400, "令牌不能包含空格或换行")
        out["token"] = tok
        if not tok:
            del out["token"]
    if spec.get("regenerate_token"):
        if t not in TOKEN_TYPES:
            raise ApiError(1400, f"{TYPES[t][1]}没有独立令牌")
        out["token"] = _new_token()
    if t in ("satori", "automatic") and out["host"] not in LOOPBACK and not out.get("token"):
        # 对外监听必须鉴权：没填就生成，显式清空则拒绝
        if explicit_token:
            raise ApiError(1400, f"监听 {out['host']} 时必须设置令牌；"
                                 "不需要鉴权请把监听地址改为 127.0.0.1")
        out["token"] = _new_token()
    if t == "satori":
        out["platform"] = str(out.get("platform") or "").strip() or "qq"
        out["webhooks"] = [dict(h) for h in spec.get("webhooks") or [] if h.get("url")]
    return out


class TransportManager:
    def __init__(self, dispatch, ws_hub, poster, media_root=None, routes=None,
                 access_token=None, log_buffer=None, self_id=0, specs_path=None,
                 bridge=None, legacy_protocols_path=None):
        self.dispatch = dispatch
        self.ws_hub = ws_hub
        self.poster = poster
        self.media_root = media_root
        self.routes = routes or {}
        self.access_token = access_token
        self.log_buffer = log_buffer
        self.self_id = self_id
        self.specs_path = specs_path
        self.bridge = bridge
        self.legacy_protocols_path = legacy_protocols_path
        self._lock = threading.RLock()
        self._next = 0
        self._items = {}

    # -- lifecycle of one channel ------------------------------------------------

    def _start(self, tid, spec, source="runtime"):
        """Bring one channel up; returns the runtime record (without id/meta)."""
        t = spec["type"]
        if t == "http":
            handler = make_api_handler(
                self.dispatch, self.access_token, ws_hub=self.ws_hub,
                media_root=self.media_root, routes=self.routes,
                log_buffer=self.log_buffer,
            )
            try:
                server = ThreadingHTTPServer((spec["host"], spec["port"]), handler)
            except OSError as exc:
                raise ApiError(1400, f"监听 {spec['host']}:{spec['port']} 失败: {exc}") from exc
            server.daemon_threads = True
            spec["port"] = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            logger.info(f"通道 {tid}: OneBot HTTP 监听 {spec['host']}:{spec['port']}")
            return {"server": server}
        if t == "ws_reverse":
            rec = {"stop": threading.Event(), "conn": None}

            def on_conn(conn):
                rec["conn"] = conn

            threading.Thread(
                target=reverse_ws_loop,
                args=(spec["url"], self.ws_hub, self.self_id, spec.get("token"), rec["stop"]),
                kwargs={"on_conn": on_conn}, daemon=True,
            ).start()
            logger.info(f"通道 {tid}: OneBot 反向 WS -> {spec['url']}")
            return rec
        if t == "http_post":
            if spec["url"] in self.poster.urls:
                # 启动参数里的地址构造 Poster 时就已加入
                if source != "startup":
                    raise ApiError(1400, f"上报地址已存在: {spec['url']}")
            else:
                self.poster.urls.append(spec["url"])
            logger.info(f"通道 {tid}: OneBot 事件 POST -> {spec['url']}")
            return {}
        if self.bridge is None:
            raise ApiError(1400, f"{TYPES[t][1]}不可用")
        if t == "satori":
            from .satori import SatoriServer

            server = SatoriServer(
                self.bridge, host=spec["host"], port=spec["port"], token=spec.get("token"),
                platform=spec["platform"], webhooks=spec["webhooks"],
            )
            server.on_webhooks_changed = lambda hooks: self._webhooks_changed(tid, hooks)
        else:
            from .automatic import AutomaticServer

            if t == "automatic":
                server = AutomaticServer(self.bridge, host=spec["host"], port=spec["port"],
                                         token=spec.get("token"))
            else:
                server = AutomaticServer(self.bridge, host=None, token=spec.get("token"),
                                         reverse_urls=[spec["url"]])
        server.name = f"{t}:{tid}"
        try:
            server.start()
        except OSError as exc:
            raise ApiError(1400, f"监听 {spec['host']}:{spec['port']} 失败: {exc}") from exc
        if t in LISTENERS:
            spec["port"] = server.port
        self.bridge.protocols.register(server)
        logger.info(f"通道 {tid}: {TYPES[t][1]} "
                    + (f"{spec['host']}:{spec['port']}" if t in LISTENERS else f"-> {spec['url']}"))
        return {"adapter": server}

    def _stop(self, rec):
        t = rec["spec"]["type"]
        if t == "http":
            rec["server"].shutdown()
            rec["server"].server_close()
        elif t == "ws_reverse":
            rec["stop"].set()
            if rec.get("conn") is not None:
                rec["conn"].close()
        elif t == "http_post":
            try:
                self.poster.urls.remove(rec["spec"]["url"])
            except ValueError:
                pass
        else:
            adapter = rec["adapter"]
            if self.bridge is not None:
                self.bridge.protocols.unregister(adapter.name)
            adapter.stop()

    def _webhooks_changed(self, tid, hooks):
        with self._lock:
            rec = self._items.get(tid)
            if rec is not None:
                rec["spec"]["webhooks"] = [dict(h) for h in hooks]
                self._persist()

    # -- API ---------------------------------------------------------------------

    def add_spec(self, spec, source="runtime"):
        """Create a channel from a spec dict; the action layer's entry point."""
        spec = normalize(dict(spec), explicit_token="token" in spec)
        with self._lock:
            self._next += 1
            tid = f"t{self._next}"
            rec = self._start(tid, spec, source)
            rec.update({"id": tid, "spec": spec, "source": source, "created_at": time.time()})
            self._items[tid] = rec
            if source != "startup":
                # startup 通道不进 specs 文件；启动序列在 restore() 之前跑，
                # 此时持久化只会把运行时的记录清成空表
                self._persist()
        return tid

    def add_http(self, host, port, source="runtime"):
        return self.add_spec({"type": "http", "host": host, "port": port}, source)

    def add_ws_reverse(self, url, token=None, source="runtime"):
        return self.add_spec({"type": "ws_reverse", "url": url, "token": token}, source)

    def add_post_url(self, url, source="runtime"):
        return self.add_spec({"type": "http_post", "url": url}, source)

    def _get(self, tid, writable=True):
        rec = self._items.get(str(tid))
        if rec is None:
            raise ApiError(1404, f"通道不存在: {tid}")
        if writable and rec["source"] == "startup":
            raise ApiError(1400, "启动配置的通道不能通过 API 修改或删除")
        return rec

    def update(self, tid, changes):
        """Edit a channel in place (same id): stop, restart with the merged
        spec, roll back to the old one if the new one fails to come up."""
        with self._lock:
            rec = self._get(tid)
            old = rec["spec"]
            t = old["type"]
            bad = set(changes) - FIELDS[t] - {"regenerate_token"}
            if bad:
                raise ApiError(1400, f"{TYPES[t][1]}不能修改的字段: {', '.join(sorted(bad))}")
            merged = {**old, **changes, "type": t}
            spec = normalize(merged, explicit_token="token" in changes)
            self._stop(rec)
            try:
                fresh = self._start(rec["id"], spec)
            except ApiError:
                try:
                    fresh = self._start(rec["id"], old)
                    fresh.update({k: rec[k] for k in ("id", "source", "created_at")}, spec=old)
                    self._items[rec["id"]] = fresh
                except ApiError as exc:
                    logger.error(f"通道 {rec['id']} 恢复旧配置失败，已移除: {exc}")
                    del self._items[rec["id"]]
                    self._persist()
                raise
            fresh.update({k: rec[k] for k in ("id", "source", "created_at")}, spec=spec)
            self._items[rec["id"]] = fresh
            self._persist()
        return rec["id"]

    def remove(self, tid):
        with self._lock:
            rec = self._get(tid)
            self._stop(rec)
            del self._items[rec["id"]]
            self._persist()

    def token(self, tid):
        rec = self._get(tid, writable=False)
        if rec["spec"]["type"] not in TOKEN_TYPES:
            raise ApiError(1400, "该通道使用全局 access_token")
        return rec["spec"].get("token") or ""

    # -- introspection -------------------------------------------------------------

    def list(self):
        out = []
        with self._lock:
            items = list(self._items.values())
        for rec in items:
            spec = rec["spec"]
            t = spec["type"]
            protocol, title = TYPES[t]
            entry = {
                "id": rec["id"], "type": t, "protocol": protocol, "title": title,
                "source": rec["source"], "created_at": int(rec["created_at"]),
                **{k: v for k, v in spec.items() if k not in ("type", "token", "webhooks")},
            }
            if t in TOKEN_TYPES:
                entry["token_set"] = bool(spec.get("token"))
            hostport = f"{spec.get('host')}:{spec.get('port')}"
            if t == "http":
                entry.update({"address": hostport, "http_url": f"http://{hostport}/",
                              "ws_url": f"ws://{hostport}/", "status": "listening"})
            elif t == "ws_reverse":
                conn = rec.get("conn")
                entry.update({"address": spec["url"], "status":
                              "connected" if conn is not None and conn.alive else "connecting"})
            elif t == "http_post":
                entry.update({"address": spec["url"], "status": "active"})
            else:
                info = rec["adapter"].info()
                entry["clients"] = info.get("clients", 0)
                if t == "satori":
                    entry.update({"address": hostport, "endpoint": f"http://{hostport}/v1",
                                  "status": "listening", "webhooks": len(spec["webhooks"])})
                elif t == "automatic":
                    entry.update({"address": hostport,
                                  "endpoint": f"ws://{hostport}/automatic/v1",
                                  "status": "listening"})
                else:
                    entry.update({"address": spec["url"], "status":
                                  "connected" if entry["clients"] else "connecting"})
            out.append(entry)
        return out

    # -- persistence ------------------------------------------------------------------

    def _persist(self):
        if not self.specs_path:
            return
        specs = [dict(r["spec"]) for r in self._items.values() if r["source"] == "runtime"]
        tmp = self.specs_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(specs, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.specs_path)
        except OSError as exc:
            logger.warning(f"通道持久化写入失败: {exc}")

    def restore(self):
        """Re-create persisted runtime channels after a restart."""
        specs = []
        if self.specs_path and os.path.exists(self.specs_path):
            try:
                with open(self.specs_path, encoding="utf-8") as fh:
                    specs = json.load(fh) or []
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(f"通道持久化文件读取失败: {exc}")
        legacy = self.legacy_protocols_path
        migrated = self._legacy_specs()
        generated = False
        for spec in specs + migrated:
            try:
                tid = self.add_spec(spec)
            except ApiError as exc:
                logger.warning(f"恢复通道 {spec.get('type')} 失败: {exc}")
                continue
            if self._items[tid]["spec"].get("token") and not spec.get("token"):
                generated = True
                logger.warning(f"通道 {tid} 监听非回环地址但未设置令牌，已自动生成"
                               "（在 WebUI「通道管理」中查看）")
        if generated:
            self._persist()
        if legacy and os.path.exists(legacy):
            self._persist()
            os.replace(legacy, legacy + ".migrated")
            logger.info(f"旧的协议配置已迁移为 {len(migrated)} 个通道")

    def _legacy_specs(self):
        """旧版本的 ``<db>.protocols.json``（每个协议一份配置）-> 通道列表。
        只迁移已启用的协议。"""
        path = self.legacy_protocols_path
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh) or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"旧协议配置读取失败，跳过迁移: {exc}")
            return []
        specs = []
        sat = old.get("satori") or {}
        if sat.get("enabled"):
            specs.append({"type": "satori", "host": sat.get("host"), "port": sat.get("port"),
                          "token": sat.get("token"), "platform": sat.get("platform"),
                          "webhooks": sat.get("webhooks") or []})
        auto = old.get("automatic") or {}
        if auto.get("enabled"):
            specs.append({"type": "automatic", "host": auto.get("host"),
                          "port": auto.get("port"), "token": auto.get("token")})
            for url in auto.get("reverse_urls") or []:
                specs.append({"type": "automatic_reverse", "url": url,
                              "token": auto.get("token")})
        # 旧配置里的空令牌表示「未设置」，不是显式清空
        return [{k: v for k, v in sp.items() if k != "token" or v} for sp in specs]
