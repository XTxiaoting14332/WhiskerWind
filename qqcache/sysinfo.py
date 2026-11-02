"""Host system overview for the WebUI dashboard (NapCat-style).

Everything reads /proc + statvfs inside the container — procfs is not
cgroup-scoped, so meminfo/stat report the HOST's global numbers, which is
what the dashboard wants.
"""

import json
import os
import time


def _cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(v) for v in parts[:8]]
    idle = vals[3] + vals[4]  # idle + iowait
    return sum(vals), idle


def host_cpu_percent(sample_s=0.15):
    """Host CPU busy percent over a short sampling window."""
    t1, i1 = _cpu_times()
    time.sleep(sample_s)
    t2, i2 = _cpu_times()
    dt, di = t2 - t1, i2 - i1
    return round(100.0 * (1 - di / dt), 1) if dt > 0 else 0.0


def host_mem():
    total = avail = 0
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
    used = total - avail
    return {
        "total": total,
        "used": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def host_disk(path="/"):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bavail) * st.f_frsize
    return {
        "path": path,
        "total": total,
        "used": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def qq_version():
    """QQ client version from its Electron package.json; '' when absent."""
    for p in ("/opt/QQ/resources/app/package.json",
              "/opt/QQ/resources/package.json"):
        try:
            with open(p) as f:
                return json.load(f).get("version", "")
        except Exception:
            continue
    return ""


def host_cpu_model():
    """Host CPU model name from /proc/cpuinfo."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if any(line.startswith(k) for k in ("model name", "Hardware", "Processor")):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    import platform
    return platform.processor() or "x86_64 Processor"


def host_cpu_cores():
    """Logical CPU core count."""
    return os.cpu_count() or 1


def qq_process_info():
    """QQ / Electron client processes overview (RSS memory, PID, process count)."""
    total_rss = 0
    main_pid = None
    proc_count = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    raw = fh.read()
                if not raw:
                    continue
                argv = raw.split(b"\0")
                exe = os.path.basename(argv[0].decode("utf-8", "replace")) if argv else ""
                if exe == "qq" or b"/opt/QQ/qq" in raw:
                    proc_count += 1
                    if main_pid is None and b"--type=" not in raw:
                        main_pid = pid
                    with open(f"/proc/{pid}/status") as sf:
                        for sline in sf:
                            if sline.startswith("VmRSS:"):
                                total_rss += int(sline.split()[1]) * 1024
                                break
            except Exception:
                continue
    except Exception:
        pass
    return {
        "pid": main_pid,
        "process_count": proc_count,
        "rss_bytes": total_rss,
    }


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def overview(bridge):
    """Dashboard payload: account + versions + host resources + service stats."""
    from . import __version__

    uin = bridge.self_id or 0
    stats = _safe(bridge.cache.stats, {})
    seen = _safe(
        lambda: bridge.cursor.db.execute(
            "SELECT COUNT(*) AS n FROM seen_messages").fetchone()["n"], 0)
    return {
        "self_id": uin,
        "self_name": bridge.self_name,
        "avatar_url": getattr(bridge, "self_face_url", "")
            or (f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640" if uin else ""),
        "login_state": _safe(lambda: bridge._login_state_cached(), "unknown"),
        "versions": {
            "qqcache": __version__,
            "onebot": "v11",
            "qq": qq_version(),
        },
        "system": {
            "platform": "linux-x64",
            "uptime_s": int(time.time() - bridge.started_at),
            "cpu_percent": host_cpu_percent(),
            "cpu_model": host_cpu_model(),
            "cpu_cores": host_cpu_cores(),
            "mem": host_mem(),
            "disk": host_disk("/"),
        },
        "qq_proc": qq_process_info(),
        "stats": {
            "conversations": stats.get("conversations", 0),
            "aliases": stats.get("aliases", 0),
            "seen_messages": seen,
            "transports": _safe(lambda: len(bridge.transports.list()), 0),
        },
    }

