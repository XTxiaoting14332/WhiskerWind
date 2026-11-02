# 本地开发

本页面向需要修改代码、调试或扩展协议端的开发者。

## 仓库结构

```text
docker/              镜像构建文件、入口脚本、冒烟测试与开发启动脚本
qqcache/             协议端（Python）
tests/               单元测试（unittest）
webui/server/        WebUI 后端（Node.js，BFF）
webui/public/        WebUI 前端页面
docs/                文档站点（VitePress）与设计文档
img/                 项目图标与标识
.github/workflows/   持续集成配置
```

## qqcache 模块

| 模块 | 职责 |
|---|---|
| `__main__.py` | 命令行入口，包括 `serve`、`send`、`poll`、`notify`、`selftest` 等子命令 |
| `serve.py` | 运行时核心（`Bridge`）：界面操作锁、通知处理、会话轮询、发送流程、OneBot 动作表（`ACTIONS`）与看门狗 |
| `core.py` | 内部事件模型（消息、成员解析、登录状态变化、心跳等）与协议分发 |
| `proto_onebot.py` | 将内部事件转换为 OneBot v11 事件 |
| `ob11.py` | OneBot v11 的线格式：CQ 码、消息段、ID 映射（`IdMap`）、HTTP 与 WebSocket 服务端、反向 WebSocket 客户端、HTTP POST 上报 |
| `satori.py`、`automatic.py` | Satori 与 automatic 协议的服务端实现，由 `transports.py` 按通道创建并运行 |
| `transports.py` | 通道管理：所有对外通道（OneBot 的 HTTP 监听、反向 WebSocket、HTTP POST，以及 Satori、automatic 服务端与 automatic 反向 WebSocket）的创建、修改、删除、持久化与旧配置迁移 |
| `notify.py` | D-Bus 通知服务（`org.freedesktop.Notifications`）与通知记录 |
| `sync.py` | 读取会话列表，更新会话缓存 |
| `sweep.py` | 未读巡检，补处理通知丢失的会话 |
| `backfill.py` | 消息列表上滚回填，处理消息缺口 |
| `messages.py` | 将无障碍树中的消息节点解析为消息对象 |
| `peer.py` | 通过资料卡解析 QQ 号与群成员身份 |
| `send.py` | 发送流程：打开会话、核验目标、输入内容、确认回显 |
| `admin.py` | 撤回、戳一戳、禁言等右键菜单操作 |
| `login.py` | 登录状态判断、二维码与屏幕截图 |
| `media.py` | 将消息与 QQ 下载到 `nt_data` 中的媒体文件关联 |
| `store.py` | 会话缓存，即会话名称与 QQ 号的对应关系 |
| `cursor.py` | 已见消息游标，用于去重与检测缺口 |
| `ratelimit.py` | 出站限速与重复内容拦截 |
| `selftest.py` | 分阶段链路自检 |
| `atspi.py` | 无障碍接口的封装：查找节点、导出控件树、调用动作 |
| `clipboard.py` | 剪贴板读写，兼容 Wayland 与 X11 |
| `ws11.py` | WebSocket 握手与帧处理 |
| `logbuf.py` | 内存中的日志缓冲，供 WebUI「服务日志」页使用 |
| `sysinfo.py` | 仪表盘所需的账号、版本与资源信息 |

## 测试与静态检查

```bash
python3 -m unittest discover -s tests

shellcheck -S warning docker/*.sh
find webui/server webui/public -name '*.js' -print0 | xargs -0 -n1 node --check
```

- 单元测试需要 `loguru` 与 PyGObject（`gi`），并需要 AT-SPI 的 GIR 数据。在 Debian 或 Ubuntu 上，相关包为 `python3-gi`、`gir1.2-atspi-2.0` 与 `python3-loguru`。持续集成使用的依赖见 `.github/workflows/ci.yml`。
- 入口脚本只在容器启动时运行，单元测试无法覆盖，因此需要冒烟测试。

## 构建镜像与冒烟测试

```bash
docker build -f docker/Dockerfile -t whiskerwind:dev .
QQ_IMAGE=whiskerwind:dev docker/smoke.sh
```

- 构建时会从官方地址下载 QQ 安装包，并校验 SHA-256。`docker/Dockerfile` 中的 `QQ_VERSION`、`QQ_URL` 与 `QQ_SHA256` 需要同时修改。
- `smoke.sh` 启动一个一次性容器，使用独立的数据目录与容器名称，走 bridge 网络，不占用宿主机端口。脚本检查各组件是否正常启动，并确认容器在启动 15 秒后仍在运行。整个过程约需 1–2 分钟。

## 开发模式

`docker/up.sh` 启动完整的容器，并将本地代码挂载到容器中：

```bash
QQ_IMAGE=whiskerwind:dev docker/up.sh
```

- 挂载的目录包括 `qqcache/`、`webui/server/`、`webui/public/`，以及只读的 `docker/`。修改代码后重启容器即可生效，无需重新构建镜像。入口脚本同样通过挂载的 `docker/` 目录运行。
- 容器不存在时，脚本创建容器；已存在时，脚本启动该容器。
- 可用的环境变量如下。`QQ_CONTAINER` 为容器名称，默认 `whiskerwind`；`QQ_DATA` 为数据目录，默认 `$HOME/data`；`QQ_NETWORK` 为网络模式，默认 `host`；`QQ_PORTS` 为桥接网络下的端口发布参数；`QQ_RESTART` 为重启策略；`NODE_BIN` 用于旧镜像缺少 Node.js 时，从宿主机挂载 Node.js 可执行文件。
- 开发模式默认开启测试触发器，即 `QQCACHE_TEST_TRIGGER` 默认为 `1`。

## WebUI 后端

```bash
cd webui
npm install
cp .env.example .env    # 填写 WEBUI_PASSWORD 与 QQCACHE_TOKEN
npm run dev            # 监听文件变化并自动重启
```

- 未设置 `WEBUI_PASSWORD` 时，后端拒绝启动。
- `QQCACHE_UPSTREAM` 指向上游的 OneBot 接口，默认为 `http://127.0.0.1:5700`。
- `.env` 文件包含本地凭据，已在 `.gitignore` 中排除，不应提交。
- 建议使用 Node.js 22，与镜像和持续集成保持一致。

## 文档站点

```bash
npm install
npm run docs:dev       # 本地预览，支持热更新
npm run docs:build     # 构建，产物位于 docs/.vitepress/dist
npm run docs:preview   # 预览构建产物
```

- 站点配置位于 `docs/.vitepress/config.mts`，页面源文件位于 `docs/`。
- 站点图标直接引用 `img/` 目录（`vite.publicDir`）。替换 `img/icons/` 中的文件，即可更新站点图标。

## 扩展协议端

- **新增 OneBot 接口**：在 `qqcache/serve.py` 的 `Bridge` 中实现 `act_<名称>` 方法，并加入 `ACTIONS` 表。需要令牌的管理接口还应加入 `ADMIN_ACTIONS`。
- **新增通道类型**：参照 `qqcache/satori.py` 与 `qqcache/automatic.py` 实现服务端，并在 `qqcache/transports.py` 的 `TYPES`、`FIELDS`、`TransportManager._start` 与 `_stop` 中注册。需要独立令牌的类型还应加入 `TOKEN_TYPES`。
- **修改入口脚本**：修改 `docker/entrypoint.sh` 后，使用 `docker/up.sh` 重启容器即可生效。发布的镜像需要重新构建。

提交前，请运行上述测试与静态检查。涉及启动流程的改动，还应执行冒烟测试。
