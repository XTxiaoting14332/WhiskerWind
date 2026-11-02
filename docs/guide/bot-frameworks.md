# 接入 Bot 框架

WhiskerWind 提供 OneBot v11、Satori 与 automatic 三种接口。大多数机器人框架支持 OneBot v11，建议优先使用该协议。三种协议的功能范围不同，逐项差异见[功能差异](/compatibility)。

## 协议概览

| 协议 | 默认端口 | 启用方式 | 令牌的传递方式 |
|---|---|---|---|
| OneBot v11 | 5700 | 默认启用 | `access_token` 查询参数，或 `Authorization: Bearer` 请求头 |
| Satori | 5500 | 在 WebUI「通道管理」中新建 Satori 通道 | HTTP 接口使用 `Authorization: Bearer` 请求头；事件 WebSocket 在 IDENTIFY 帧中携带 `token` |
| automatic | 5600 | 在 WebUI「通道管理」中新建 automatic 通道 | `Authorization: Bearer` 请求头或 `token` 查询参数；WebSocket 在 hello 帧中携带 `token` |

Satori 与 automatic 监听非回环地址（默认为 `0.0.0.0`）时必须有令牌。未设置时系统会自动生成，可以在 WebUI「通道管理」中显示或复制，也可以通过 `get_transport_token` 读取。只有监听回环地址（如 `127.0.0.1`）时才可以不设置令牌。安全建议见[运行与维护](/guide/operations)。

## OneBot v11

### 连接方式

| 方式 | 地址 | 说明 |
|---|---|---|
| HTTP API | `http://<主机>:5700/<action>` | 支持 GET 与 POST 请求 |
| 正向 WebSocket | `ws://<主机>:5700/` | 与 HTTP API 共用端口 |
| 反向 WebSocket | 由协议端连接框架，框架地址形如 `ws://<框架地址>/onebot/v11/ws` | 通过 `QQCACHE_WS_REVERSE_URL` 设置（启动时创建），或在 WebUI「通道管理」中新建「反向 WebSocket」通道 |
| HTTP POST 上报 | 由协议端请求框架，地址形如 `http://<框架地址>/onebot/v11/http` | 通过 `QQCACHE_POST_URL` 设置（启动时创建），或在 WebUI「通道管理」中新建「HTTP POST 上报」通道 |

### 鉴权

OneBot 接口通过以下两种方式之一提供 `access_token`：

- 查询参数：`?access_token=<token>`
- 请求头：`Authorization: Bearer <token>`

容器中的 OneBot 接口均需要令牌，包括 HTTP API、正向 WebSocket 握手、`/media`、`/qr`、`/screen` 与管理接口。令牌缺失或不匹配时，HTTP 接口返回 HTTP 401，并附带 retcode 1403。

### HTTP API

请求参数可以放在查询字符串中，也可以放在 `application/json` 或表单格式的请求体中。同名参数以请求体为准。

`_async` 与 `_rate_limited` 后缀会被忽略，调用行为与同步调用相同。请求中的 `echo` 字段会原样返回。

发送私聊消息：

```bash
TOKEN=$(docker exec whiskerwind cat /var/lib/qqcache/access-token)

curl -s -X POST "http://127.0.0.1:5700/send_private_msg?access_token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id": 234567890, "message": "测试消息"}'
```

返回示例（字段值仅作示意）：

```json
{"status": "ok", "retcode": 0, "data": {"message_id": 1001}}
```

发送群消息，同时使用 @ 与文本消息段：

```bash
curl -s -X POST "http://127.0.0.1:5700/send_group_msg?access_token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 987654321, "message": [{"type": "at", "data": {"qq": "234567890"}}, {"type": "text", "data": {"text": " 收到"}}]}'
```

响应结构如下。

| 字段 | 说明 |
|---|---|
| `status` | `ok` 或 `failed` |
| `retcode` | 返回码，0 表示成功 |
| `data` | 接口返回的数据，失败时为 `null` |
| `message` | 失败时的错误说明 |
| `echo` | 请求中提供了 `echo` 时返回 |

返回码如下。

