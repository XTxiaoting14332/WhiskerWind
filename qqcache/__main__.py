"""CLI: python -m qqcache <command>"""

import argparse
import os
import sys
import warnings

from loguru import logger

warnings.simplefilter("ignore", DeprecationWarning)

from . import atspi
from .cursor import Cursor
from .media import MediaIndex, correlate, find_nt_data
from .messages import parse_messages
from .send import SendError, current_conversation, send
from .store import Cache
from .sync import ScrapeError, list_conversations, sync

DEFAULT_DB = os.environ.get("QQCACHE_DB", "qqcache.sqlite3")


def find_qq_pid():
    candidates = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0]:
            continue
        if b"--type=" in b" ".join(argv):
            continue
        if os.path.basename(argv[0].decode("utf-8", "replace")) != "qq":
            continue
        candidates.append(int(entry))
    # QQ 被挤下线/更新后会用 --relaunch 自我重启，加上守护进程补拉，可能
    # 同时存在两个主进程：一个只剩登录窗，一个已经登录。优先选已登录的那个，
    # 否则选第一个在 a11y 上可见的
    from . import login

    visible = []
    for pid in candidates:
        app = atspi.find_app(pid)
        if app is None:
            continue
        visible.append(pid)
        if len(candidates) > 1 and login.login_state(app) == "logged_in":
            return pid
    if visible:
        return visible[0]
    return candidates[0] if candidates else None


def cmd_sync(args):
    pid = args.pid or find_qq_pid()
    if pid is None:
        logger.error("找不到 QQ 进程，用 --pid 指定")
        return 2
    ttl = args.ttl * 86400 if args.ttl is not None else None
    with Cache(args.db) as cache:
        try:
            updated, skipped, failed = sync(
                cache, pid, ttl=ttl, force=args.force, limit=args.limit, verbose=not args.quiet
            )
        except ScrapeError as exc:
            logger.error(f"错误: {exc}")
            return 1
    print(f"新增/更新 {updated}，跳过 {skipped}，失败 {failed}")
    return 0


def cmd_list(args):
    with Cache(args.db) as cache:
        rows = cache.conversations()
    if not rows:
        print("缓存为空，先跑 `python -m qqcache sync`")
        return 0
    width = max((len(r["name_clean"]) for r in rows), default=8)
    for r in rows:
        print(f"{r['uin']:<12} {r['kind']:<6} {r['name_clean']:<{width}}  x{r['fetch_count']}")
    return 0


def cmd_resolve(args):
    with Cache(args.db) as cache:
        hits = cache.resolve(args.name)
        if not hits:
            print("未命中")
            return 1
        for uin in hits:
            row = cache.lookup(uin)
            print(f"{uin}  {row['kind']}  {row['name_clean']}")
        if len(hits) > 1:
            print(f"注意: {args.name!r} 对应 {len(hits)} 个会话，无法唯一解析")
    return 0


def cmd_stats(args):
    with Cache(args.db) as cache:
        print(cache.stats())
    return 0


def cmd_conversations(args):
    pid = args.pid or find_qq_pid()
    if pid is None:
        logger.error("找不到 QQ 进程")
        return 2
    app = atspi.find_app(pid)
    if app is None:
        logger.error("QQ 不在无障碍树里")
        return 1
    with Cache(args.db) as cache:
        for name, _ in list_conversations(app):
            uin = cache.get(name)
            mark = uin or ("<歧义>" if cache.ambiguous(name) else "-")
            print(f"{mark:<12} {name}")
    return 0


def current_conversation_name(app):
    return current_conversation(app)


