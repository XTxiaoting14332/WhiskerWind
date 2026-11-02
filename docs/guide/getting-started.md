# 快速开始

本页说明如何在 Linux 主机上启动 WhiskerWind 容器、完成 QQ 登录，并确认协议端正常运行。

## 运行环境

- x86_64 架构的 Linux 主机，并已安装 Docker。镜像仅提供 `linux/amd64` 版本。
- 主机需要能够访问 `ghcr.io`，用于拉取镜像。
- 本页示例使用 `--network host`（主机网络模式）。该模式仅适用于 Linux 主机。桥接网络的配置方法见[配置参数](/guide/configuration)。

## 启动容器

```bash
mkdir -p whiskerwind && cd whiskerwind

docker run -d --name whiskerwind \
  --network host \
  --restart unless-stopped \
  -e TZ=Asia/Shanghai \
  -e WEBUI_PORT=3000 \
  -e QQCACHE_PORT=5700 \
  -e QQCACHE_WS_REVERSE_URL=ws://127.0.0.1:8080/onebot/v11/ws \
  -v "$PWD/data/QQ:/root/.config/QQ" \
  -v "$PWD/data/qqcache:/var/lib/qqcache" \
  ghcr.io/xtxiaoting14332/whiskerwind:latest
```

各参数的作用如下。

| 参数 | 说明 |
|---|---|
| `--network host` | 容器与主机共用网络。框架与协议端之间可以直接通过 `127.0.0.1` 访问。 |
| `--restart unless-stopped` | 容器异常退出或主机重启后自动启动。执行 `docker stop` 后不会自动启动。 |
| `-e TZ` | 容器的时区，影响日志中的时间显示。 |
| `-e WEBUI_PORT`、`-e QQCACHE_PORT` | WebUI 与 OneBot 接口的监听端口。 |
| `-e QQCACHE_WS_REVERSE_URL` | 反向 WebSocket 的目标地址。不需要反向 WebSocket 时删除此参数。 |
| `-v "$PWD/data/QQ:/root/.config/QQ"` | QQ 客户端的数据目录，包含登录态，以及收到的图片、语音等文件。 |
| `-v "$PWD/data/qqcache:/var/lib/qqcache"` | 协议端的数据目录，包含消息库、访问凭据与配置。 |

两个数据目录都应当挂载。

- 缺少 `data/QQ` 时，容器重建后需要重新扫码登录。
- 缺少 `data/qqcache` 时，容器重建后访问凭据、设备标识（`machine-id`）与消息 ID 映射都会重新生成。设备标识发生变化后，QQ 可能将本机识别为新设备。

省略 `-v` 参数时，Docker 会创建匿名卷，不便于查找和备份，不建议使用。

## 使用 Docker Compose 启动

在同一目录下创建 `docker-compose.yml`：

```yaml
services:
  whiskerwind:
    image: ghcr.io/xtxiaoting14332/whiskerwind:latest
    container_name: whiskerwind
    network_mode: host
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      WEBUI_PORT: 3000
      QQCACHE_PORT: 5700
      # 反向 WebSocket 地址，多个地址用逗号分隔。不需要时删除此行
      QQCACHE_WS_REVERSE_URL: ws://127.0.0.1:8080/onebot/v11/ws
      # HTTP POST 上报地址，多个地址用逗号分隔
      # QQCACHE_POST_URL: http://127.0.0.1:8080/onebot/v11/http
      # 未设置时，首次启动自动生成并保存在数据卷中
      # QQCACHE_TOKEN: change-me
      # WEBUI_PASSWORD: change-me
      # VNC 默认只监听 127.0.0.1。对外开放时必须同时设置 VNC_PASSWORD
      # VNC_HOST: 0.0.0.0
      # VNC_PASSWORD: change-me
    volumes:
      - ./data/QQ:/root/.config/QQ
      - ./data/qqcache:/var/lib/qqcache
```

