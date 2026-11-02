"""OneBot v11 adapter: core events -> OneBot v11 event dicts.

Rendering is a pure function (``render``) so the wire format can be tested
without a bridge; the adapter just pushes the dicts into the bridge's existing
OneBot sink (HTTP POST urls + forward/reverse ws, see Bridge.emit).
"""

from . import core
from .ob11 import build_message_event, meta_event


def render(event, self_id, ids=None):
    """List of OneBot v11 event dicts for one core event ([] = nothing)."""
    if isinstance(event, core.MessageCreated):
        ev = build_message_event(
            event.msg, event.conv_uin, event.kind, self_id, ids, event.cq,
            event.ob_id, user_id=event.sender_uin, now=event.time,
        )
        return [ev]
    if isinstance(event, core.MemberResolved):
        return [{
            "time": int(event.time),
            "self_id": int(self_id or 0),
            "post_type": "notice",
            "notice_type": "qqcache_member_resolved",
            "group_id": int(event.group_uin),
            "user_id": int(event.user_id),
            "was_user_id": int(event.was_user_id),
            "nickname": event.name,
        }]
    if isinstance(event, core.SystemNotification):
        # 扩展通知：QQ 系统通知原文（下线通知、加群申请…），只读
        return [{
            "time": int(event.time),
            "self_id": int(self_id or 0),
            "post_type": "notice",
            "notice_type": "qqcache_system_notification",
            "title": event.title,
            "text": event.text,
        }]
    if isinstance(event, core.Heartbeat):
        return [meta_event(
            self_id, "heartbeat",
            interval=int(event.interval * 1000),
            status={
                "online": event.state == "logged_in",
                "good": event.state != "not_running",
                "login_state": event.state,
            },
        )]
    if isinstance(event, core.LoginChanged):
        return [meta_event(self_id, "login_state", prev_state=event.prev, state=event.state)]
    if isinstance(event, core.Connected):
        return [meta_event(self_id, "lifecycle", sub_type="connect",
                           interval=int(event.interval * 1000))]
    return []


class OneBotAdapter:
    name = "onebot11"

    def __init__(self, bridge):
        self.bridge = bridge

    def handle(self, event):
        for ev in render(event, self.bridge.self_id, self.bridge.ids):
            self.bridge.emit(ev)