def cmd_notify(args):
    import time as _time

    from .notify import Service, Store, monitor

    store = Store(args.db)
    cache = Cache(args.db)
    pid = args.pid or find_qq_pid()

    def resolver(name):
        uin = cache.get(name)
        if not uin:
            return None
        row = cache.lookup(uin)
        return row["kind"] if row else None

    attempted = {}  # summary -> last attempt monotonic；失败冷却 60s 后可重试

    def attributor(summary):
        """Runs after ActionInvoked made QQ open the notification's chat.

        Verify the switched conversation really is ``summary`` before
        attributing — a send/user switching chats mid-flight must not write a
        wrong uin into the cache.
        """
        from . import peer
        from .send import _names_match

        if cache.get(summary) or _time.monotonic() - attempted.get(summary, -61) < 60:
            return
        attempted[summary] = _time.monotonic()
        app = atspi.find_app(pid)
        if app is None:
            return
        deadline = _time.monotonic() + 4.0
        name = None
        while _time.monotonic() < deadline:
            name = current_conversation(app)
            if name and _names_match(name, summary):
                break
            name = None
            _time.sleep(0.2)
        if not name:
            return
        try:
            ident = peer.uin_of(app)
        except peer.PeerError:
            return
        if ident.uin:
            cache.record(name, ident.uin, kind=ident.kind)
            if name != summary:
                cache.record(summary, ident.uin, kind=ident.kind)
            if not args.quiet:
                logger.success(f"归因: {summary!r} -> {ident.uin} ({ident.kind})")
        # Invoke 把窗口弹起聚焦了 -> 通知通道被抑制，收回去让它恢复
        from .send import _main_window_id

        wid = _main_window_id()
        if wid is not None:
            import subprocess as _sp

            _sp.run(["xdotool", "windowminimize", wid], capture_output=True, timeout=2)

    if not args.quiet:
        logger.info("等待通知… (Ctrl+C 退出)")
    try:
        if args.monitor:
            monitor(store, verbose=not args.quiet, resolver=resolver)
        else:
            Service(
                store,
                verbose=not args.quiet,
                resolver=resolver,
                attributor=attributor if pid else None,
            ).run()
    finally:
        cache.close()
        store.close()
    return 0


def cmd_events(args):
    import json

    from .notify import Store

    with Store(args.db) as store:
        rows = store.recent(args.limit, args.since)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("还没有通知")
        return 0
    for row in reversed(rows):
        flags = ""
        if row["mention"]:
            flags += " @我"
        if row["truncated"]:
            flags += " 截断"
        who = row["sender_name"] or "-"
        print(f"{row['id']:<5} {row['summary'][:18]:<18} {who[:12]:<12} {row['content'][:44]!r}{flags}")
    return 0


def cmd_send(args):
    pid = args.pid or find_qq_pid()
    if pid is None:
        logger.error("找不到 QQ 进程")
        return 2
    app = atspi.find_app(pid)
    if app is None:
        logger.error("QQ 不在无障碍树里")
        return 1

    try:
        from .send import parse_segments

        segments = parse_segments(args.text)
        segments += [("at", u) for u in args.at]
        with Cache(args.db) as cache:
            result = send(
                app,
                name=args.name,
                uin=args.uin,
                cache=cache,
                text=args.text,
                dry_run=args.dry_run,
                verify=not args.no_verify,
                verify_identity=not args.no_identity_check,
                segments=segments,
                reply_to=args.reply_to,
                images=args.image,
            )
            if args.uin:
                # 发送成功说明身份校验已通过，顺手把显示名映射沉淀进缓存。
                name = current_conversation(app)
                if name:
                    cache.record(name, args.uin, kind="unknown")
    except SendError as exc:
        logger.error(f"错误: {exc}")
        return 1

    if isinstance(result, dict):
        print(f"演练完成: {result}")
        return 0
    if result is None:
        logger.warning("已发送，但没有确认到回显")
        return 3
    print(f"已发送 msg_id={result.msg_id} timestamp={result.timestamp}")
    return 0


