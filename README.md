<div align="center">

<img src="img/whiskerwind-sticker.svg" width="180" alt="WhiskerWind">

# WhiskerWind | 风须

_基于官方 Linux QQ 客户端的 Bot 协议端_

> 园果不留双子莺，金瞳亦识夜风清。

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

WhiskerWind 把 Docker 容器里运行的**官方 Linux QQ 桌面客户端**通过自动化操作实现协议端：

- **不注入、不修改客户端、不走私有协议**：消息靠系统通知 + 无障碍接口（AT-SPI）读取，发送靠模拟操作界面完成，和真人用 QQ 的方式一样。
- **开箱即用**：一个容器包含 QQ、协议端和 WebUI，在网页上扫码登录即可。
- **多协议**：OneBot v11（HTTP / 正向 WS / 反向 WS / HTTP POST）、Satori，以及自定义的 [automatic 协议](docs/automatic-protocol.md)。
- **宁可不发，不可发错**：发送前校验目标会话，对不上就拒绝；内置出站限速与重复内容拦截。


夜风是项目的看板猫。

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

## 接入 Bot 框架

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

协议端通过界面操作官方QQ客户端，部分行为做不到或做不快：

- **发送是串行的**：所有发送都要排队经过同一个 QQ 窗口，每条约 1–5 秒，调用超时建议 ≥ 60 秒。
- **免打扰会话收不到消息**：QQ 不为这类会话弹通知。设置了「接收不提醒」的群同理。
- **只有服务运行期间的消息**：`get_msg_history`、群成员列表等都只包含服务启动后经过的消息和发过言的成员。
- **消息段有限**：支持文字、@、图片、引用；表情、语音、视频降级为占位文本，合并转发、卡片消息暂不支持。
- **版本锁定**：界面结构随 QQ 版本变化，镜像固定使用验证过的 QQ 3.2.33。

与标准 OneBot v11、Satori 的逐项功能差异见 [docs/compatibility.md](docs/compatibility.md)，实现原理见 [docs/architecture.md](docs/architecture.md)。

## 已知问题

以下问题在实际运行中出现过。排查时，WebUI 的「服务日志」页记录了协议端每一步的处理结果，「状态与设置」页可以查看容器的实时屏幕截图。

### 收到消息后无响应，或很久之后才响应

可能的原因：

- **QQ 界面崩溃后正在重建。** QQ 的渲染进程偶尔会崩溃，重建期间（通常一到几分钟）无法读取和打开会话，这段时间到达的消息会处理失败。协议端会在 65 秒、3 分钟、10 分钟后各巡检一次未读会话进行补处理；该会话有新消息到达时也会一并读取。若 QQ 主进程退出或界面长时间无法恢复，容器会在约 10 秒内重启 QQ。
- **会话设置了免打扰、「接收不提醒」或「只提醒 @」。** QQ 不为这类会话发送通知，协议端无从得知有新消息（「只提醒 @」的群只有 @ 本账号的消息能收到）。请在 QQ 中把需要机器人响应的会话设为正常提醒。
- **界面正忙。** 所有界面操作串行执行。正在发送消息、首次解析群成员身份或批量补处理时，新消息需要排队，可能延迟数秒到数十秒。
- **有人正在通过 VNC 操作 QQ。** QQ 窗口持有焦点时不会为其他会话发送通知，手动点击也会打乱协议端正在进行的操作。VNC 仅建议用于查看，不要在其上操作 QQ。
- **账号已掉线。** 见下文。

### 账号掉线或被挤下线

在其他电脑端登录同一账号、长时间离线或触发风控时，QQ 会被下线。协议端会关闭下线提示框并在 WebUI 弹窗提醒，但**不会自动重新登录**，需要在 WebUI 上重新扫码。掉线期间收到的消息在重新登录后会通过未读巡检补处理。

### 群成员的 `user_id` 先是一个很大的数字，之后又变了

协议端需要通过界面打开资料卡才能得到群成员的真实 QQ 号。解析完成前，消息中的 `user_id` 为合成 id（≥ 900000000000）；解析成功后，协议端推送 `qqcache_member_resolved` 通知（Satori 为 `qqcache/member-resolved` 事件），后续消息使用真实 QQ 号。若机器人按 `user_id` 存储数据（如权限、积分），需要处理该通知以合并身份，或忽略合成 id 的用户。

### 发送消息失败

- **`retcode 1429`**：触发了出站限速或重复内容拦截（同一会话 10 秒 3 条、60 秒 12 条，全局 60 秒 20 条，30 秒内相同内容不能重复发送）。请降低发送频率。
- **「身份验证失败，拒绝发送」「没有找到会话」等**：协议端无法确认目标会话，为避免发错对象而放弃发送。此类失败不会产生任何副作用，可以直接重试；反复出现时请检查目标是否仍在好友或群列表中。
- **调用超时**：一次发送需要 1–5 秒，排队时更久。HTTP 客户端或框架的超时时间建议不少于 60 秒。

### 回复速度慢

发送一条消息通常需要 1–5 秒，首次出现的群成员还需额外 3–6 秒解析身份。会话数量多、置顶会话多或主机负载高时会更慢。这是通过界面操作 QQ 的固有开销，无法消除。

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


## 许可证

本项目以 [GPL-3.0](LICENSE) 协议开源。