| retcode | 含义 | 常见原因 |
|---|---|---|
| 0 | 成功 | |
| 1400 | 参数错误或执行失败 | 缺少必需参数，消息段类型不受支持，界面操作失败。具体原因见 `message` |
| 1403 | 鉴权失败 | 令牌缺失或不匹配，管理接口未配置访问令牌 |
| 1404 | 未找到 | 接口未实现，消息 ID、群或成员不存在 |
| 1429 | 发送受限 | 触发出站限速或重复内容拦截，参见[已知问题与排查](/guide/known-issues) |

JSON 请求体无法解析时，返回 HTTP 400，retcode 为 1400。

### 正向 WebSocket

连接 `ws://<主机>:5700/` 时需要提供令牌。连接建立后，客户端发送的请求帧格式如下：

```json
{"action": "send_msg", "params": {"user_id": 234567890, "message": "hello"}, "echo": "req-1"}
```

响应帧包含 `status`、`retcode`、`data` 与 `echo` 字段。协议端推送的事件也通过同一连接发送。

### 反向 WebSocket

协议端作为 WebSocket 客户端连接框架，握手请求包含以下请求头。

| 请求头 | 值 |
|---|---|
| `X-Self-ID` | 登录账号的 QQ 号 |
| `X-Client-Role` | `Universal`，表示事件推送与 API 调用共用同一连接 |
| `Authorization` | `Bearer <token>`，设置令牌时携带 |

连接建立后，事件推送与 API 调用都通过该连接进行。连接失败或断开后，协议端每 3 秒重试一次。

通过环境变量设置的反向 WebSocket 使用全局 `access_token` 作为令牌。在 WebUI 中新建的反向 WebSocket 通道使用自己的令牌（可选），令牌可以在通道的编辑窗口中查看或修改。

### HTTP POST 上报

每个事件以一次 POST 请求发送到上报地址，请求体为事件的 JSON 内容。

| 请求头 | 说明 |
|---|---|
| `Content-Type` | `application/json` |
| `X-Self-ID` | 登录账号的 QQ 号 |
| `Authorization` | 设置令牌时为 `Bearer <token>` |
| `X-Signature` | 设置 `QQCACHE_POST_SECRET` 后出现。值为 `sha1=` 加上请求体的 HMAC-SHA1 十六进制摘要 |

签名校验的示例代码如下（Python）：

```python
import hashlib
import hmac

def verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, header)
```

请求超时时间为 5 秒。上报失败时，协议端记录警告日志，不会重试。协议端不处理响应体的内容，因此不支持通过响应体执行快速操作。

### 事件

| post_type | 说明 |
|---|---|
| `message` | 收到的群消息或私聊消息 |
| `message_sent` | 本账号发出的消息，包括在其他设备发出并同步到本机的消息（扩展事件） |
| `notice` | 扩展通知。`qqcache_member_resolved` 表示群成员的合成 ID 已解析为真实 QQ 号；`qqcache_system_notification` 为 QQ 系统通知的原文，如下线提示、入群申请 |
| `meta_event` | `heartbeat` 为心跳，包含登录状态；`lifecycle` 的 `sub_type` 为 `connect`，服务启动时发送；`login_state` 为登录状态变化 |

消息事件的字段如下。

| 字段 | 说明 |
|---|---|
| `time` | 事件时间，Unix 时间戳（秒） |
| `self_id` | 登录账号的 QQ 号 |
| `post_type` | `message` 或 `message_sent` |
| `message_type` | `group` 或 `private`。私聊一律为 `private`，不区分群临时会话 |
| `sub_type` | 群消息为 `normal`，私聊为 `friend` |
| `message_id` | 消息 ID。由协议端分配的 int32 数值，映射关系持久保存 |
| `user_id` | 发送者的 QQ 号。群成员未解析时为合成 ID（≥ 900000000000） |
| `group_id` | 群号，仅群消息包含 |
| `target_id` | 会话对象，仅 `message_sent` 包含 |
| `message` | 消息内容，CQ 码字符串 |
| `raw_message` | 消息的纯文本内容 |
| `font` | 恒为 `0` |
| `sender` | 发送者信息。`nickname` 为群名片或昵称。群消息还包含 `role`（`owner`、`admin` 或 `member`）与 `title` |
| `qqcache_msg_id` | QQ 原生消息 ID（扩展字段） |
| `qqcache_kind` | 消息类别，取值为 `text`、`image`、`voice`、`video`、`file`、`forward`、`card`、`reply` 或 `unknown`（扩展字段） |

