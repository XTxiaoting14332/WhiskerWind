"""Login state detection and QR/screenshot capture.

The container fixes the display at 1920x1080, so a screenshot is just
``scrot``. The QR region is located through AT-SPI extents rather than a
hard-coded rect — the login window's layout may shift between QQ versions.

self_id comes free from the filesystem: QQ creates a per-account
``Partitions/qqnt_<uin>`` directory on login (system partitions use 4-digit
names, real uins are 5+ digits).
"""

import json
import os
import re
import subprocess
import tempfile

from . import atspi

PARTITION_RE = re.compile(r"^qqnt_(\d{5,12})$")
LOGIN_HINTS = ("二维码", "扫码", "登录", "密码登录", "扫一扫")


def detect_self_uin(qq_config=None):
    """uin of the logged-in account from the partition dir, or None."""
    root = os.path.join(qq_config or os.path.expanduser("~/.config/QQ"), "Partitions")
    try:
        names = os.listdir(root)
    except OSError:
        return None
    uins = [m.group(1) for n in names if (m := PARTITION_RE.match(n))]
    return uins[0] if uins else None


def detect_self_profile(qq_config=None):
    """{uin, nickname, face_url} of the most recent account, or {}.

    auth/login.enc 名字带 enc 但内容就是明文 JSON——登录窗的账号列表
    靠它渲染，uin/nickName/faceUrl 都是明文。分区目录 uin 优先，其次
    顶层 account 字段，最后兜底第一条。
    """
    path = os.path.join(qq_config or os.path.expanduser("~/.config/QQ"),
                        "auth", "login.enc")
    try:
        with open(path, "rb") as fh:
            obj = json.loads(fh.read())
    except (OSError, ValueError):
        return {}
    accounts = obj.get("loginList") or []
    if not accounts:
        return {}
    part_uin = detect_self_uin(qq_config)
    pick = next(
        (a for a in accounts if str(a.get("uin")) == str(part_uin)), None)
    if pick is None:
        want = str(obj.get("account") or "")
        pick = next(
            (a for a in accounts if str(a.get("uin")) == want), accounts[0])
    return {
        "uin": str(pick.get("uin") or ""),
        "nickname": pick.get("nickName") or "",
        "face_url": pick.get("faceUrl") or "",
    }


def _login_frame(app):
    """(node, extents) of the compact login/registration window, or None.

    The login window's widgets never project into AT-SPI — the tree stays
    bare ``application > frame > panel`` — so geometry is the only tell:
    it is always a small centered window while the main UI fills the
    display (~1920x1080). Only reached when main-UI markers are absent,
    so a small frame here always means a login/registration page.
    """
    if app is None:
        return None
    best = None
    for n in atspi.walk(atspi.ui_root(app)):
        if atspi.node_role(n) not in ("frame", "dialog", "window"):
            continue
        ext = atspi.extents(n)
        if ext is None or ext[2] < 300 or ext[3] < 300:
            continue
        if ext[2] <= 1300 and ext[3] <= 800:
            if best is None or ext[2] * ext[3] < best[1][2] * best[1][3]:
                best = (n, ext)
    return best


def login_state(app):
    """'logged_in' | 'login_required' | 'unknown' | 'not_running'."""
    if app is None:
        return "not_running"
    root = atspi.ui_root(app)
    if (
        atspi.find_one(root, role="embedded", name="会话列表") is not None
        or atspi.find_by_class(root, "ProseMirror") is not None
        or atspi.find_by_class(root, "ml-root") is not None
        # 左侧导航栏只在登录后的主界面存在；联系人/群通知页没有会话列表，
        # 只认会话列表会把它们误判成登录页（WebUI 跳「待扫码」）
        or atspi.find_one(root, role="toggle button", name="消息") is not None
    ):
        return "logged_in"
    for n in atspi.walk(root):
        if atspi.node_role(n) == "static" and any(
            h in (atspi.node_name(n) or "") for h in LOGIN_HINTS
        ):
            return "login_required"
    # 登录页裸树兜底：控件不投影时文本探针全空，几何上认小窗
    if _login_frame(app) is not None:
        return "login_required"
    return "unknown"


