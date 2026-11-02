#!/usr/bin/env bash
# 开发用：拉起单容器全栈（QQ GUI + qqcache serve + WebUI BFF），代码 bind-mount 进去，
# 改完 repo 重启容器即生效。普通部署直接用 README 里的 docker run / compose。
#   - 状态外置：$QQ_DATA/QQ = QQ 登录态，$QQ_DATA/qqcache = 数据库、凭据、machine-id
#   - docker/ 挂整个目录而不是单个 entrypoint.sh：单文件挂载绑的是 inode，
#     编辑器「写临时文件再 rename」之后容器里还是旧文件
# 幂等：容器存在 → docker start；不存在 → docker run
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${QQ_CONTAINER:-whiskerwind}"
IMAGE="${QQ_IMAGE:-ghcr.io/xtxiaoting14332/whiskerwind:latest}"
DATA="${QQ_DATA:-$HOME/data}"
RESTART="${QQ_RESTART:-unless-stopped}"
NETWORK="${QQ_NETWORK:-host}"
API_PORT="${QQCACHE_PORT:-5700}"
# bridge 网络时的端口发布（冒烟测试传空值：不占宿主端口，直接走容器 IP）
read -r -a PORTS <<< "${QQ_PORTS-"-p 127.0.0.1:5900:5900 -p 127.0.0.1:3000:3000"}"

if ! docker inspect "$NAME" >/dev/null 2>&1; then
    echo "容器 $NAME 不存在，创建中（网络: $NETWORK）"
    mkdir -p "$DATA/QQ" "$DATA/qqcache"
    net=(--network "$NETWORK")
    [ "$NETWORK" = host ] || net+=(--add-host host.docker.internal:host-gateway "${PORTS[@]}")
    extra=()
    # 旧镜像不带 node 时可以从宿主挂一个进去
    [ -n "${NODE_BIN:-}" ] && extra+=(-v "$NODE_BIN:/usr/local/bin/node:ro")
    docker run -d \
      --name "$NAME" \
      --restart "$RESTART" \
      "${net[@]}" \
      -e TZ=Asia/Shanghai \
      -e QQCACHE_TOKEN -e WEBUI_PASSWORD -e QQCACHE_PORT -e WEBUI_PORT -e VNC_PORT -e VNC_HOST -e VNC_PASSWORD \
      -e QQCACHE_POST_URL -e QQCACHE_WS_REVERSE_URL \
      -e QQCACHE_TEST_TRIGGER="${QQCACHE_TEST_TRIGGER-1}" \
      -v "$DATA/QQ:/root/.config/QQ" \
      -v "$DATA/qqcache:/var/lib/qqcache" \
      -v "$PWD/qqcache:/opt/qqcache/qqcache" \
      -v "$PWD/webui/server:/opt/webui/server" \
      -v "$PWD/webui/public:/opt/webui/public" \
      -v "$PWD/docker:/opt/qqcache-docker:ro" \
      --entrypoint /opt/qqcache-docker/entrypoint.sh \
      "${extra[@]}" \
      "$IMAGE"
    sleep 3
else
    docker start "$NAME" >/dev/null 2>&1 || true
    sleep 2
fi

if [ "$(docker inspect "$NAME" -f '{{.HostConfig.NetworkMode}}')" = host ]; then
    HOST=127.0.0.1
else
    HOST=$(docker inspect "$NAME" -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
fi
[ -n "$HOST" ] || { echo "容器未运行"; exit 1; }
TOKEN="${QQCACHE_TOKEN:-$(docker exec "$NAME" cat /var/lib/qqcache/access-token 2>/dev/null || true)}"
echo "容器 $NAME @ $HOST"

# 等 API 就绪（AT-SPI 冷启动期间 GUI 类接口会阻塞，version 接口不碰 GUI）
for _ in $(seq 1 15); do
  curl -sf -m 2 "http://$HOST:$API_PORT/get_version_info?access_token=$TOKEN" -d '{}' >/dev/null 2>&1 && break
  sleep 2
done
echo "API:   http://$HOST:$API_PORT  $(curl -s -m 3 "http://$HOST:$API_PORT/get_login_state?access_token=$TOKEN" -d '{}' | python3 -c 'import json,sys; d=json.load(sys.stdin).get("data",{}); print(d.get("state","?"))' 2>/dev/null || echo '初始化中')"
echo "WebUI: http://127.0.0.1:${WEBUI_PORT:-3000}  （密码: docker exec $NAME cat /var/lib/qqcache/webui-password）"
echo "VNC:   127.0.0.1:${VNC_PORT:-5900}"
