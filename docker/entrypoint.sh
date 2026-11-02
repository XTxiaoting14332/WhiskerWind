#!/bin/bash
set -euo pipefail

SCREEN="${SCREEN:-1920x1080x24}"
VNC_PORT="${VNC_PORT:-5900}"
VNC_PASSWORD="${VNC_PASSWORD:-}"
VNC_HOST="${VNC_HOST:-127.0.0.1}"
STATE_DIR="${STATE_DIR:-/var/lib/qqcache}"

export DISPLAY=:99
export LANG="${LANG:-C.UTF-8}"
# pip 依赖装在持久卷里（可写层重建即丢）；repo 代码 bind-mount 直替 /opt/qqcache/qqcache
export PYTHONPATH="/opt/qqcache:${STATE_DIR}/py-deps"
export NO_AT_BRIDGE=0
export QT_ACCESSIBILITY=1

mkdir -p "$STATE_DIR"

# VNC 能直接操作 QQ 界面：监听回环以外的地址必须设密码
case "$VNC_HOST" in
    127.0.0.1|localhost|::1) vnc_local=1 ;;
    *) vnc_local=0 ;;
esac
if [ "$vnc_local" = 0 ] && [ -z "$VNC_PASSWORD" ]; then
    echo "WhiskerWind: VNC_HOST=$VNC_HOST 会把桌面暴露到网络上，请同时设置 VNC_PASSWORD" >&2
    exit 1
fi

# 设备指纹：machine-id 跨重启必须稳定，否则每次都是「新设备」
if [ ! -s "$STATE_DIR/machine-id" ]; then
    cat /proc/sys/kernel/random/uuid | tr -d '-' > "$STATE_DIR/machine-id"
fi
cp "$STATE_DIR/machine-id" /etc/machine-id

# 凭据：没通过环境变量指定时，首次启动随机生成并持久化在数据卷里
secret() {
    local file="$STATE_DIR/$1"
    if [ ! -s "$file" ]; then
        tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$2" > "$file"
        chmod 600 "$file"
    fi
    cat "$file"
}
QQCACHE_TOKEN="${QQCACHE_TOKEN:-$(secret access-token 32)}"
WEBUI_PASSWORD="${WEBUI_PASSWORD:-$(secret webui-password 16)}"
export QQCACHE_TOKEN WEBUI_PASSWORD
echo "WhiskerWind: WebUI 端口 ${WEBUI_PORT:-3000}，OneBot 端口 ${QQCACHE_PORT:-5700}" >&2
echo "WhiskerWind: 凭据见数据卷 access-token / webui-password（或自行设置 QQCACHE_TOKEN / WEBUI_PASSWORD）" >&2

GEOM="${SCREEN%x*}"
DEPTH="${SCREEN##*x}"
# -nolisten local：不开抽象 socket。host 网络下抽象 socket 在宿主的网络命名空间里，
# 会和宿主机自己的 :99 冲突，宿主上的程序也能连进来
XVNC_ARGS=(:99 -geometry "$GEOM" -depth "$DEPTH" -rfbport "$VNC_PORT"
    -nolisten tcp -nolisten local -AlwaysShared)
if [ -n "$VNC_PASSWORD" ]; then
    printf '%s\n' "$VNC_PASSWORD" | tigervncpasswd -f > /tmp/vncpasswd
    XVNC_ARGS+=(-SecurityTypes VncAuth -PasswordFile /tmp/vncpasswd)
else
    XVNC_ARGS+=(-SecurityTypes None)
fi
if [ "$vnc_local" = 1 ]; then
    XVNC_ARGS+=(-localhost)
elif [ "$VNC_HOST" != 0.0.0.0 ]; then
    XVNC_ARGS+=(-interface "$VNC_HOST")
fi
# 重启幂等：X display 锁文件同样会让 Xtigervnc 拒起
rm -f "/tmp/.X${DISPLAY#*:}-lock" "/tmp/.X11-unix/X${DISPLAY#*:}"
Xtigervnc "${XVNC_ARGS[@]}" &
for _ in $(seq 1 50); do
    xdpyinfo >/dev/null 2>&1 && break
    sleep 0.1
done

# 重启幂等：上次运行留下的 pid/socket 残留会让 dbus-daemon 拒起
rm -f /run/dbus/pid /run/dbus/system_bus_socket /run/dbus/system_bus_socket.tv
mkdir -p /run/dbus
dbus-daemon --system --fork

eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS
# 落盘给 docker exec 进来的进程用：/proc/1/environ 只含 exec 时环境，
# 拿不到这个运行时 export；docker exec 进来的进程从这里读
echo "$DBUS_SESSION_BUS_ADDRESS" > "$STATE_DIR/dbus-addr"

openbox &
sleep 0.5

# host.docker.internal：host 网络下就是本机；bridge 网络下是网关（docker run 的
# --add-host 没加时从 /proc/net/route 读，十六进制小端序）
if ! grep -q "host.docker.internal" /etc/hosts 2>/dev/null; then
    if [ -e /sys/class/net/docker0 ]; then
        echo "127.0.0.1 host.docker.internal" >> /etc/hosts
    else
        gw_hex=$(awk '$2=="00000000" {print $3; exit}' /proc/net/route 2>/dev/null)
        if [ -n "$gw_hex" ]; then
            gw=$(printf '%d.%d.%d.%d' "0x${gw_hex:6:2}" "0x${gw_hex:4:2}" "0x${gw_hex:2:2}" "0x${gw_hex:0:2}")
            echo "$gw host.docker.internal" >> /etc/hosts
        fi
    fi
fi

# Xvfb 默认 ~600s 无输入就 blank，/screen 和 QR 裁图会拿到纯黑。
# xset 禁掉 blanking/DPMS 是主开关；Shift 保活循环做兜底（xset 缺失也能活）。
if command -v xset >/dev/null 2>&1; then
    xset s off s noblank -dpms 2>/dev/null || true
fi
(while :; do DISPLAY=$DISPLAY xdotool key Shift 2>/dev/null; sleep 240; done) &

/usr/libexec/at-spi-bus-launcher --launch-immediately >/dev/null 2>&1 &
sleep 0.5

# QQ 和 serve 都由 PID 1 的主循环守护：QQ 会因手机登出/崩溃退出，serve 可能
# 因 pyatspi 的原生崩溃无声退出，任何一个死掉都在 10s 内重新拉起，容器本身不退。
# 空壳 QQ 自检探针：渲染器死掉时主进程还活着（kill -0 测不出），但它
# 在 AT-SPI 桌面上的窗口树会塌掉。exit 0=活 1=空壳 2=a11y 总线侧故障
cat > "$STATE_DIR/qq_alive_probe.py" <<'PY'
import sys
import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

Atspi.init()
d = Atspi.get_desktop(0)
try:
    n = d.get_child_count()
except Exception:
    sys.exit(2)
if n == 0:
    sys.exit(2)
pid = int(sys.argv[1])
for i in range(n):
    app = d.get_child_at_index(i)
    try:
        if app is None or app.get_process_id() != pid:
            continue
    except Exception:
        continue
    for j in range(app.get_child_count()):
        w = app.get_child_at_index(j)
        try:
            if w is not None and w.get_child_count() > 0:
                sys.exit(0)
        except Exception:
            pass
sys.exit(1)
PY

qq_pid=""
serve_pid=""
bff_pid=""
spawn_bff() {
    pgrep -f "node /opt/webui/server/index.js" >/dev/null 2>&1 && return
    PORT="${WEBUI_PORT:-3000}" HOST="${WEBUI_HOST:-0.0.0.0}" \
    QQCACHE_UPSTREAM="${QQCACHE_UPSTREAM:-http://127.0.0.1:${QQCACHE_PORT:-5700}}" \
    /usr/local/bin/node /opt/webui/server/index.js >>/var/log/webui.log 2>&1 &
    bff_pid=$!
}
spawn_serve() {
    pgrep -f "python3 -m qqcache serve" >/dev/null 2>&1 && return
    # shellcheck disable=SC2206  # QQCACHE_CMD/QQCACHE_ARGS 故意按空格拆成多个参数
    # QQCACHE_POST_URL、QQCACHE_WS_REVERSE_URL、QQCACHE_TEST_TRIGGER 由 serve
    # 直接从环境变量读取，这里不再转成参数，否则地址会注册两次
    local py=(python3 -m qqcache ${QQCACHE_CMD:-serve --host ${QQCACHE_HOST:-0.0.0.0} --port ${QQCACHE_PORT:-5700}}
        --db "$STATE_DIR/qqcache.sqlite3"
        --access-token "$QQCACHE_TOKEN"
        --heartbeat 5 ${QQCACHE_ARGS:-})
    if [ "${QQCACHE_GDB:-0}" = "1" ] && command -v gdb >/dev/null 2>&1; then
        # 诊断模式（QQCACHE_GDB=1）：native 崩溃没有 traceback——套 gdb
        # 让段错误把全线程 backtrace 落进日志再死。默认不开：gdb 的 vfork
        # 跟踪在高频 xdotool 调用下制造噪音和额外延迟，还可能在信号边缘
        # 把活进程带走。
        gdb -batch -ex "set pagination off" \
            -ex "handle SIGPIPE nostop noprint pass" -ex run \
            -ex "thread apply all bt" -ex quit \
            --args "${py[@]}" >>/var/log/qqcache.log 2>&1 &
    else
        "${py[@]}" >>/var/log/qqcache.log 2>&1 &
    fi
    serve_pid=$!
}
trap 'kill "$qq_pid" "$serve_pid" "$bff_pid" 2>/dev/null; exit 0' TERM INT

