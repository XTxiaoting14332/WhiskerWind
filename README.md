<div align="center">

<img src="img/whiskerwind-sticker.svg" width="180" alt="WhiskerWind">

# WhiskerWind

_基于官方 Linux QQ 客户端的 Bot 协议端_

> 夜风掠过窗沿，把每一条消息都捎回来。

[![CI](https://github.com/XTxiaoting14332/WhiskerWind/actions/workflows/ci.yml/badge.svg)](https://github.com/XTxiaoting14332/WhiskerWind/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)
[![OneBot v11](https://img.shields.io/badge/OneBot-v11-black)](https://github.com/botuniverse/onebot-11)
[![Satori](https://img.shields.io/badge/Satori-supported-purple)](https://satori.chat)
[![ghcr](https://img.shields.io/badge/ghcr.io-whiskerwind-2496ED?logo=docker&logoColor=white)](https://github.com/XTxiaoting14332/WhiskerWind/pkgs/container/whiskerwind)

</div>

> [!WARNING]
> **本项目仅作为概念验证（PoC）**：用于验证「不注入、不修改客户端，只通过系统通知和无障碍接口驱动官方 QQ」这条路线是否可行。它不是成熟的生产级协议端，不保证稳定性和长期维护，请勿用于重要账号或生产环境。

---

## 欢迎

WhiskerWind 把 Docker 容器里运行的**官方 Linux QQ 桌面客户端**变成一个机器人协议端：

- **不注入、不修改客户端、不走私有协议**：消息靠系统通知 + 无障碍接口（AT-SPI）读取，发送靠模拟操作界面完成，和真人用 QQ 的方式一样。
- **开箱即用**：一个容器包含 QQ、协议端和 WebUI，在网页上扫码登录即可。
- **多协议**：OneBot v11（HTTP / 正向 WS / 反向 WS / HTTP POST）、Satori，以及自定义的 [automatic 协议](docs/automatic-protocol.md)。
- **宁可不发，不可发错**：发送前校验目标会话，对不上就拒绝；内置出站限速与重复内容拦截。
- **真实 QQ 号**：群成员发言时自动解析真实 QQ 号，解析不出才回落到稳定的合成 id。

夜风是项目的看板猫 🐈‍⬛。

## 快速开始

需要一台 x86_64 Linux 主机和 Docker。

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

- `--network host`：容器与宿主共用网络，框架和协议端互相用 `127.0.0.1` 访问即可；
- `QQCACHE_WS_REVERSE_URL`：反向 WS 地址，换成你的机器人框架的地址；不用反向 WS 可以去掉这一行；
- `data/QQ` 保存 QQ 登录态，`data/qqcache` 保存消息库、凭据和配置，两者都要挂载，否则重建容器后要重新扫码。

需要固定凭据或远程查看桌面时，可以再加上：

```bash
  -e QQCACHE_TOKEN=<自定义 access_token> \
  -e WEBUI_PASSWORD=<自定义 WebUI 密码> \
  -e VNC_HOST=0.0.0.0 -e VNC_PASSWORD=<VNC 密码> \
```

也可以使用 Docker Compose，在同一目录下创建 `docker-compose.yml`：

```yaml
services:
  whiskerwind:
    image: ghcr.io/xtxiaoting14332/whiskerwind:latest
    container_name: whiskerwind
    network_mode: host
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      WEBUI_PORT: 3000                 # WebUI
      QQCACHE_PORT: 5700               # OneBot v11 HTTP / 正向 WS
      # 反向 WS 与 HTTP POST 上报，多个地址用逗号分隔
      QQCACHE_WS_REVERSE_URL: ws://127.0.0.1:8080/onebot/v11/ws
      # QQCACHE_POST_URL: http://127.0.0.1:8080/onebot/v11/http
      # 不设置时首次启动自动生成，保存在 data/qqcache 中
      # QQCACHE_TOKEN: change-me
      # WEBUI_PASSWORD: change-me
      # VNC 默认只监听 127.0.0.1；对外开放时必须设置密码
      # VNC_HOST: 0.0.0.0
      # VNC_PASSWORD: change-me
    volumes:
      - ./data/QQ:/root/.config/QQ
      - ./data/qqcache:/var/lib/qqcache
```

```bash
docker compose up -d
```

未设置 `QQCACHE_TOKEN` / `WEBUI_PASSWORD` 时，首次启动会自动生成访问凭据并保存在数据卷中：

```bash
docker exec whiskerwind cat /var/lib/qqcache/webui-password   # WebUI 登录密码
docker exec whiskerwind cat /var/lib/qqcache/access-token     # OneBot access_token
```

打开 `http://<主机>:3000` 登录 WebUI，QQ 未登录时会弹出二维码，用手机 QQ 扫码即可。登录态保存在 `data/QQ` 里，重启容器不用重新扫码。

## 接入机器人框架

OneBot v11 服务在 `5700` 端口（HTTP API 与正向 WS 同端口），请求需带 `access_token`：

| 方式 | 地址 |
|---|---|
| HTTP API | `http://127.0.0.1:5700/<action>` |
| 正向 WebSocket | `ws://127.0.0.1:5700/` |
| 反向 WebSocket | 设置 `QQCACHE_WS_REVERSE_URL`，或在 WebUI「传输管理」中添加 |
| HTTP POST 上报 | 设置 `QQCACHE_POST_URL`，或在 WebUI「传输管理」中添加 |

以 NoneBot2 为例（反向 WS）：容器按上面的示例设置 `QQCACHE_WS_REVERSE_URL=ws://127.0.0.1:8080/onebot/v11/ws`，NoneBot 一侧监听 8080 端口并填写同一个令牌：

```dotenv
# NoneBot .env
DRIVER=~fastapi+~websockets
HOST=127.0.0.1
PORT=8080
ONEBOT_ACCESS_TOKEN=<access-token>
```

Satori（默认 `5500`）和 automatic 协议（默认 `5600`）在 WebUI「传输管理」页启用。Koishi 使用 `@koishijs/plugin-adapter-satori` 连接。

## 配置

都通过环境变量设置，均为可选：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCACHE_TOKEN` | 自动生成 | OneBot / WebUI 上游的 access_token |
| `WEBUI_PASSWORD` | 自动生成 | WebUI 登录密码 |
| `QQCACHE_PORT` / `QQCACHE_HOST` | `5700` / `0.0.0.0` | OneBot 服务监听 |
| `WEBUI_PORT` / `WEBUI_HOST` | `3000` / `0.0.0.0` | WebUI 监听 |
| `QQCACHE_WS_REVERSE_URL` | — | 反向 WS 地址，逗号分隔可写多个 |
| `QQCACHE_POST_URL` | — | HTTP POST 上报地址，逗号分隔可写多个 |
| `QQCACHE_TEST_TRIGGER` | 关 | 设为 `1` 后，收到 `#ww` 会回复服务状态，用于检查链路 |
| `VNC_PORT` / `VNC_HOST` | `5900` / `127.0.0.1` | 查看容器桌面的 VNC；`VNC_HOST` 设为非回环地址（如 `0.0.0.0`）时必须同时设置 `VNC_PASSWORD`，否则容器拒绝启动 |
| `VNC_PASSWORD` | — | VNC 密码（TigerVNC 只取前 8 位） |
| `TZ` | `Asia/Shanghai` | 时区 |

一台机器上跑多个实例时，给每个实例换一组端口即可。如果不想用 host 网络，也可以用 bridge 网络加 `-p` 端口映射，此时容器里的 `127.0.0.1` 指容器自己，宿主上的框架地址要写 `host.docker.internal`。bridge 网络下要通过端口映射访问 VNC，还需设置 `VNC_HOST=0.0.0.0` 和 `VNC_PASSWORD`。

## 已知限制

协议端通过界面操作 QQ，有些事情做不到或做不快：

- **发送是串行的**：所有发送都要排队经过同一个 QQ 窗口，每条约 1–5 秒，调用超时建议 ≥ 60 秒。
- **免打扰会话收不到消息**：QQ 不为这类会话弹通知。设置了「接收不提醒」的群同理。
- **只有服务运行期间的消息**：`get_msg_history`、群成员列表等都只包含服务启动后经过的消息和发过言的成员。
- **消息段有限**：支持文字、@、图片、引用；表情、语音、视频降级为占位文本，合并转发、卡片消息不支持。
- **版本锁定**：界面结构随 QQ 版本变化，镜像固定使用验证过的 QQ 3.2.33。

与标准 OneBot v11、Satori 的逐项功能差异见 [docs/compatibility.md](docs/compatibility.md)，实现原理见 [docs/architecture.md](docs/architecture.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 实现原理：消息接收、身份解析、发送流程 |
| [docs/compatibility.md](docs/compatibility.md) | 与标准 OneBot v11 / Satori 的功能差异 |
| [docs/webui-api.md](docs/webui-api.md) | OneBot 接口、事件、扩展 action、限制 |
| [docs/automatic-protocol.md](docs/automatic-protocol.md) | automatic 协议 |
| [docs/webui-design.md](docs/webui-design.md) | WebUI 设计 |
| [webui/README.md](webui/README.md) | WebUI 本地开发 |

## 开发

```bash
python3 -m unittest discover -s tests      # 单元测试
docker build -f docker/Dockerfile -t whiskerwind:dev .
QQ_IMAGE=whiskerwind:dev docker/smoke.sh   # 冷启动冒烟测试
QQ_IMAGE=whiskerwind:dev docker/up.sh      # 挂载本地代码启动，改完重启容器即生效
```

## 免责声明

- 本项目仅用于概念验证（PoC）与技术研究，按「现状」提供，不对使用后果承担任何责任。
- 本项目与腾讯公司无关。QQ 是腾讯公司的商标，客户端版权归腾讯所有；镜像构建时从官方地址下载客户端，仓库和镜像不修改它。
- 使用机器人存在账号被限制或封禁的风险，请自行承担。不要用于发送垃圾信息、骚扰他人或任何违法用途。
- **请不要在 QQ 官方群聊以及哔哩哔哩、微博、知乎、抖音等影响力较大的平台上宣传或讨论本项目。**

## 许可证

本项目以 [GPL-3.0](LICENSE) 协议开源。