```bash
docker compose up -d
```

## 首次启动过程

容器的入口脚本（`docker/entrypoint.sh`）作为主进程运行，按以下顺序启动各组件：

1. 创建数据目录，读取或生成设备标识。
2. 读取或生成访问凭据（`access-token` 与 `webui-password`）。
3. 启动虚拟显示（Xtigervnc，显示编号 `:99`）、D-Bus 会话总线、openbox 窗口管理器与 AT-SPI 无障碍总线。
4. 启动 QQ 客户端，并启用渲染器的无障碍功能。
5. 启动协议端（`qqcache serve`）与 WebUI 后端，并持续守护这三个进程。

冷启动时，QQ 与无障碍接口的注册最长约需 1 分钟。这一阶段内，`get_version_info` 等不访问界面的接口可以正常应答，涉及界面的接口可能暂时无法应答。入口脚本的输出可以通过 `docker logs whiskerwind` 查看。

## 获取访问凭据

首次启动时，如果没有通过环境变量指定凭据，入口脚本会随机生成凭据并保存在数据卷中，文件权限为 `600`：

```bash
docker exec whiskerwind cat /var/lib/qqcache/access-token    # OneBot 访问令牌（access_token）
docker exec whiskerwind cat /var/lib/qqcache/webui-password  # WebUI 登录密码
```

- `access-token` 由 OneBot 接口与 WebUI 后端共用，长度为 32 位字母和数字。
- `webui-password` 的长度为 16 位字母和数字。
- 设置了 `QQCACHE_TOKEN` 或 `WEBUI_PASSWORD` 环境变量时，启动使用环境变量的值，数据卷中的同名文件不再被读取。

## 扫码登录

1. 在浏览器中打开 `http://<主机地址>:3000`，输入 WebUI 密码。
2. 进入「状态与设置」页。QQ 未登录时，页面会显示登录二维码。
3. 使用手机 QQ 扫描二维码，并按手机端的提示完成确认。

二维码过期后，协议端会自动触发 QQ 的刷新操作，重新获取二维码。登录完成后，登录态保存在 `data/QQ` 中。之后重启容器无需重新扫码，除非账号被下线（见[运行与维护](/guide/operations)）。

## 验证服务

```bash
TOKEN=$(docker exec whiskerwind cat /var/lib/qqcache/access-token)

# 版本信息。该接口不访问界面，启动后即可应答
curl -s "http://127.0.0.1:5700/get_version_info?access_token=$TOKEN"

# 登录状态
curl -s -X POST "http://127.0.0.1:5700/get_login_state?access_token=$TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

`get_login_state` 返回的 `data.state` 取值如下：

| 取值 | 含义 |
|---|---|
| `logged_in` | 已登录 |
| `login_required` | 等待扫码登录 |
| `unknown` | 界面状态暂时无法识别，例如正在加载。可稍后重试，或在 WebUI 中查看屏幕截图 |
| `not_running` | QQ 客户端未运行 |

## 停止、启动与升级

```bash
docker stop whiskerwind     # 停止容器
docker start whiskerwind    # 启动容器
```

停止或启动容器不会影响数据卷中的内容。升级镜像或修改启动参数时，先拉取新镜像，删除旧容器，再以相同的数据卷挂载重新执行 `docker run`：

```bash
docker pull ghcr.io/xtxiaoting14332/whiskerwind:latest
docker rm -f whiskerwind    # 只删除容器。数据卷位于宿主机目录，不受影响
```

升级前建议备份数据目录，备份方法见[运行与维护](/guide/operations)。

## 后续步骤

- [配置参数](/guide/configuration)：环境变量、端口与数据目录的说明。
- [接入 Bot 框架](/guide/bot-frameworks)：OneBot v11、Satori 与 automatic 协议的连接方式。
- [运行与维护](/guide/operations)：日志、健康检查、备份与安全建议。
