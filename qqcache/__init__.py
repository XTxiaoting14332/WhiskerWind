"""GUI-driven QQ client toolkit: name->uin cache, message reader, media correlator."""

__version__ = "0.1.0"

from .cursor import Cursor
from .media import MediaIndex, correlate, find_nt_data
from .messages import Message, parse_messages
from .store import Cache, display_name, is_uin, sanitize
from .sync import ScrapeError, copy_uin, list_conversations, sync

__all__ = [
    "Cache",
    "Cursor",
    "MediaIndex",
    "Message",
    "ScrapeError",
    "correlate",
    "copy_uin",
    "display_name",
    "find_nt_data",
    "is_uin",
    "list_conversations",
    "parse_messages",
    "sanitize",
    "sync",
]
