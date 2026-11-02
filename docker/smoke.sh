#!/usr/bin/env bash
# 冷启动冒烟测试：用 up.sh 同一套参数起一个一次性容器（空 QQ 配置、空数据目录），
# 确认 entrypoint 能把 QQ / serve / WebUI BFF 全部拉起来、且不进重启循环。
# 单元测试覆盖不到 entrypoint——它只在容器启动时跑，坏了就是整个容器起不来。
#   docker/smoke.sh            # 约 1–2 分钟，结束后自动清理
# 不碰正在用的容器：容器名、数据目录独立，走 bridge 网络不占宿主端口。
# 测指定镜像：QQ_IMAGE=whiskerwind:dev docker/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=whiskerwind-smoke
TOKEN=smoketok
DATA=$(mktemp -d "${TMPDIR:-/tmp}/whiskerwind-smoke.XXXXXX")
fail=0

cleanup() {
    # 容器里以 root 写的文件宿主用户删不掉，先在容器里清
    docker exec "$NAME" sh -c 'rm -rf /root/.config/QQ/* /var/lib/qqcache/*' >/dev/null 2>&1 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$DATA" 2>/dev/null || true
}
trap cleanup EXIT
check() { # check <名字> <命令...>
    local name=$1; shift
    if "$@"; then echo "  ok   $name"; else echo "  FAIL $name"; fail=1; fi
}

echo "[1/3] 静态检查"
for f in docker/*.sh; do
    check "bash -n $f" bash -n "$f"
done
SHELLCHECK=$(command -v shellcheck || echo "$HOME/.local/share/shck/bin/shellcheck")
if [ -x "$SHELLCHECK" ]; then
    check "shellcheck" "$SHELLCHECK" -S warning docker/*.sh
else
    echo "  skip shellcheck（未安装）"
fi

echo "[2/3] 冷启动 $NAME"
docker rm -f "$NAME" >/dev/null 2>&1 || true
QQ_CONTAINER=$NAME QQ_DATA=$DATA QQ_NETWORK=bridge QQ_PORTS="" QQ_RESTART=no \
    QQCACHE_TOKEN=$TOKEN WEBUI_PASSWORD=smoke \
    docker/up.sh >/dev/null 2>&1 || true
IP=$(docker inspect "$NAME" -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || true)

api_ok() {
    curl -sf -m 3 "http://$IP:5700/get_version_info?access_token=$TOKEN" -d '{}' >/dev/null
}
login_state() {
    curl -s -m 5 "http://$IP:5700/get_login_state?access_token=$TOKEN" -d '{}' |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["state"])' 2>/dev/null || true
}
running() { [ "$(docker inspect "$NAME" -f '{{.State.Running}}' 2>/dev/null)" = true ]; }

state=""
for _ in $(seq 1 60); do   # QQ 冷启动 + AT-SPI 注册最长约 1 分钟
    running || break
    if [ -n "$IP" ] && api_ok; then
        state=$(login_state)
        case "$state" in login_required|logged_in) break ;; esac
    fi
    sleep 3
done

echo "[3/3] 检查"
check "容器在运行" running
check "serve API 应答" api_ok
check "QQ 登录态可读（$state）" test "$state" = login_required -o "$state" = logged_in
check "WebUI BFF 应答" curl -sf -m 3 -o /dev/null "http://$IP:3000/"
check "只有一个 QQ 主进程" test "$(docker exec "$NAME" sh -c \
    'for p in $(pgrep -f "^/opt/QQ/qq "); do tr "\0" " " < /proc/$p/cmdline | grep -q -- --type= || echo $p; done' | wc -l)" = 1
sleep 15
check "15s 后仍在运行" running
if [ "$fail" != 0 ]; then
    echo "--- 容器日志（末 30 行）"
    docker logs --tail 30 "$NAME" 2>&1 | grep -v vncext || true
    echo "冒烟测试失败"
    exit 1
fi
echo "冒烟测试通过"
