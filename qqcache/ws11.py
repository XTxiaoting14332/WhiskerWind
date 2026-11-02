"""Minimal WebSocket (RFC 6455) for the OneBot v11 transports.

stdlib-only: server-side accept for the forward endpoint and a client for
reverse connections. Text frames only; ping/pong/close handled; incoming
fragmentation reassembled. Clients mask, servers don't — per the spec.
"""

import base64
import hashlib
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WsError(RuntimeError):
    pass


def accept_key(client_key):
    digest = hashlib.sha1((client_key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def read_http_head(sock, limit=65536):
    """Read up to \\r\\n\\r\\n, returning (head_bytes, leftover)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise WsError("连接在握手前关闭")
        buf += chunk
        if len(buf) > limit:
            raise WsError("握手头部过大")
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def parse_request(head):
    """HTTP request head -> (method, path, {header: value})."""
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers


def server_handshake(sock, head=None):
    """Validate an incoming Upgrade request; returns (path, headers, leftover).

    Caller must send the 101 response afterwards (auth checks happen between).
    """
    if head is None:
        head, rest = read_http_head(sock)
    else:
        rest = b""
    method, path, headers = parse_request(head)
    if method != "GET" or "websocket" not in headers.get("upgrade", "").lower():
        raise WsError("不是 WebSocket Upgrade 请求")
    key = headers.get("sec-websocket-key")
    if not key:
        raise WsError("缺少 Sec-WebSocket-Key")
    return path, headers, key, rest


def accept_response(key):
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
        "\r\n"
    ).encode("latin-1")


def client_handshake(sock, path, host, headers=None):
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    head, rest = read_http_head(sock)
    status = head.split(b"\r\n", 1)[0]
    if b" 101" not in status:
        raise WsError(f"握手被拒绝: {status.decode('latin-1', 'replace')}")
    # 不强校验 Accept：反向代理有时会改头；握上了就用
    return rest


def _read_exact(sock, n, buffered):
    while len(buffered) < n:
        chunk = sock.recv(min(65536, n - len(buffered) + 4096))
        if not chunk:
            raise WsError("连接关闭")
        buffered += chunk
    return buffered[:n], buffered[n:]


def recv_frame(sock, buffered=b""):
    """Read one frame -> (fin, opcode, payload, leftover)."""
    hdr, buffered = _read_exact(sock, 2, buffered)
    fin = bool(hdr[0] & 0x80)
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        ext, buffered = _read_exact(sock, 2, buffered)
        (length,) = struct.unpack(">H", ext)
    elif length == 127:
        ext, buffered = _read_exact(sock, 8, buffered)
        (length,) = struct.unpack(">Q", ext)
    mask = b""
    if masked:
        mask, buffered = _read_exact(sock, 4, buffered)
    payload, buffered = _read_exact(sock, length, buffered)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, buffered


def send_frame(sock, payload, opcode=OP_TEXT, mask=False):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    head = bytearray([0x80 | opcode])
    n = len(payload)
    mask_bit = 0x80 if mask else 0
    if n < 126:
        head.append(mask_bit | n)
    elif n < 65536:
        head.append(mask_bit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mask_bit | 127)
        head += struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(head) + payload)


class WsConn:
    """A live ws connection (either end). ``mask_out`` True for clients."""

    def __init__(self, sock, mask_out=False, buffered=b""):
        import threading

        self.sock = sock
        self.mask_out = mask_out
        self.buffered = buffered
        self.alive = True
        self._send_lock = threading.Lock()

    def send_text(self, text):
        with self._send_lock:
            send_frame(self.sock, text, OP_TEXT, mask=self.mask_out)

    def ping(self, payload=b""):
        with self._send_lock:
            send_frame(self.sock, payload, OP_PING, mask=self.mask_out)

    def close(self):
        if self.alive:
            try:
                send_frame(self.sock, b"", OP_CLOSE, mask=self.mask_out)
            except OSError:
                pass
            self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass

    def messages(self):
        """Yield complete text messages until the peer closes/errors."""
        parts = []
        while self.alive:
            try:
                fin, opcode, payload, self.buffered = recv_frame(self.sock, self.buffered)
            except (WsError, OSError):
                break
            if opcode == OP_CLOSE:
                try:
                    send_frame(self.sock, b"", OP_CLOSE, mask=self.mask_out)
                except OSError:
                    pass
                break
            if opcode == OP_PING:
                try:
                    send_frame(self.sock, payload, OP_PONG, mask=self.mask_out)
                except OSError:
                    break
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BIN, OP_CONT):
                parts.append(payload)
                if fin:
                    yield b"".join(parts).decode("utf-8", "replace")
                    parts = []
        self.alive = False


def connect(url, headers=None, timeout=10):
    """Client-side ws:// connect -> WsConn (ws:// only; no TLS)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("ws", ""):
        raise WsError(f"只支持 ws:// : {url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    sock = socket.create_connection((host, port), timeout=timeout)
    rest = client_handshake(sock, path, f"{host}:{port}", headers)
    sock.settimeout(None)
    return WsConn(sock, mask_out=True, buffered=rest)