群消息事件的示例如下。为便于阅读，字段已省略部分内容。

```json
{
  "time": 1762000000,
  "self_id": 123456789,
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal",
  "message_id": 1001,
  "user_id": 234567890,
  "group_id": 987654321,
  "message": "今天天气不错",
  "raw_message": "今天天气不错",
  "font": 0,
  "sender": {"user_id": 234567890, "nickname": "示例成员", "role": "member", "title": ""},
  "qqcache_msg_id": "<QQ 原生消息 ID>",
  "qqcache_kind": "text"
}
```

### 消息格式

**接收**时，`message` 为 CQ 码字符串，`raw_message` 为纯文本。`message` 中的 `&`、`[`、`]` 分别转义为 `&amp;`、`&#91;`、`&#93;`，CQ 码参数中的 `,` 转义为 `&#44;`。群内的 @ 以 `@显示名` 的文本形式出现，不会生成 `at` 消息段。

| `qqcache_kind` | `message` 字段的内容 |
|---|---|
| `text` | 文本内容 |
| `image` | `[CQ:image,file=<文件名>,url=file://<容器内路径>]`。未能关联到媒体文件时为 `[CQ:image]` |
| `voice` | `[CQ:record,file=file://<容器内路径>]`。未能关联到媒体文件时为 `[语音]` |
| `video` | `[CQ:video,file=file://<容器内路径>]`。未能关联到媒体文件时为 `[视频]` |
| `file` | `[文件] <文件名> (<大小>)` |
| `forward` | `[聊天记录]` 加摘要文本 |
| `card` | 卡片的摘要文本 |
| `reply` | `[CQ:reply,id=<消息 ID>]` 后接回复内容 |

**发送**时，`message` 可以是 CQ 码字符串，也可以是消息段数组。设置 `auto_escape` 为 `true` 时，整条消息按纯文本发送，不解析 CQ 码与消息段。

| 消息段 | 发送 | 说明 |
|---|---|---|
| `text` | 支持 | `data.text` 为文本内容 |
| `at` | 支持 | `data.qq` 为 QQ 号，或 `all` 表示全体成员 |
| `image` | 支持 | `data.file` 或 `data.url`，可以是本地路径、`file://`、`base64://` 或 `http(s)://` 地址 |
| `reply` | 支持 | `data.id` 为协议端返回的消息 ID，且必须是本服务记录过的消息 |
| `face`、`mface`、`bface`、`sface`、`record`、`video`、`share`、`location`、`contact`、`music`、`poke`、`dice`、`rps` | 降级为文本 | 以占位文本发送，例如 `[表情]`、`[语音]` |
| 其他类型，如 `file`、`forward`、`json`、`xml`、`node` | 不支持 | 整条消息失败，返回 retcode 1400 |

### 媒体文件

- QQ 会自动下载收到的图片与语音，并保存在 `/root/.config/QQ/nt_qq_*/nt_data` 中。协议端会把这些文件与消息关联，并在 `message` 字段中给出 `file://` 路径。
- 通过 `GET /media?path=<容器内路径>&access_token=<token>` 获取文件内容。路径必须位于 `nt_data` 目录内，否则返回 404。
- 文件与视频消息的原始内容需要在 QQ 中手动下载，协议端不会自动获取。

### 扩展接口

除 OneBot v11 标准接口外，协议端还提供 `get_login_state`、`get_system_info`、`get_msg_history`、`send_poke`、`resolve_member` 等扩展接口。管理类接口（如 `get_logs`、`list_transports`、`add_transport`、`update_transport`、`get_transport_token`、`del_transport`）需要配置访问令牌。接口列表与参数见 [WebUI 接口](/webui-api)。

### 示例：NoneBot2（反向 WebSocket）

NoneBot2 的 OneBot V11 适配器默认在 `/onebot/v11/ws` 提供反向 WebSocket 服务。

1. 启动容器时设置 `QQCACHE_WS_REVERSE_URL=ws://127.0.0.1:8080/onebot/v11/ws`，参见[快速开始](/guide/getting-started)。
2. 在 NoneBot 的 `.env` 文件中设置监听地址与令牌：

