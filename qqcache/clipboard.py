"""Clipboard access that works on both Wayland and X11 sessions."""

import contextlib
import os
import shutil
import subprocess


def _run(cmd, data=None, timeout=3):
    return subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)


def _write(cmd, data=None, timeout=3):
    """Run a clipboard *writer*.

    Writers (xclip/xsel/wl-copy) fork and stay alive to serve the selection;
    with captured pipes the grandchild keeps them open and communicate() would
    hang forever. DEVNULL everything and wait for the parent to exit instead.
    """
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return False
    return process.returncode == 0


def backend():
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return "wl"
    if shutil.which("xclip"):
        return "xclip"
    if shutil.which("xsel"):
        return "xsel"
    return None


def read_text():
    kind = backend()
    try:
        if kind == "wl":
            return _run(["wl-paste", "--no-newline"]).stdout.decode("utf-8", "replace")
        if kind == "xclip":
            return _run(["xclip", "-o", "-selection", "clipboard"]).stdout.decode("utf-8", "replace")
        if kind == "xsel":
            return _run(["xsel", "-b", "-o"]).stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def write_text(text):
    kind = backend()
    data = text.encode("utf-8")
    try:
        if kind == "wl":
            return _write(["wl-copy"], data)
        if kind == "xclip":
            return _write(["xclip", "-i", "-selection", "clipboard"], data)
        if kind == "xsel":
            return _write(["xsel", "-b", "-i"], data)
    except (subprocess.SubprocessError, OSError):
        return False
    return False


def write_image(path):
    """Put an image file's bytes on the clipboard as ``image/png``.

    xsel has no mime-type selection, so it is not supported here.
    """
    kind = backend()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if kind == "wl":
            return _write(["wl-copy", "-t", "image/png"], data)
        if kind == "xclip":
            return _write(["xclip", "-i", "-selection", "clipboard", "-t", "image/png"], data)
    except (subprocess.SubprocessError, OSError):
        return False
    return False


@contextlib.contextmanager
def preserved():
    """Restore the previous clipboard text when the block exits.

    Only text is restored; image payloads cannot be round-tripped here.
    """
    saved = read_text()
    try:
        yield
    finally:
        if saved:
            write_text(saved)
