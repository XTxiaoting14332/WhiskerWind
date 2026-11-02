# 配置参数

WhiskerWind 的配置分为三部分：启动时读取的环境变量、运行时通过 WebUI 或管理接口修改的设置，以及数据目录中保存的文件。本页分别说明它们的作用、默认值和优先级。

## 配置方式

- **环境变量**：通过 `docker run -e` 或 Compose 的 `environment` 设置。环境变量在容器创建时写入，修改后需要重建容器（`docker rm` 后重新执行 `docker run`，或执行 `docker compose up -d`）。仅执行 `docker restart` 不会使新值生效。
- **运行时设置**：在 WebUI「通道管理」页或管理接口中创建、修改的通道，以及测试触发器等开关。这些设置保存在数据目录中，重启后仍然生效。
- **优先级**：访问凭据未通过环境变量指定时，才使用数据目录中的文件。测试触发器、成员解析顺序和成员绑定缓存时长在运行时修改后会写入文件。文件存在时，以文件中的值为准，覆盖对应的环境变量或默认值。

## 环境变量

以下变量均为可选。表中的默认值是入口脚本未设置该变量时实际使用的值。

### 凭据与鉴权

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCACHE_TOKEN` | 首次启动时生成 | OneBot 接口与 WebUI 后端使用的 access_token |
| `WEBUI_PASSWORD` | 首次启动时生成 | WebUI 登录密码 |
| `QQCACHE_POST_SECRET` | 空 | HTTP POST 上报的签名密钥。设置后，上报请求带有 `X-Signature` 头 |

### 网络与端口

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCACHE_HOST` | `0.0.0.0` | OneBot 接口（HTTP API 与正向 WebSocket）的监听地址 |
| `QQCACHE_PORT` | `5700` | OneBot 接口的监听端口 |
| `WEBUI_HOST` | `0.0.0.0` | WebUI 的监听地址 |
| `WEBUI_PORT` | `3000` | WebUI 的监听端口 |
| `VNC_HOST` | `127.0.0.1` | VNC 的监听地址。设为非回环地址时，必须同时设置 `VNC_PASSWORD`，否则容器拒绝启动 |
| `VNC_PORT` | `5900` | VNC 的监听端口 |
| `VNC_PASSWORD` | 空 | VNC 密码。TigerVNC 只使用前 8 个字符 |

### 事件上报

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCACHE_WS_REVERSE_URL` | 空 | 反向 WebSocket 地址，多个地址用逗号分隔。启动时创建为「启动参数」通道，同一地址只注册一次 |
| `QQCACHE_POST_URL` | 空 | HTTP POST 上报地址，多个地址用逗号分隔。启动时创建为「启动参数」通道，同一地址只注册一次 |

### 行为调整

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCACHE_TEST_TRIGGER` | 空（关闭） | 设为 `1`、`true`、`yes` 或 `on` 时，收到内容为 `#ww` 的消息会回复服务状态，用于检查链路。其他取值（包括 `0`）均视为关闭 |
| `QQCACHE_POLL_INTERVAL` | `2` | 对当前打开的会话进行轮询的间隔，单位为秒。设为 `0` 时关闭轮询 |
| `QQCACHE_MEMBER_TTL` | `3600` | 群成员名称与 QQ 号绑定关系的缓存时长，单位为秒。设为 `0` 时绑定关系不再过期 |
| `QQCACHE_SELF_ID` | 自动检测 | 登录账号的 QQ 号。默认从 QQ 数据目录中的分区名读取 |
| `QQCACHE_SELF_NAME` | 自动检测 | 登录账号的昵称 |
| `QQCACHE_DEBUG` | 空 | 设为任意非空值时，输出 DEBUG 级别的日志 |

### 高级选项

以下变量通常不需要设置，用于排查问题或调整启动参数。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCACHE_QQ_FLAGS` | `--disable-hang-monitor` | 传给 QQ 客户端的启动参数。设置后会替换默认值，需要时应一并保留默认参数。默认参数用于关闭渲染器无响应看门狗，避免切换会话时渲染器被误杀 |
| `QQCACHE_GDB` | 空 | 设为 `1` 时，协议端在 gdb 中运行，原生崩溃时输出各线程的调用栈。官方镜像未安装 gdb，此选项在官方镜像中不生效 |
| `QQCACHE_CMD` | `serve --host … --port …` | 替换协议端的子命令及其默认参数 |
| `QQCACHE_ARGS` | 空 | 追加到协议端命令行末尾的参数，按空格拆分 |
| `QQCACHE_UPSTREAM` | `http://127.0.0.1:<QQCACHE_PORT>` | WebUI 后端访问的 OneBot 接口地址 |
| `SESSION_TTL_MS` | `604800000`（7 天） | WebUI 登录会话的有效期，单位为毫秒 |
| `SCREEN` | `1920x1080x24` | 虚拟显示的分辨率和色深。项目按该分辨率验证，一般不需要修改 |
| `TZ` | `Asia/Shanghai` | 容器时区 |

## 运行时设置