```dotenv
DRIVER=~fastapi+~websockets
HOST=127.0.0.1
PORT=8080
ONEBOT_ACCESS_TOKEN=<access-token>
```

`<access-token>` 为容器内 `access-token` 文件的内容，或 `QQCACHE_TOKEN` 设置的值。NoneBot 使用该令牌校验连接请求，两端必须一致。

若希望由框架主动连接协议端（正向连接），可在 NoneBot 中设置 `ONEBOT_WS_URLS=["ws://127.0.0.1:5700/"]`。所用驱动需要支持 WebSocket 客户端。

## Satori

Satori 默认监听 `5500` 端口。在 WebUI「通道管理」中新建 Satori 通道后生效。每条 Satori 通道都是独立的适配器实例，可以同时存在多条，各自拥有独立的事件序号与令牌。

| 项目 | 说明 |
|---|---|
| HTTP API | `http://<主机>:5500/v1/<资源>.<方法>`，请求头为 `Authorization: Bearer <token>` |
| 事件 WebSocket | `ws://<主机>:5500/v1/events`。IDENTIFY 帧携带 `token`，并可以通过 `sn` 补发断线期间的事件 |
| 登录信息 | `platform` 默认为 `qq`，可在 WebUI 中修改。请求中的 `Satori-Platform` 与之不一致时返回 404 |
| 媒体资源 | `/v1/proxy/` 路径，需要令牌 |

| 对象 | 映射方式 |
|---|---|
| 群 | 同时作为 guild 与唯一的 channel，两者的 ID 都是群号 |
| 私聊 | 频道 ID 为 `private:<QQ号>` |
| 消息 | ID 为 QQ 原生消息 ID |
| 未解析的成员 | 使用合成 ID。解析完成后，通过 `internal` 事件 `qqcache/member-resolved` 通知 |

### 示例：Koishi

1. 在 WebUI「通道管理」中新建 Satori 通道。默认监听 `0.0.0.0:5500`，令牌会自动生成，可在通道的编辑窗口中显示或复制。
2. 安装适配器：

```bash
npm i @koishijs/plugin-adapter-satori
```

3. 在 Koishi 中添加适配器实例：

```ts
import satori from '@koishijs/plugin-adapter-satori'

ctx.plugin(satori, {
  endpoint: 'http://127.0.0.1:5500',
  token: '<satori-token>',
})
```

`endpoint` 为 Satori 服务的地址，`token` 与 WebUI 中设置的令牌一致。

## automatic

automatic 是本项目定义的协议，默认监听 `5600` 端口。在 WebUI「通道管理」中新建 automatic 通道后生效。

| 项目 | 说明 |
|---|---|
| WebSocket | `ws://<主机>:5600/automatic/v1`，hello 帧携带 `token` |
| 反向连接 | 在 WebUI「通道管理」中新建「automatic 反向 WebSocket」通道，填写框架地址（只支持 `ws://`） |
| HTTP | `POST /automatic/v1/<action>`，令牌通过 `Authorization: Bearer` 请求头或 `token` 查询参数提供 |

协议的消息、事件与错误码定义见 [automatic 协议](/automatic-protocol)。

## 连接排查

| 现象 | 检查项 |
|---|---|
| HTTP 返回 401，retcode 为 1403 | 令牌是否与容器内 `access-token` 文件一致，请求中是否带有 `access_token` 参数或 `Authorization` 请求头 |
| 反向 WebSocket 状态为 `connecting`，服务日志出现「反向 WS 连接 … 失败」 | 框架是否已启动，地址、端口和路径是否正确。桥接网络下应使用 `host.docker.internal`，并让框架监听 `0.0.0.0` |
| 框架已连接，但收不到事件 | QQ 是否处于登录状态（`get_login_state`），会话是否设为免打扰（参见[已知问题与排查](/guide/known-issues)） |
| 调用超时 | 发送一条消息通常需要 1–5 秒，排队时更久。框架的超时时间建议不少于 60 秒 |

WebUI「通道管理」页会显示各通道的状态。反向 WebSocket 通道的状态为 `connecting` 或 `connected`。