# 点「确定」不会有副作用的通知类弹窗：被挤下线、登录失败/失效。只认这些
# 标题（或 QQ 给被踢弹窗打的 kickoff-tips 标记），别的弹窗一概不碰——
# 「确定删除？」之类的确认框点了就是事故。
DISMISSABLE_TITLES = ("下线通知", "登录失败", "登录失效")


def notice_dialog(app):
    """{"title", "text", "button"} for a dismissable notice on screen, or None."""
    if app is None:
        return None
    root = atspi.ui_root(app)
    for box in atspi.find_all_by_class(root, "q-dialog-main"):
        title_node = atspi.find_by_class(box, "q-dialog-title")
        title = (atspi.text_of(title_node).strip() if title_node is not None else "")
        if title not in DISMISSABLE_TITLES and not atspi.has_class(box, "kickoff-tips"):
            continue
        button = None
        texts = []
        for n in atspi.walk(box):
            role = atspi.node_role(n)
            name = (atspi.node_name(n) or "").strip()
            if role == "button" and name == "确定":
                button = n
            elif role == "static" and name and name != title:
                texts.append(name)
        ext = atspi.extents(button) if button is not None else None
        # 登录窗的 a11y 树不随动画更新：对话框关掉后节点照样留着、照样标
        # showing/visible。按钮位置上真有 QQ 蓝的「确定」才算还开着
        if ext is None or not primary_button_on_screen(ext):
            continue
        return {"title": title or "下线通知", "text": "".join(texts),
                "button": button, "ext": ext}
    return None


# QQ 主按钮的蓝（实测 (0,153,255)）：对话框开着时按钮区 ~89% 是它，
# 关掉后同一位置是登录页背景（~100% 近白）
_PRIMARY_MIN_RATIO = 0.5


def primary_button_on_screen(ext):
    img = png_rgb(screenshot(ext))
    if img is None:
        return False
    w, h, rows = img
    blue = 0
    for row in rows:
        for k in range(0, len(row), 3):
            r, g, b = row[k], row[k + 1], row[k + 2]
            if b > 200 and r < 90 and 110 < g < 200:
                blue += 1
    return blue >= _PRIMARY_MIN_RATIO * (w * h or 1)