| 设置 | 修改方式 | 保存位置 | 说明 |
|---|---|---|---|
| 通道：OneBot 的 HTTP 监听、反向 WebSocket 与 HTTP POST 上报，以及 Satori、automatic 服务端与 automatic 反向 WebSocket | WebUI「通道管理」页，或 `add_transport`、`update_transport`、`del_transport` 管理接口 | `qqcache.sqlite3.transports.json`（权限 `600`） | 运行时创建的通道在重启后自动恢复。由环境变量或启动参数产生的通道不会保存，也不能通过接口修改或删除。文件中包含各通道的令牌 |
| 旧版本的 Satori、automatic 配置 | 启动时自动迁移为通道 | `qqcache.sqlite3.protocols.json` 改名为 `.migrated` | 只迁移已启用的协议。迁移后不再读取该文件 |
| 测试触发器 | `set_test_trigger` 管理接口 | `qqcache.sqlite3.test_trigger` | 文件存在时，其值覆盖 `QQCACHE_TEST_TRIGGER` |
| 成员解析顺序 | `set_resolve_first` 管理接口 | `qqcache.sqlite3.resolve_first` | 默认值为先解析群成员身份，再推送群消息。参见[已知问题与排查](/guide/known-issues) |
| 成员绑定缓存时长 | `set_member_ttl` 管理接口 | `qqcache.sqlite3.member_ttl` | 文件存在时，其值覆盖 `QQCACHE_MEMBER_TTL` |

管理接口的调用方式与参数见 [WebUI 接口](/webui-api)。

## 端口一览

| 服务 | 容器内端口 | 默认监听地址 | 启用方式 | 鉴权 |
|---|---|---|---|---|
| OneBot v11（HTTP API 与正向 WebSocket） | 5700 | `0.0.0.0` | 默认启用 | access_token |
| WebUI | 3000 | `0.0.0.0` | 默认启用 | WebUI 密码，登录后使用会话凭据 |
| Satori | 5500 | `0.0.0.0` | 在 WebUI「通道管理」中新建 Satori 通道 | 令牌。监听非回环地址时必须有令牌，未设置时自动生成 |
| automatic | 5600 | `0.0.0.0` | 在 WebUI「通道管理」中新建 automatic 通道 | 令牌。监听非回环地址时必须有令牌，未设置时自动生成 |
| VNC | 5900 | `127.0.0.1` | 默认启用 | `VNC_PASSWORD`，未设置时不要求密码 |

在 WebUI「通道管理」中新建的 HTTP 监听可以指定任意端口，使用全局 access_token 鉴权。

Satori 与 automatic 的默认监听地址为 `0.0.0.0`，因此默认会自动生成令牌。若只在本机使用，可将监听地址改为 `127.0.0.1`，此时可以不设置令牌。

## 网络模式

### 主机网络（推荐）

使用 `--network host` 时，服务直接绑定在宿主机端口上。框架与协议端可以通过 `127.0.0.1` 互相访问。入口脚本会把 `host.docker.internal` 指向 `127.0.0.1`。

### 桥接网络

使用默认的 bridge 网络时，需要通过 `-p` 发布端口，并注意以下差异。

- 容器内的 `127.0.0.1` 指容器自身。入口脚本会把 `host.docker.internal` 写入容器的 `/etc/hosts`，指向默认网关，即宿主机。框架运行在宿主机上时，协议端的反向 WebSocket 地址应写为 `host.docker.internal`，并且框架需要监听 `0.0.0.0`。
- 通过端口映射访问 VNC 时，需要设置 `VNC_HOST=0.0.0.0` 和 `VNC_PASSWORD`。

```bash
docker run -d --name whiskerwind \
  -p 127.0.0.1:5700:5700 \
  -p 127.0.0.1:3000:3000 \
  -p 127.0.0.1:5900:5900 \
  -e VNC_HOST=0.0.0.0 \
  -e VNC_PASSWORD=change-me \
  -e QQCACHE_WS_REVERSE_URL=ws://host.docker.internal:8080/onebot/v11/ws \
  -v "$PWD/data/QQ:/root/.config/QQ" \
  -v "$PWD/data/qqcache:/var/lib/qqcache" \
  ghcr.io/xtxiaoting14332/whiskerwind:latest
```

## 数据目录与文件

| 路径（容器内） | 宿主机目录 | 内容 |
|---|---|---|
| `/var/lib/qqcache/qqcache.sqlite3` | `data/qqcache/` | 会话缓存、消息记录与 ID 映射、通知记录 |
| `/var/lib/qqcache/qqcache.sqlite3.transports.json` | `data/qqcache/` | 运行时创建的通道，包括 OneBot 的 HTTP 监听、反向 WebSocket、HTTP POST，以及 Satori、automatic 通道。文件中包含各通道的令牌，权限为 `600` |
| `/var/lib/qqcache/qqcache.sqlite3.protocols.json.migrated` | `data/qqcache/` | 旧版本的协议配置。启动时已迁移为通道，原文件改名保留 |
| `qqcache.sqlite3.test_trigger`、`.resolve_first`、`.member_ttl` | `data/qqcache/` | 运行时设置的开关与参数 |
| `qqcache.sqlite3.selftest.jsonl` | `data/qqcache/` | 自检报告，每执行一次自检追加一行 |
| `/var/lib/qqcache/access-token`、`webui-password` | `data/qqcache/` | 自动生成的访问凭据，权限为 `600` |
| `/var/lib/qqcache/machine-id` | `data/qqcache/` | 设备标识，启动时复制到 `/etc/machine-id`。不应删除 |
| `/root/.config/QQ/Partitions/qqnt_<QQ号>` | `data/QQ/` | 账号的登录态。目录名中的数字即登录账号的 QQ 号 |
| `/root/.config/QQ/nt_qq_*/nt_data` | `data/QQ/` | QQ 自动下载的图片、语音等媒体文件 |
| `/var/log/qqcache.log`、`/var/log/webui.log` | 不挂载 | 协议端与 WebUI 后端的日志，容器删除后丢失 |

## 与其他实例共存

- 每个实例使用独立的容器名称、数据目录和端口，包括 OneBot、WebUI、VNC，以及在 WebUI 中配置的 Satori 与 automatic 端口。
- 同一 QQ 账号不应同时在两个实例中登录，否则两者会相互挤下线。