def cmd_poll(args):
    import json

    pid = args.pid or find_qq_pid()
    if pid is None:
        logger.error("找不到 QQ 进程")
        return 2
    app = atspi.find_app(pid)
    if app is None:
        logger.error("QQ 不在无障碍树里")
        return 1
    message_list = atspi.find_one(atspi.ui_root(app), role="embedded", name="消息列表")
    if message_list is None:
        logger.error("找不到「消息列表」，先打开一个会话")
        return 1

    with Cache(args.db) as cache, Cursor(args.db) as cursor:
        name = current_conversation_name(app)
        if name is None:
            logger.error("当前没有打开的会话")
            return 1
        uin = cache.get(name)
        if uin is None and not args.no_resolve:
            try:
                from . import peer

                identity = peer.uin_of(app)
            except Exception as exc:
                logger.warning(f"资料卡解析失败: {exc}")
            else:
                if identity.uin:
                    cache.record(name, identity.uin, kind=identity.kind)
                    uin = identity.uin
        uin = uin or name

        messages = parse_messages(atspi.dump_tree(message_list))
        anchor = cursor.seen(uin)
        result = cursor.filter_new(uin, messages)
        cursor.commit(uin, messages)

        emit = [] if (result.first_run and args.bootstrap == "skip") else list(result.new)
        backfilled = None
        if result.gap and not args.no_backfill:
            import subprocess as _sp
            import time as _time

            from . import backfill
            from .send import _main_window_id, activate_qq

            if activate_qq():
                _time.sleep(0.3)
            backfilled = backfill.backfill(app, cursor, uin, anchor=anchor)
            emit = backfilled["recovered"] + emit
            tail = backfill.scroll_to_bottom(app)
            tail_result = cursor.filter_new(uin, tail)
            cursor.commit(uin, tail)
            emitted_ids = {m.msg_id for m in emit}
            emit += [m for m in tail_result.new if m.msg_id not in emitted_ids]
            wid = _main_window_id()
            if wid is not None:
                _sp.run(["xdotool", "windowminimize", wid], capture_output=True, timeout=2)

        links, leftover = {}, 0
        nt_data = find_nt_data()
        if nt_data:
            with MediaIndex(nt_data, args.db) as media_index:
                fresh = media_index.scan()
                initial = media_index.last_scan_was_initial
            if not initial:
                links, leftover = correlate(emit, fresh)

        if args.json:
            payload = {
                "uin": uin,
                "name": name,
                "gap": result.gap,
                "backfill": (
                    {
                        "stitched": backfilled["stitched"],
                        "steps": backfilled["steps"],
                        "chain_broken": backfilled["chain_broken"],
                        "recovered": len(backfilled["recovered"]),
                    }
                    if backfilled
                    else None
                ),
                "first_run": result.first_run,
                "unmatched_media": leftover,
                "messages": [
                    {**m.as_dict(), "media": links[m.msg_id].as_dict() if m.msg_id in links else None}
                    for m in emit
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if result.first_run and args.bootstrap == "skip":
            print(f"{name} ({uin}): 首次读取，{len(messages)} 条已记为已见")
            return 0
        if result.gap:
            if backfilled is None:
                logger.warning("视口最旧消息不在已见集合里，两次轮询之间可能漏消息")
            elif not backfilled["stitched"]:
                logger.warning(
                    f"回填 {backfilled['steps']} 步后仍未接上已见历史"
                    + ("，且帧间出现跳窗" if backfilled["chain_broken"] else "")
                )
        for m in emit:
            media = links.get(m.msg_id)
            suffix = f"  <{os.path.basename(media.path)}>" if media else ""
            print(f"{m.msg_id} {m.kind:<7} {m.sender[:12]:<12} {m.content[:50]!r}{suffix}")
        print(f"新增 {len(emit)} 条，未匹配媒体 {leftover} 个")
    return 0


def cmd_messages(args):
    import json

    pid = args.pid or find_qq_pid()
    if pid is None:
        logger.error("找不到 QQ 进程")
        return 2
    app = atspi.find_app(pid)
    if app is None:
        logger.error("QQ 不在无障碍树里")
        return 1
    message_list = atspi.find_one(atspi.ui_root(app), role="embedded", name="消息列表")
    if message_list is None:
        logger.error("找不到「消息列表」，先打开一个会话")
        return 1

    tree = atspi.dump_tree(message_list)
    if args.dump:
        os.makedirs(os.path.dirname(os.path.abspath(args.dump)), exist_ok=True)
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump(tree, fh, ensure_ascii=False, indent=1)
        logger.info(f"已保存 {args.dump}")

    messages = parse_messages(tree)
    if args.json:
        print(json.dumps([m.as_dict() for m in messages], ensure_ascii=False, indent=2))
        return 0

    for m in messages:
        who = "我" if m.is_self else ("他" if m.is_self is False else "?")
        print(f"{m.msg_id:<20} {m.kind:<8} {who} {(m.timestamp or '-'):<12} {m.sender[:14]:<14} {m.content[:44]!r}")
    return 0


def cmd_serve(args):
    import faulthandler
    import threading

    # libatspi 的段错误/abort 没有 Python traceback，进程无声消失（QQ 自我
    # 重启后出现过）。faulthandler 在致命信号时把所有线程的栈写进 stderr
    # （即 qqcache.log），下次能看到死在哪
    faulthandler.enable(all_threads=True)

    from .logbuf import LogBuffer
    from .notify import Service, Store
    from . import core
    from .serve import Bridge, heartbeat_loop, poll_loop

    logbuf = LogBuffer()
    logger.add(
        logbuf.sink,
        level="DEBUG" if os.environ.get("QQCACHE_DEBUG") else "INFO",
    )

    def urls(cli, env):
        # 参数与环境变量合并；同一地址只注册一次
        items = list(cli) + os.environ.get(env, "").split(",")
        return list(dict.fromkeys(u.strip() for u in items if u.strip()))

    post_urls = urls(args.post_url, "QQCACHE_POST_URL")
    ws_reverse_urls = urls(args.ws_reverse_url, "QQCACHE_WS_REVERSE_URL")

    bridge = Bridge(
        args.db,
        self_id=args.self_id,
        self_name=args.self_name,
        post_urls=post_urls,
        token=args.access_token,
        secret=args.post_secret,
        verify_identity=not args.no_identity_check,
        log_buffer=logbuf,
        test_trigger=args.test_trigger,
    )
    if bridge.test_trigger:
        logger.warning("测试触发器已启用：收到 #ww 会回复服务状态")

    # Startup transports go through the manager too — they show up in
    # list_transports (marked source=startup, not removable via API), and
    # runtime-added ones get persisted/restored alongside them.
    try:
        bridge.transports.add_http(args.host, args.port, source="startup")
    except Exception as exc:
        logger.error(f"HTTP 监听启动失败: {exc}")
        return 1
    logger.info(f"OneBot v11 HTTP API + 正向 WS: http://{args.host}:{args.port}/  ws://{args.host}:{args.port}/")
    for url in post_urls:
        bridge.transports.add_post_url(url, source="startup")
        logger.info(f"事件上报: {url}")

    stop = threading.Event()
    for url in ws_reverse_urls:
        bridge.transports.add_ws_reverse(url, token=args.access_token, source="startup")
        logger.info(f"反向 WS: {url}")
    bridge.transports.restore()
    bridge.protocol_mgr.start_enabled()

    # QQ 启动即自动获焦，持焦期间别的会话的通知静默——serve 起来先还焦点
    threading.Timer(5.0, bridge._defocus_when_idle).start()

    from .serve import gui_watchdog

    threading.Thread(target=gui_watchdog, args=(bridge, stop), daemon=True).start()

    if args.poll_interval > 0:
        threading.Thread(
            target=poll_loop, args=(bridge, args.poll_interval, stop), daemon=True
        ).start()
        logger.info(f"打开会话轮询: 每 {args.poll_interval}s")

    # 心跳总是开：反向 WS/上报多在运行时添加，正向 WS 客户端（WebUI BFF）
    # 也依赖心跳和 login_state 事件
    if args.heartbeat > 0:
        threading.Thread(
            target=heartbeat_loop, args=(bridge, args.heartbeat, stop), daemon=True
        ).start()
    bridge.publish(core.Connected(interval=args.heartbeat))

    store = Store(args.db)
    bridge.notified_names = store.notified_summaries

    def resolver(name):
        uin = bridge.cache.get(name)
        if not uin:
            return None
        row = bridge.cache.lookup(uin)
        return row["kind"] if row else None

    try:
        service = Service(
            store,
            verbose=not args.quiet,
            resolver=resolver,
            attributor=bridge.on_notification,
            on_event=bridge.on_event,
            invoke_all=True,
        )
        bridge.invoke_notification = service.invoke_live
        service.run()
    finally:
        stop.set()
        bridge.cache.close()
        bridge.cursor.close()
        bridge.ids.close()
        store.close()
    return 0


def cmd_selftest(args):
    import json

    from .selftest import run
    from .serve import Bridge

    bridge = Bridge(args.db)
    try:
        report = run(bridge, args.conv, open_timeout=args.timeout)
    finally:
        bridge.cache.close()
        bridge.cursor.close()
        bridge.ids.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for s in report["stages"]:
            mark = "OK" if s["ok"] else "FAIL"
            print(f"[{mark:<4}] {s['name']:<12} {s['ms']:>6}ms  {s['detail']}")
        print("通过" if report["ok"] else f"断在 {report['failed_stage']}")
    return 0 if report["ok"] else 1


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB)
    common.add_argument("--pid", type=int)

    parser = argparse.ArgumentParser(prog="qqcache")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", parents=[common], help="补齐缓存里缺失或过期的会话")
    p.add_argument("--ttl", type=float, help="多少天后视为过期（默认永不过期）")
    p.add_argument("--force", action="store_true", help="全部重抓")
    p.add_argument("--limit", type=int, help="最多抓几个")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sync)

    sub.add_parser("list", parents=[common], help="列出缓存").set_defaults(func=cmd_list)
    sub.add_parser("stats", parents=[common], help="缓存统计").set_defaults(func=cmd_stats)
    sub.add_parser(
        "conversations", parents=[common], help="列出 QQ 当前会话列表并标注缓存状态"
    ).set_defaults(func=cmd_conversations)

    p = sub.add_parser("messages", parents=[common], help="解析当前会话的消息列表")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dump", help="把无障碍树保存成 fixture JSON")
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("poll", parents=[common], help="增量读取当前会话并关联媒体")
    p.add_argument("--json", action="store_true")
    p.add_argument("--bootstrap", choices=["skip", "all"], default="skip")
    p.add_argument("--no-resolve", action="store_true", help="会话 uin 未知时不要打开资料卡解析")
    p.add_argument("--no-backfill", action="store_true", help="检测到 gap 时不要上滚回填")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("send", parents=[common], help="切换会话并发送一条消息")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--name", help="会话名（会话列表里显示的那个）")
    target.add_argument("--uin", help="QQ号或群号，直接按号码搜索定位")
    p.add_argument("--text", default="", help="消息文本；{at:UIN} 会被替换成真实 @ 芯片")
    p.add_argument("--at", action="append", default=[], metavar="UIN", help="@群成员（可加多个，追加在文本后）")
    p.add_argument("--image", action="append", default=[], metavar="PATH", help="图片文件（可加多个，追加在最后）")
    p.add_argument("--reply-to", metavar="MSG_ID", help="引用回复指定消息")
    p.add_argument("--dry-run", action="store_true", help="只组装不发送，然后清空输入框")
    p.add_argument("--no-verify", action="store_true", help="跳过发送后的回显确认")
    p.add_argument("--no-identity-check", action="store_true", help="跳过资料卡身份校验（--uin 时）")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("selftest", parents=[common],
                       help="分阶段跑一遍入站链路，报告断在哪一级")
    p.add_argument("--conv", required=True, help="目标会话名")
    p.add_argument("--timeout", type=float, default=8.0, help="打开会话的超时秒数")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("notify", parents=[common], help="接管 org.freedesktop.Notifications")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--monitor", action="store_true", help="被动监听，不抢占总线名")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("events", parents=[common], help="查看已记录的通知")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--since", type=int, help="只看 id 大于该值的")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("resolve", parents=[common], help="按名字查 uin")
    p.add_argument("name")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("serve", parents=[common], help="OneBot v11 服务（HTTP API + 事件 POST）")
    p.add_argument("--host", default=os.environ.get("QQCACHE_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("QQCACHE_PORT", "5700")))
    p.add_argument(
        "--post-url", action="append", default=[],
        help="事件上报地址，可重复；也可用 QQCACHE_POST_URL（逗号分隔）",
    )
    p.add_argument(
        "--ws-reverse-url", action="append", default=[],
        help="反向 WS 地址（连到框架的 universal 端点），可重复；"
             "也可用 QQCACHE_WS_REVERSE_URL（逗号分隔）",
    )
    p.add_argument("--access-token", default=os.environ.get("QQCACHE_ACCESS_TOKEN"),
                   help="API 鉴权 token（查询参数或 Bearer 头）")
    p.add_argument("--post-secret", default=os.environ.get("QQCACHE_POST_SECRET"),
                   help="事件上报的 HMAC-SHA1 签名密钥")
    p.add_argument("--self-id", default=os.environ.get("QQCACHE_SELF_ID", "0"),
                   help="bot 自己的 QQ 号（事件里的 self_id）")
    p.add_argument("--self-name", default=os.environ.get("QQCACHE_SELF_NAME", ""))
    p.add_argument("--heartbeat", type=float, default=30.0,
                   help="心跳 meta_event 间隔秒数，0 关闭")
    p.add_argument("--poll-interval", type=float,
                   default=float(os.environ.get("QQCACHE_POLL_INTERVAL", "2")),
                   help="当前打开会话的轮询间隔秒数（QQ 对开着的会话不发通知），0 关闭")
    p.add_argument("--no-identity-check", action="store_true",
                   help="出站跳过资料卡身份校验（更快但失去防误发保护）")
    p.add_argument("--test-trigger", action="store_true",
                   default=os.environ.get("QQCACHE_TEST_TRIGGER", "").lower()
                   in ("1", "true", "yes", "on"),
                   help="测试开关：收到 #ww 消息时回复服务状态（默认关闭）")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv=None):
    logger.remove()
    logger.add(
        sys.stderr,
        format="{time:HH:mm:ss} {level: <7} {message}",
        level="DEBUG" if os.environ.get("QQCACHE_DEBUG") else "INFO",
    )
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
