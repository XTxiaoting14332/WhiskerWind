"""Parse QQ's message list into structured records.

Everything here works on the plain-dict tree produced by ``atspi.dump_tree`` so
the rules can be tested offline against saved fixtures instead of a live client.

Chromium leaks the DOM classes and ids through AT-SPI object attributes, and
those are a far more stable anchor than the accessibility tree's shape:

    div.ml-item[id=<msgId>]
      div.message__timestamp                        optional time divider
      div.message-container--self|--others          direction
        span.avatar-span[name=<sender>]             sender
        div.group-member-label                      title badge (LV/群主)
        div.msg-content-container.container--self   direction (authoritative)
          div.message-content                       body
        div.file-message--content                   files live here instead
      div.gray-tip-message                          recalls and other system tips

The msgId is an identity, not an ordering key: messages sent from this client
use a 777-prefixed id space while synced ones use 770, so ids are not globally
monotonic. Order always comes from the DOM.
"""

from dataclasses import dataclass, field

MODIFIER_KINDS = {
    "pic": "image",
    "image": "image",
    "file": "file",
    "audio": "voice",
    "ptt": "voice",
    "video": "video",
    "ark": "card",
}

# 名字里带这些 class 的子树不属于正文
NON_CONTENT = ("reply-element", "md-tips", "message-extra-top", "file-status")


@dataclass
class Message:
    msg_id: str
    kind: str
    sender: str
    title: str | None
    content: str
    timestamp: str | None
    is_self: bool | None
    quoted_sender: str | None = None
    quoted_content: str | None = None
    file_name: str | None = None
    file_size: str | None = None
    classes: list[str] = field(default_factory=list)

    def as_dict(self):
        return {
            "msg_id": self.msg_id,
            "kind": self.kind,
            "sender": self.sender,
            "title": self.title,
            "content": self.content,
            "timestamp": self.timestamp,
            "is_self": self.is_self,
            "quoted_sender": self.quoted_sender,
            "quoted_content": self.quoted_content,
            "file_name": self.file_name,
            "file_size": self.file_size,
        }


def _classes(node):
    return (node.get("attrs") or {}).get("class", "").split()


def _has(node, token):
    return token in _classes(node)


def _find(node, token, max_depth=30):
    for n, depth in _walk(node):
        if depth > max_depth:
            break
        if _has(n, token):
            return n
    return None


def _find_all(node, token):
    return [n for n, _ in _walk(node) if _has(n, token)]


def _walk(node, depth=0):
    if node is None:
        return
    yield node, depth
    if depth > 30:
        return
    for child in node.get("children") or []:
        yield from _walk(child, depth + 1)


def _texts(node, skip=()):
    """Static descendants in document order.

    QQ splits one message's text across several nodes (an IP, a link or an
    @-mention each get their own wrapper), so a shallow read loses content.
    Nodes matching a skip token are pruned together with their whole subtree.
    """
    if node is None:
        return []
    parts = []

    def rec(n, depth):
        if any(_has(n, token) for token in skip):
            return
        if n.get("role") == "static":
            parts.append(n.get("name") or "")
            return
        if depth > 30:
            return
        for child in n.get("children") or []:
            rec(child, depth + 1)

    rec(node, 0)
    return parts


def _text(node, skip=()):
    return "".join(_texts(node, skip))


def _kind(item):
    if _find(item, "gray-tip-message") is not None:
        return "system"
    if _find(item, "forward-msg") is not None:
        return "forward"
    if _find(item, "file-element") is not None:
        return "file"
    container = _find(item, "msg-content-container")
    if container is None:
        return "unknown"
    if _has(container, "ptt-message__container") or _has(container, "ptt-message"):
        return "voice"
    if _has(container, "reply-message__container"):
        return "reply"
    for token in _classes(container):
        if token.startswith("mix-message__container--"):
            suffix = token.split("--", 1)[1]
            return MODIFIER_KINDS.get(suffix, suffix)
    return "text"


def _direction(item):
    """True when the message was sent by us.

    ``msg-content-container`` carries the authoritative marker; file messages
    have no content container, so fall back to ``message-container``.
    """
    for token in ("msg-content-container", "message-container"):
        node = _find(item, token)
        if node is None:
            continue
        classes = _classes(node)
        if "container--others" in classes or "message-container--others" in classes:
            return False
        if "container--self" in classes or "message-container--self" in classes:
            return True
    return None


def _file_info(item):
    element = _find(item, "file-element")
    if element is None:
        return None, None
    paragraphs = [n for n, _ in _walk(element) if n.get("role") == "paragraph"]
    name = _text(_find(element, "file-name")) or (_text(paragraphs[0]) if paragraphs else "")
    extras = [_text(p) for p in paragraphs[1:]]
    extras += [
        c.get("name") or ""
        for c in element.get("children") or []
        if c.get("role") == "static"
    ]
    size = " ".join(part for part in extras if part.strip())
    return name or None, size or None


def parse_item(item):
    msg_id = (item.get("attrs") or {}).get("id", "")
    if not _has(item, "ml-item") or not msg_id:
        return None

    kind = _kind(item)
    quoted_sender = quoted_content = file_name = file_size = None

    if kind == "forward":
        title = _text(_find(item, "fwd-title"))
        lines = [_text(n) for n in _find_all(item, "fwd-content")]
        content = "\n".join([title] + [line for line in lines if line])
    elif kind == "file":
        file_name, file_size = _file_info(item)
        content = file_name or ""
    elif kind == "system":
        content = _text(_find(item, "gray-tip-content"), skip=("gray-tip-action",))
    elif kind == "reply":
        reply = _find(item, "reply-element")
        if reply is not None:
            texts = [n.get("name") or "" for n, _ in _walk(reply) if n.get("role") == "static"]
            quoted_sender = texts[0] if texts else None
            quoted_content = "".join(texts[1:]) or None
        content = _text(_find(item, "message-content"), skip=("reply-element",))
    elif kind == "voice":
        content = _text(_find(item, "message-content")) or _text(_find(item, "msg-content-container"))
    else:
        content = _text(_find(item, "message-content"), skip=NON_CONTENT)

    sender_node = _find(item, "avatar-span")
    sender = (sender_node.get("name") or "") if sender_node else ""

    return Message(
        msg_id=msg_id,
        kind=kind,
        sender=sender,
        title=" ".join(part for part in _texts(_find(item, "group-member-label")) if part) or None,
        content=content,
        timestamp=_text(_find(item, "message__timestamp")) or None,
        is_self=_direction(item),
        quoted_sender=quoted_sender,
        quoted_content=quoted_content,
        file_name=file_name,
        file_size=file_size,
        classes=_classes(item),
    )


def parse_messages(tree, inherit_timestamp=True):
    """Turn a dumped message-list subtree into an ordered list of Message.

    Only the messages currently rendered exist in the tree: QQ virtualises the
    list, so reaching further back needs scrolling (a later step).
    """
    if tree is None:
        return []

    list_node = _find(tree, "ml-list") or tree
    out = []
    last_ts = None
    for item in list_node.get("children") or []:
        message = parse_item(item)
        if message is None:
            continue
        if message.timestamp:
            last_ts = message.timestamp
        elif inherit_timestamp:
            message.timestamp = last_ts
        out.append(message)
    return out
