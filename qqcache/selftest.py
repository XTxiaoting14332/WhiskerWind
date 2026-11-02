"""分阶段链路自检：按真实入站链路逐段验证，报告断在哪一级。

每级镜像真实链路上的一跳：

    Notify ──► ActionInvoked/打开会话 ──► uin 归因 ──► 读树 emit
    (D-Bus)      (open_conversation)     (peer.uin_of)  (poll_emit)

notify 一级是真的往自己持有的总线名发一条探针通知（summary 用保留值
``SELFTEST_SUMMARY``，Service 收到后只落库不进 worker，避免再触发一遍
归因链）；open/attribute/emit 直接跑 on_notification 内部的同一批函数。

报告三处去向：返回值给调用方（API/CLI）、loguru 逐段落日志缓冲
（WebUI 日志页实时可见）、追加写 ``<db>.selftest.jsonl`` 供事后翻案。
"""

import json
import time

from loguru import logger

from . import login
from .notify import SELFTEST_SUMMARY, Store
from .send import (
    _names_match,
    current_conversation,
    open_conversation,
)


class StageFail(Exception):
    """一级自检失败——报告记录为断点，后续段不再跑。"""


def _session_bus():
    import gi

    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib

    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return conn, Gio, GLib


def _st_atspi(bridge, env):
    app = bridge._app()
    if app is None:
        raise StageFail("QQ 不在无障碍树里（AT-SPI 未就绪或 QQ 未运行）")
    env["app"] = app
    return "QQ 已接入无障碍树"


def _st_login(bridge, env):
    state = login.login_state(env["app"])
    if state != "logged_in":
        raise StageFail(f"登录态异常: {state}（需要 logged_in）")
    return "logged_in"


def _st_notify_bus(bridge, env):
    try:
        conn, Gio, GLib = _session_bus()
        res = conn.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", ("org.freedesktop.Notifications",)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE, 3000, None,
        )
        owned = bool(res and res.unpack()[0])
    except Exception as exc:
        raise StageFail(f"查询通知总线失败: {exc}") from exc
    if not owned:
        raise StageFail("org.freedesktop.Notifications 无人持有——通知服务未运行或总线名被抢")
    env["bus"] = (conn, Gio, GLib)
    return "总线名已持有"


def _st_notify(bridge, env):
    """真发一条探针通知走完整 D-Bus 链路，验证 Notify->落库这一跳。"""
    conn, Gio, GLib = env["bus"]
    try:
        res = conn.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                ("qqcache-selftest", 0, "", SELFTEST_SUMMARY, "自检探针", [], {}, 0),
            ),
            GLib.VariantType.new("(u)"),
            Gio.DBusCallFlags.NONE, 3000, None,
        )
        nid = res.unpack()[0]
    except Exception as exc:
        raise StageFail(f"Notify 调用失败: {exc}") from exc

    deadline = time.monotonic() + 3.0
    with Store(bridge.db_path) as store:
        row = None
        while time.monotonic() < deadline:
            row = store.db.execute(
                "SELECT id FROM notifications WHERE summary = ?"
                " ORDER BY id DESC LIMIT 1",
                (SELFTEST_SUMMARY,),
            ).fetchone()
            if row:
                break
            time.sleep(0.15)
        if row is None:
            raise StageFail("Notify 已返回 id 但通知没落库")
        # 探针行不进 worker（Service 跳过了入队），processed_at 会留在
        # NULL——不标掉的话重启回放会把它当崩溃残留重放一遍
        store.mark_processed(row[0])
    return f"Notify id={nid} → 通知行 #{row[0]}"


def _st_open(env, conv, timeout):
    app = env["app"]
    if not open_conversation(app, conv, timeout=timeout):
        raise StageFail(f"会话 {conv!r} 打不开（侧栏没有，搜索也无结果）")
    name = current_conversation(app)
    if not (name and _names_match(name, conv)):
        raise StageFail(f"打开后读到 {name!r}，与 {conv!r} 不符")
    env["name"] = name
    return f"已打开 {name!r}"


def _st_attribute(bridge, env, conv):
    uin, kind = bridge._resolve_uin(env["app"], env["name"], conv)
    if not uin:
        raise StageFail("uin 归因失败（卡校验拒收 / 归因竞态丢弃 / 60s 冷却中）")
    env["uin"], env["kind"] = str(uin), kind
    return f"{env['name']} → {uin} ({kind})"


def _st_emit(bridge, env):
    posted = bridge.poll_emit(
        env["app"], env["uin"], env["kind"], notified=True
    )
    return f"emit {posted} 条" if posted else "emit 0 条（无新消息或树仍在切换）"


def _stage_list(bridge, conv, open_timeout):
    return [
        ("atspi", lambda e: _st_atspi(bridge, e)),
        ("login", lambda e: _st_login(bridge, e)),
        ("notify_bus", lambda e: _st_notify_bus(bridge, e)),
        ("notify", lambda e: _st_notify(bridge, e)),
        ("open", lambda e: _st_open(e, conv, open_timeout)),
        ("attribute", lambda e: _st_attribute(bridge, e, conv)),
        ("emit", lambda e: _st_emit(bridge, e)),
    ]


def _run_stage(report, name, fn, env):
    t0 = time.monotonic()
    try:
        detail = fn(env) or "ok"
        ok = True
    except Exception as exc:
        detail, ok = str(exc), False
    ms = int((time.monotonic() - t0) * 1000)
    report["stages"].append(
        {"name": name, "ok": ok, "ms": ms, "detail": detail}
    )
    if ok:
        logger.success(f"自检 [{name}] {detail} ({ms}ms)")
    else:
        report["ok"] = False
        report["failed_stage"] = name
        logger.warning(f"自检 [{name}] 断点: {detail} ({ms}ms)")
    return ok


def _persist(bridge, report):
    if bridge.db_path == ":memory:":
        return
    try:
        with open(bridge.db_path + ".selftest.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.debug(f"自检报告落盘失败: {exc}")


def run(bridge, conv, open_timeout=8.0):
    """跑一遍入站链路自检；返回报告 dict。全程持 gui_lock——与
    on_notification 同一排他域，真实通知处理会让位到自检结束。"""
    conv = (conv or "").strip()
    if not conv:
        raise ValueError("缺少目标会话名 conv")

    report = {
        "conv": conv,
        "started_at": time.time(),
        "ok": True,
        "failed_stage": None,
        "stages": [],
    }
    logger.warning(f"自检开始: 目标会话 {conv!r}")
    env = {}
    with bridge.gui_lock:
        for name, fn in _stage_list(bridge, conv, open_timeout):
            if not _run_stage(report, name, fn, env):
                break
    report["ms_total"] = sum(s["ms"] for s in report["stages"])
    verdict = "全部通过" if report["ok"] else f"断在 {report['failed_stage']}"
    logger.warning(f"自检结束: {verdict}（共 {report['ms_total']}ms）")
    _persist(bridge, report)
    return report
