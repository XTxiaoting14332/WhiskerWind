"""Runtime management of the outward protocols (which are on, and how).

OneBot v11 is always on: the WebUI BFF itself talks to the service over it,
and its endpoints are managed as transports. Every other protocol has a small
persisted config (``<db>.protocols.json``) and can be started, stopped or
reconfigured without restarting the service.
"""

import json
import os
import secrets
import threading

from loguru import logger

from .ob11 import ApiError

DEFAULTS = {
    "satori": {"enabled": False, "host": "0.0.0.0", "port": 5500, "token": "",
               "platform": "qq", "webhooks": []},
    "automatic": {"enabled": False, "host": "0.0.0.0", "port": 5600, "token": "",
                  "reverse_urls": []},
}
EDITABLE = {
    "satori": {"enabled", "host", "port", "token", "platform"},
    "automatic": {"enabled", "host", "port", "token", "reverse_urls"},
}
TITLES = {"satori": "Satori", "automatic": "automatic"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _needs_token(cfg):
    """启用且监听非回环地址的协议必须有令牌。"""
    return bool(cfg.get("enabled")) and str(cfg.get("host") or "") not in LOOPBACK


class ProtocolManager:
    def __init__(self, bridge, path=None):
        self.bridge = bridge
        self.path = path
        self._lock = threading.Lock()
        self._running = {}   # name -> server
        self.config = {k: dict(v) for k, v in DEFAULTS.items()}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                for name, cfg in saved.items():
                    if name in self.config and isinstance(cfg, dict):
                        self.config[name].update(cfg)
            except (OSError, ValueError) as exc:
                logger.warning(f"协议配置读取失败，用默认值: {exc}")

    def _persist(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def start_enabled(self):
        generated = False
        for name, cfg in self.config.items():
            if _needs_token(cfg) and not cfg.get("token"):
                # 旧版本保存的无令牌配置：补一个令牌再启动
                cfg["token"] = secrets.token_urlsafe(24)
                generated = True
                logger.warning(f"协议 {name} 监听 {cfg['host']} 但未设置令牌，已自动生成"
                               "（在 WebUI「传输管理」中查看）")
        if generated:
            self._persist()
        for name, cfg in self.config.items():
            if cfg.get("enabled"):
                try:
                    self._start(name)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"协议 {name} 启动失败: {exc}")

    def _start(self, name):
        cfg = self.config[name]
        if name == "satori":
            from .satori import SatoriServer

            server = SatoriServer(
                self.bridge, host=cfg["host"], port=cfg["port"], token=cfg.get("token"),
                platform=cfg.get("platform") or "qq", webhooks=cfg.get("webhooks"),
            )
            server.on_webhooks_changed = lambda hooks: self._save_webhooks(name, hooks)
            try:
                server.start()
            except OSError as exc:
                raise ApiError(1400, f"Satori 监听 {cfg['host']}:{cfg['port']} 失败: {exc}") from exc
            self._running[name] = server
            self.bridge.protocols.register(server)
            return server
        if name == "automatic":
            from .automatic import AutomaticServer

            server = AutomaticServer(
                self.bridge, host=cfg["host"], port=cfg["port"], token=cfg.get("token"),
                reverse_urls=cfg.get("reverse_urls"),
            )
            try:
                server.start()
            except OSError as exc:
                raise ApiError(1400, f"automatic 监听 {cfg['host']}:{cfg['port']} 失败: {exc}") from exc
            self._running[name] = server
            self.bridge.protocols.register(server)
            return server
        raise ApiError(1404, f"未知协议 {name}")

    def _stop(self, name):
        server = self._running.pop(name, None)
        if server is not None:
            self.bridge.protocols.unregister(name)
            server.stop()

    def _save_webhooks(self, name, hooks):
        with self._lock:
            self.config[name]["webhooks"] = [dict(h) for h in hooks]
            self._persist()

    def list(self):
        out = [{
            "name": "onebot11", "title": "OneBot v11", "enabled": True, "fixed": True,
            "running": True, "note": "WebUI 依赖此协议，始终启用；传输通道在「传输管理」中配置",
        }]
        with self._lock:
            for name, cfg in self.config.items():
                server = self._running.get(name)
                item = {"name": name, "title": TITLES.get(name, name),
                        "fixed": False, "running": server is not None,
                        **{k: v for k, v in cfg.items() if k != "token"},
                        "token_set": bool(cfg.get("token"))}
                if server is not None:
                    item.update(server.info())
                out.append(item)
        return out

    def update(self, name, changes):
        if name == "onebot11":
            raise ApiError(1400, "OneBot v11 始终启用，不能停用（WebUI 依赖此协议）")
        if name not in self.config:
            raise ApiError(1404, f"未知协议 {name}")
        bad = set(changes) - EDITABLE[name] - {"regenerate_token"}
        if bad:
            raise ApiError(1400, f"不能修改的字段: {', '.join(sorted(bad))}")
        with self._lock:
            new = dict(self.config[name])
            new.update({k: v for k, v in changes.items() if k in EDITABLE[name]})
            if "token" in changes:
                tok = str(changes.get("token") or "").strip()
                if any(c.isspace() for c in tok):
                    raise ApiError(1400, "令牌不能包含空格或换行")
                new["token"] = tok
            if changes.get("regenerate_token"):
                new["token"] = secrets.token_urlsafe(24)
            if _needs_token(new) and not new.get("token"):
                if "token" in changes:
                    raise ApiError(1400, f"监听 {new['host']} 时必须设置令牌；"
                                         "不需要鉴权请把监听地址改为 127.0.0.1")
                new["token"] = secrets.token_urlsafe(24)
            try:
                new["port"] = int(new["port"])
            except (TypeError, ValueError):
                raise ApiError(1400, "port 必须是整数")
            if not 1 <= new["port"] <= 65535:
                raise ApiError(1400, "port 超出范围")
            new["enabled"] = bool(new.get("enabled"))
            if "reverse_urls" in new:
                urls = new["reverse_urls"]
                if isinstance(urls, str):
                    urls = [u.strip() for u in urls.replace(",", "\n").splitlines()]
                urls = [u for u in (urls or []) if u]
                bad = [u for u in urls if not u.startswith("ws://")]
                if bad:
                    raise ApiError(1400, f"反向 ws 地址只支持 ws://: {', '.join(bad)}")
                new["reverse_urls"] = urls
            old = self.config[name]
            self.config[name] = new
            try:
                self._stop(name)
                if new["enabled"]:
                    self._start(name)
            except ApiError:
                # 新配置起不来：回滚并尽量恢复旧的运行状态
                self.config[name] = old
                if old.get("enabled"):
                    try:
                        self._start(name)
                    except Exception:  # noqa: BLE001
                        pass
                raise
            self._persist()
        logger.warning(f"协议 {name}: {'已启用' if new['enabled'] else '已停用'}"
                       + (f" @ {new['host']}:{new['port']}" if new["enabled"] else ""))
        return self.list()

    def token(self, name):
        return self.config.get(name, {}).get("token") or ""

    def stop_all(self):
        for name in list(self._running):
            self._stop(name)
