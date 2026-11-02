"""Runtime-managed transports for the OneBot service.

Three kinds of plumbing the WebUI can add/remove without a restart:

- ``http``        — an extra API listener (HTTP actions + forward ws on one port)
- ``ws_reverse``  — an outbound ws client to a framework's universal endpoint
- ``http_post``   — an extra event POST url

Runtime-added transports persist to a small JSON file next to the database so
they come back after a restart. Startup-configured transports (CLI flags /
env) are marked ``source: "startup"``: they are never persisted and cannot be
removed through the API — deleting the channel you're talking through is a
foot-gun.
"""

import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

from loguru import logger

from .ob11 import ApiError, make_api_handler, reverse_ws_loop


class TransportManager:
    def __init__(self, dispatch, ws_hub, poster, media_root=None, routes=None,
                 access_token=None, log_buffer=None, self_id=0, specs_path=None):
        self.dispatch = dispatch
        self.ws_hub = ws_hub
        self.poster = poster
        self.media_root = media_root
        self.routes = routes or {}
        self.access_token = access_token
        self.log_buffer = log_buffer
        self.self_id = self_id
        self.specs_path = specs_path
        self._lock = threading.Lock()
        self._next = 0
        self._items = {}

    # -- creation -------------------------------------------------------------

    def _register(self, rec):
        with self._lock:
            self._next += 1
            tid = f"t{self._next}"
            rec["id"] = tid
            rec["created_at"] = time.time()
            self._items[tid] = rec
        return tid

    def add_http(self, host, port, source="runtime"):
        handler = make_api_handler(
            self.dispatch, self.access_token, ws_hub=self.ws_hub,
            media_root=self.media_root, routes=self.routes,
            log_buffer=self.log_buffer,
        )
        try:
            server = ThreadingHTTPServer((host, int(port)), handler)
        except OSError as exc:
            raise ApiError(1400, f"监听 {host}:{port} 失败: {exc}") from exc
        server.daemon_threads = True
        tid = self._register({
            "type": "http", "host": host, "port": server.server_address[1],
            "source": source, "server": server,
        })
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if source != "startup":
            # startup 传输不进 specs 文件；启动序列在 restore() 之前跑，
            # 此时持久化只会把运行时的记录清成空表
            self._persist()
        logger.info(f"传输 {tid}: http 监听 {host}:{server.server_address[1]}")
        return tid

    def add_ws_reverse(self, url, token=None, source="runtime"):
        rec = {
            "type": "ws_reverse", "url": url, "token": token, "source": source,
            "stop": threading.Event(), "conn": None,
        }
        tid = self._register(rec)

        def on_conn(conn):
            rec["conn"] = conn

        rec["thread"] = threading.Thread(
            target=reverse_ws_loop,
            args=(url, self.ws_hub, self.self_id, token, rec["stop"]),
            kwargs={"on_conn": on_conn},
            daemon=True,
        )
        rec["thread"].start()
        if source != "startup":
            self._persist()
        logger.info(f"传输 {tid}: 反向 WS -> {url}")
        return tid

    def add_post_url(self, url, source="runtime"):
        """Extra event-POST destination; startup urls are already in Poster."""
        existing = url in self.poster.urls
        if existing and source != "startup":
            raise ApiError(1400, f"上报地址已存在: {url}")
        if not existing:
            self.poster.urls.append(url)
        tid = self._register({"type": "http_post", "url": url, "source": source})
        if source != "startup":
            self._persist()
        logger.info(f"传输 {tid}: 事件 POST -> {url}")
        return tid

    # -- removal --------------------------------------------------------------

    def remove(self, tid):
        rec = self._items.get(str(tid))
        if rec is None:
            raise ApiError(1404, f"传输不存在: {tid}")
        if rec["source"] == "startup":
            raise ApiError(1400, "启动配置的传输不能通过 API 删除")
        if rec["type"] == "http":
            rec["server"].shutdown()
            rec["server"].server_close()
        elif rec["type"] == "ws_reverse":
            rec["stop"].set()
            conn = rec.get("conn")
            if conn is not None:
                conn.close()
        elif rec["type"] == "http_post":
            try:
                self.poster.urls.remove(rec["url"])
            except ValueError:
                pass
        del self._items[rec["id"]]
        self._persist()

    # -- introspection ----------------------------------------------------------

    def list(self):
        out = []
        for rec in self._items.values():
            entry = {
                "id": rec["id"],
                "type": rec["type"],
                "source": rec["source"],
                "created_at": int(rec["created_at"]),
            }
            if rec["type"] == "http":
                entry.update({
                    "address": f"{rec['host']}:{rec['port']}",
                    "http_url": f"http://{rec['host']}:{rec['port']}/",
                    "ws_url": f"ws://{rec['host']}:{rec['port']}/",
                    "status": "listening",
                })
            elif rec["type"] == "ws_reverse":
                conn = rec.get("conn")
                entry.update({
                    "address": rec["url"],
                    "status": "connected" if conn is not None and conn.alive else "connecting",
                })
            else:
                entry.update({"address": rec["url"], "status": "active"})
            out.append(entry)
        return out

    # -- persistence --------------------------------------------------------------

    def _specs(self):
        specs = []
        for rec in self._items.values():
            if rec["source"] != "runtime":
                continue
            if rec["type"] == "http":
                specs.append({"type": "http", "host": rec["host"], "port": rec["port"]})
            elif rec["type"] == "ws_reverse":
                spec = {"type": "ws_reverse", "url": rec["url"]}
                if rec.get("token"):
                    spec["token"] = rec["token"]
                specs.append(spec)
            elif rec["type"] == "http_post":
                specs.append({"type": "http_post", "url": rec["url"]})
        return specs

    def _persist(self):
        if not self.specs_path:
            return
        tmp = self.specs_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._specs(), fh, ensure_ascii=False)
            os.replace(tmp, self.specs_path)
            os.chmod(self.specs_path, 0o600)
        except OSError as exc:
            logger.warning(f"传输持久化写入失败: {exc}")

    def restore(self):
        """Re-create persisted runtime transports after a restart."""
        if not self.specs_path or not os.path.exists(self.specs_path):
            return
        try:
            with open(self.specs_path, encoding="utf-8") as fh:
                specs = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"传输持久化文件读取失败: {exc}")
            return
        for spec in specs or []:
            try:
                self.add_spec(spec)
            except ApiError as exc:
                logger.warning(f"恢复传输 {spec} 失败: {exc}")

    def add_spec(self, spec, source="runtime"):
        """One spec dict -> add call; also the action layer's entry point."""
        t = spec.get("type")
        if t == "http":
            if spec.get("port") in (None, ""):
                raise ApiError(1400, "http 传输需要 port")
            return self.add_http(spec.get("host") or "127.0.0.1", spec["port"], source)
        if t == "ws_reverse":
            if not spec.get("url"):
                raise ApiError(1400, "ws_reverse 传输需要 url")
            return self.add_ws_reverse(spec["url"], token=spec.get("token"), source=source)
        if t == "http_post":
            if not spec.get("url"):
                raise ApiError(1400, "http_post 传输需要 url")
            return self.add_post_url(spec["url"], source=source)
        raise ApiError(1400, f"未知传输类型: {t!r}（http / ws_reverse / http_post）")