# QQ 主进程（argv 里没有 --type= 的 /opt/QQ/qq）；没有则输出空
find_main_qq() {
    # set -euo pipefail 下 pgrep 没匹配会返回 1、把整个脚本带走（首启时必然
    # 没有 QQ）——这里「找不到」是正常结果，统一返回 0
    { pgrep -f "^/opt/QQ/qq " || true; } | while read -r p; do
        tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q -- "--type=" || echo "$p"
    done | head -1 || true
}

# 盯住 $qq_pid 直到它退出；期间顺便巡 serve/BFF（死了 10s 内重拉）
watch_qq() {
    qq_spawned_at=$SECONDS
    qq_dead_count=0
    while kill -0 "$qq_pid" 2>/dev/null; do
        sleep 10
        pgrep -f "python3 -m qqcache serve" >/dev/null 2>&1 || {
            echo "qqcache serve died, respawning" >>/var/log/qqcache.log
            spawn_serve
        }
        pgrep -f "node /opt/webui/server/index.js" >/dev/null 2>&1 || {
            echo "webui bff died, respawning" >>/var/log/webui.log
            spawn_bff
        }
        # 空壳检测：主进程活着但窗口树塌了（渲染器死透）时 kill -0 依然
        # 通过——a11y 是唯一可信死活信号。连续 12 次（≈2min）才杀，
        # 避开启动注册延迟/总线抖动误杀；exit 2 = 总线侧故障不计数。
        # 覆盖不到「树完好但渲染卡死」的形态——那种只能人工判断。
        if [ $((SECONDS - qq_spawned_at)) -gt 150 ]; then
            qq_alive_rc=0
            python3 "$STATE_DIR/qq_alive_probe.py" "$qq_pid" >/dev/null 2>&1 || qq_alive_rc=$?
            case "$qq_alive_rc" in
                0) qq_dead_count=0 ;;
                2) ;;
                *)
                    qq_dead_count=$((qq_dead_count + 1))
                    if [ "$qq_dead_count" -ge 12 ]; then
                        echo "qq a11y tree gone (hollow process), killing for relaunch" >&2
                        kill "$qq_pid" 2>/dev/null || true
                        qq_dead_count=0
                    fi
                    ;;
            esac
        fi
    done
}

while :; do
    # QQ 不能自动获焦：窗口一有键盘焦点，通知通道就静默
    # --disable-hang-monitor：渲染器无响应看门狗会在切重会话时把渲染器
    # dump+杀掉（ud2 + crashpad，签名固定）——关掉换「慢但能活」。
    # 代价：真·死锁的渲染器不再自愈，靠上面的 a11y 空壳检测兜底。
    # 其余 flag 可经 QQCACHE_QQ_FLAGS 追加/覆盖。
    # 已经有 QQ 主进程在跑（比如上一轮它自我重启出来的）就直接接管
    qq_pid=$(find_main_qq)
    if [ -z "$qq_pid" ]; then
        /opt/QQ/qq --force-renderer-accessibility --no-sandbox ${QQCACHE_QQ_FLAGS:---disable-hang-monitor} &
        qq_pid=$!
    else
        echo "adopting running qq pid $qq_pid" >&2
    fi
    spawn_serve
    spawn_bff
    watch_qq
    echo "qq $qq_pid exited" >&2
    # QQ 被挤下线/热更新后会用 --relaunch 自己重启：原进程退出时新的主进程
    # 可能已经在了，或几秒内出现。给它 10s 冒头，有就在下一轮接管，不再另起
    # 一个——两个 QQ 共用一个账号目录会互相干扰
    for _ in $(seq 1 10); do
        if [ -n "$(find_main_qq)" ]; then break; fi
        sleep 1
    done
    [ -n "$(find_main_qq)" ] || { echo "no qq left, relaunching in 3s" >&2; sleep 3; }
done