def dismiss_notice(app, timeout=2.0):
    """Click 确定 on a dismissable notice. Returns (title, text) once it is
    really gone from the screen, else None.

    Real mouse click: a11y ``press`` reports success on this button but QQ
    ignores it (seen live on 下线通知)."""
    import time

    d = notice_dialog(app)
    if d is None:
        return None
    x, y, w, h = d["ext"]
    subprocess.run(["xdotool", "mousemove", str(x + w // 2), str(y + h // 2), "click", "1"],
                   capture_output=True, timeout=3)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if not primary_button_on_screen(d["ext"]):
            return d["title"], d["text"]
    return None


def screenshot(rect=None, timeout=5):
    """PNG bytes of the display (or an x,y,w,h rect) via scrot."""
    cmd = ["scrot", "-z"]
    if rect:
        cmd += ["-a", "{},{},{},{}".format(*rect)]
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    # scrot refuses to overwrite and silently renames its output — the
    # target must NOT exist before it runs.
    os.unlink(path)
    cmd.append(path)
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"))
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
        with open(path, "rb") as fh:
            data = fh.read()
        return data if data.startswith(b"\x89PNG") else None
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def qr_region(app):
    """(x, y, w, h) of the login QR image, or None.

    Primary anchor is the ``qr-code-img`` / ``qr-code`` class pair QQ marks
    the element with; it survives every login-page layout seen so far
    (avatar page, 账密+QR side-by-side, QR-only fresh install). Fallback for
    unknown layouts: largest roughly-square image node — so nothing is
    hard-coded to a position. Returns None when logged in (no QR in tree).
    """
    if app is None:
        return None
    root = atspi.ui_root(app)
    for cls in ("qr-code-img", "qr-code"):
        node = atspi.find_by_class(root, cls)
        if node is not None:
            ext = atspi.extents(node)
            if ext and ext[2] >= 80 and ext[3] >= 80:
                return ext
    best = None
    for n in atspi.walk(root):
        if atspi.node_role(n) not in ("image", "icon"):
            continue
        name = (atspi.node_name(n) or "").lower()
        ext = atspi.extents(n)
        if ext is None:
            continue
        x, y, w, h = ext
        if w < 80 or h < 80:
            continue
        square = abs(w - h) <= max(20, w * 0.2)
        hinted = any(t in name for t in ("二维码", "qr", "code", "码"))
        if hinted or square:
            if best is None or w * h > best[2] * best[3]:
                best = ext
    return best


def _frame_qr_guess(app):
    """The QR block's expected rect on the side-by-side login layout.

    Bare-tree fallback only: qr-code nodes are absent and the position is
    derived from the login frame's geometry. The 账密+QR layout keeps the
    QR in the right ~40% of the window, vertically centred under the
    扫码登录 caption. ``qr_png``'s entropy gate still decides whether the
    crop really contains a code (avatar/select pages return nothing).
    """
    frame = _login_frame(app)
    if frame is None:
        return None
    _, (fx, fy, fw, fh) = frame
    x = fx + int(fw * 0.62)
    y = fy + int(fh * 0.16)
    w = int(fw * 0.36)
    return (x, y, w, int(w * 0.9))


def qr_expired(app):
    """True when the visible QR carries QQ's 已过期 overlay/refresh button.

    The login window only sometimes projects its widgets into AT-SPI; when it
    does, the ``expired-btn``/``expired-label`` classes decide. When the tree
    is bare, look at the pixels instead (see qr_pixels_expired).
    """
    if app is None:
        return False
    root = atspi.ui_root(app)
    for n in atspi.walk(root):
        cls = atspi.attr(n, "class").split()
        if "expired-btn" in cls or "expired-label" in cls:
            return True
    rect = qr_region(app) or _frame_qr_guess(app)
    if rect is None:
        return False
    return qr_pixels_expired(screenshot(rect))


# 实测（3.2.33 登录页）：有效码的深灰方块占 ~5–11%，过期遮罩下方块淡到
# ~0.4%，叠一行红字「二维码已过期」（~0.3–0.75%）；有效码的红色只来自
# 中间企鹅围巾。加载中的白图没有红字——要求有红，免得把转圈当过期。
_EXPIRED_MAX_DARK = 0.015
_EXPIRED_MIN_RED = 0.001


def qr_pixels_expired(png):
    stats = qr_pixel_stats(png)
    if stats is None:
        return False
    dark, red = stats
    return dark < _EXPIRED_MAX_DARK and red > _EXPIRED_MIN_RED


def _press_expired(app, timeout=4.0):
    """Click QQ's own refresh button and wait for the overlay to go."""
    import time

    btn = atspi.find_by_class(atspi.ui_root(app), "expired-btn")
    if btn is None:
        return not qr_expired(app)
    atspi.invoke(btn, "press")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not qr_expired(app):
            return True
        time.sleep(0.2)
    return False


def prepare(app, timeout=4.0):
    """Drive the login window to the QR page.

    After a session expires QQ shows a 登录失败 dialog on top of the avatar
    page; the QR lives behind 其他登录方式. Returns True once a QR region is
    visible. No-op (True) if the QR is already there.
    """
    import time

    if qr_region(app) is not None:
        # QR 在但可能已过期——点 QQ 自己的「刷新」让它重生
        return _press_expired(app) if qr_expired(app) else True
    root = atspi.ui_root(app)
    # 下线通知/登录失败：a11y press 对它无效，走真点击 + 像素确认
    dismiss_notice(app)
    # 过期弹窗：先收掉「登录失败」之类的模态框
    for n in atspi.find_all(root, role="button"):
        if atspi.node_name(n) in ("确定", "好的", "知道了"):
            atspi.invoke(n, "press")
            time.sleep(0.5)
            break
    # 头像页 -> 其他登录方式 -> 账号密码+二维码页
    btn = atspi.find_one(root, role="button", name="其他登录方式")
    if btn is not None:
        atspi.invoke(btn, "press")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if qr_region(app) is not None:
                break
            time.sleep(0.2)
    if qr_region(app) is None:
        # 裸树兜底：按钮节点不存在，a11y 驱动的两步全落空。按布局几何
        # 点 QR 区中心一次——码过期时该处正是「刷新」按钮（重生新码），
        # 码有效时点图无害；头像页上此处是空白同样无害。裁图熵门兜底：
        # 没有真码的页面 qr_png 自己会超时返回 None。
        guess = _frame_qr_guess(app)
        if guess is None:
            return False
        gx, gy, gw, gh = guess
        subprocess.run(
            ["xdotool", "mousemove", str(gx + gw // 2), str(gy + gh // 2),
             "click", "1"],
            capture_output=True, timeout=3,
        )
        time.sleep(0.6)
        return True
    return _press_expired(app) if qr_expired(app) else True


def png_rgb(data):
    """Decode an 8-bit non-interlaced RGB/RGBA PNG (what scrot writes) into
    (width, height, rows) where rows[y] is a bytes of RGB triples. Stdlib
    only — the container has no imaging library. None for anything else."""
    import struct
    import zlib

    if not data or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, idat, w = 8, [], None
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            w, h, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color not in (2, 6) or interlace:
                return None
            bpp = 3 if color == 2 else 4
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if w is None:
        return None
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(h):
        ftype, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a_ = line[x - bpp] if x >= bpp else 0
            b_ = prev[x]
            c_ = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a_) & 255
            elif ftype == 2:
                line[x] = (line[x] + b_) & 255
            elif ftype == 3:
                line[x] = (line[x] + ((a_ + b_) >> 1)) & 255
            elif ftype == 4:
                p_ = a_ + b_ - c_
                pa, pb, pc = abs(p_ - a_), abs(p_ - b_), abs(p_ - c_)
                pred = a_ if pa <= pb and pa <= pc else (b_ if pb <= pc else c_)
                line[x] = (line[x] + pred) & 255
        prev = line
        rows.append(bytes(line) if bpp == 3 else bytes(
            v for k in range(0, stride, 4) for v in line[k:k + 3]))
    return w, h, rows


def qr_pixel_stats(data):
    """(dark_ratio, red_ratio) of a QR crop: share of dark pixels (QR
    modules) and of saturated red ones (the 二维码已过期 caption)."""
    img = png_rgb(data)
    if img is None:
        return None
    w, h, rows = img
    dark = red = 0
    for row in rows:
        for k in range(0, len(row), 3):
            r, g, b = row[k], row[k + 1], row[k + 2]
            if r + g + b < 288:   # QR 方块是深灰（不是纯黑）
                dark += 1
            elif r > 180 and g < 130 and b < 130 and r - g > 70:
                red += 1
    n = w * h or 1
    return dark / n, red / n


# 真二维码是高频黑白噪点，PNG 压不动（实测 ≥5.5KB）；刷新加载态的
# 转圈/白底高度可压缩（通常 <1.5KB）。尺寸阈值用于区分"节点在但图没出来"。
_QR_MIN_BYTES = 2048


def qr_png(app, timeout=6.0):
    """Screenshot cropped to the QR region; None when not on the login page.

    prepare() 点刷新后 QR 节点经历加载态——节点已就位但画面还是转圈，
    单次探测/截图会裁到白图。等到画面看起来像真码（熵够大）或超时返回 None，
    让上层返回可重试的错误而不是把转圈图当码发出去。
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        region = qr_region(app) or _frame_qr_guess(app)
        if region is not None:
            x, y, w, h = region
            pad = 12
            data = screenshot((x - pad, y - pad, w + 2 * pad, h + 2 * pad))
            if data is not None and len(data) >= _QR_MIN_BYTES:
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.3)
