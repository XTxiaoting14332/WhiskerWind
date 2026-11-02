# automatic 协议 v1

> 版本：v1。在 WebUI「通道管理」页新建「automatic 服务端」或「automatic 反向 WebSocket」通道即可启用。

## 1. 设计目标

automatic 协议针对 OneBot v11 的以下不足而设计。对于通过界面读取消息的协议端，这些不足的影响尤为明显：

| OneBot v11 的不足 | 对本项目的影响 | automatic 的设计 |
| --- | --- | --- |
| `message_id` 为 int32，跨重启、跨实现不稳定 | 需要维护 int 与 QQ 原生 id 的映射表 | **所有 ID 都是字符串**，消息 ID 直接用 QQ 原生 msg_id |
| 存在 CQ 码字符串与消息段数组两种格式，转义规则复杂 | 收发两端都需要解析 CQ 码 | **只有 JSON 段数组**，没有字符串编码 |
| `post_type` / `notice_type` / `sub_type` 三层嵌套 | 自定义事件只能放入 `notice_type` | **扁平事件名**：`message.created`、`member.resolved` |
| `user_id` 只是一个数字，无法体现可信程度 | 未解析成员只能使用合成号，下游无法区分真实号码与合成号码 | **身份显式标注**：`resolved: false` 的 ID 带 `syn:` 前缀，不会被误认为 QQ 号 |
| 不支持断线续传 | 下游重连期间的事件会丢失 | 事件带 `seq`，重连时可**按 seq 补发** |
| 没有能力声明 | 下游只能在调用失败后得知功能不受支持 | 握手时下发 **capabilities**，调用前就知道能做什么 |
| 错误只有 retcode 数字 | 无法区分可重试与不可重试的错误 | 错误是**字符串 code + `retryable`** |
| 积压消息与实时消息无法区分 | 服务重启或补读时，历史命令会被当作新命令推送给机器人 | 消息带 **`delivery: live / backlog`** |

本协议不以覆盖 QQ 的全部功能为目标。受界面自动化能力的限制，协议端只保证 capabilities 中声明的功能。

## 2. 传输

- **正向 WebSocket**：`ws://<host>:<port>/automatic/v1`。一条连接同时收事件、发调用（全双工）。
- **反向 WebSocket**：协议端主动连下游给的 URL（连接时带 `Authorization: Bearer <token>`），之后的帧格式与正向完全相同——仍由下游先发 `hello`。
- **HTTP**：`POST /automatic/v1/<action>`，请求体是 `params`，响应体是 `result`。只用于调用，不推事件。
- 鉴权：WS 在 `hello` 帧里带 `token`；HTTP 用 `Authorization: Bearer <token>`。
- 编码：UTF-8 JSON，时间戳一律**毫秒**。

v1 不提供 WebHook 方式的事件推送（见第 10 节）。

媒体：收到的图片/语音/视频段里 `url` 是**相对地址** `/automatic/v1/media/<path>`，相对于协议端的 HTTP 地址解析，鉴权同 HTTP 调用（Bearer 头或 `?token=`）。

## 3. 帧

所有 WS 帧都是 `{"type": ..., ...}`。

### 3.1 握手

```jsonc
// 客户端 → 协议端（连接后 10s 内）
{"type": "hello", "protocol": "automatic/1", "token": "…",
 "resume": {"session": "s_8f2c…", "seq": 1042}}   // 可选，断线续传
// 协议端 → 客户端
{"type": "ready", "session": "s_8f2c…", "seq": 1042, "resumed": true,
 "self": {"id": "1234567890", "name": "【示例】BotName", "avatar": "https://…"},
 "login": "online",                       // online | offline | login_required
 "capabilities": ["message.send", "message.send.image", "message.send.mention", "message.send.quote",
                  "message.recall", "message.history", "member.resolve", "member.mute", "poke"],
 "limits": {"replay_window": 1000}}
```

- `session` 在协议端进程生命周期内不变；进程重启后旧 session 失效，`resumed: false`，客户端应自行对账。
- `resumed: true` 时，协议端紧接着按顺序补发 `seq` 之后的事件，再转入实时推送。

### 3.2 事件

```jsonc
{"type": "event", "seq": 1043, "event": {"type": "message.created", "time": 1793539038981, …}}
```

`seq` 在 session 内严格递增；客户端以 `event.message.id` 去重（续传可能重放边界上的一条）。

### 3.3 调用

```jsonc
// 客户端 → 协议端
{"type": "call", "id": "c17", "action": "message.send", "params": {…}}
// 协议端 → 客户端（与请求同 id，可乱序返回）
{"type": "result", "id": "c17", "ok": true, "data": {…}}
{"type": "result", "id": "c17", "ok": false,
 "error": {"code": "target_unverified", "message": "资料卡验号失败，拒绝发送", "retryable": true}}
```

### 3.4 心跳

用 WebSocket 原生 ping/pong。协议端额外每 30s 推一条 `status.heartbeat` 事件（带登录态），不占 `seq`。

## 4. 实体

### Conversation

```jsonc
{"id": "group:100000002", "kind": "group", "name": "Linux&开源技术交流群"}
{"id": "private:2345678901", "kind": "private", "name": "【示例】Alice"}
```

ID 带有类型前缀，群号与 QQ 号不会冲突。

### User

```jsonc
{"id": "2345678901", "resolved": true,  "name": "【示例】Alice"}
{"id": "syn:900000000003", "resolved": false, "name": "Neko_XZJ"}
```

- `resolved: false` 表示尚未从界面解析出真实 QQ 号。`syn:` ID 在同一群、同一昵称下保持稳定，可作为临时主键。
- 解析成功后推送 `member.resolved`，下游据此合并两个身份。

未解析成员的消息是立即推送（携带 syn ID），还是等待解析完成后再推送，由**协议端全局设置**决定（`set_resolve_first` 接口，与 OneBot v11 共用），当前设置通过 `ready.options.wait_resolve` 告知客户端。两种方式下，解析成功后都会推送 `member.resolved`。

### Message

```jsonc
{
  "id": "7703189013652762603",           // QQ 原生 msg_id；本账号发出但未获取到回显时为 "local:<n>"
  "conversation": {"id": "group:100000001", "kind": "group", "name": "files"},
  "sender": {"id": "2345678901", "resolved": true, "name": "【示例】Alice"},
  "role": "member",                        // owner | admin | member（仅群）
  "self": false,                           // 是不是本账号发的
  "time": 1793538534707,
  "segments": [{"type": "text", "text": "#ww"}],
  "quote": {"id": "7703186345878813055"},  // 可选
  "delivery": "live"                       // live | backlog
}
```

`delivery: backlog` 表示该消息并非实时到达，而是服务重启后补读或漏读后回填得到的。**机器人应默认忽略 backlog 消息中的命令。**

## 5. 消息段

消息段为扁平 JSON 对象，不使用 `data` 字段包裹：

| type | 字段 | 收 | 发 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | `text` | ✓ | ✓ | |
| `mention` | `user_id` 或 `all: true` | ✓ | ✓ | 接收时界面只提供显示名，`user_id` 可能缺失，仅有 `name` |
| `image` | `url` / `file` / `base64`，`name?` | ✓ | ✓ | 接收时 `url` 指向协议端的媒体路由 |
| `voice` / `video` | `url?` | ✓ | — | 暂不支持发送 |
| `file` | `name`, `size?` | ✓ | — | |
| `face` | `id?`, `text` | ✓ | 降级 | 发送时降级成 `text` |
| `forward` | `text` | ✓ | — | 合并转发，仅提供摘要文本 |
| `card` | `text` | ✓ | — | 卡片或小程序，仅提供摘要文本 |
| `unknown` | `text` | ✓ | — | 无法识别的消息形式，以文本提供 |

发送的消息包含不支持的段时，整条消息被拒绝并返回 `unsupported_segment`，不会静默丢弃。

## 6. 事件

| type | 主要字段 | 说明 |
| --- | --- | --- |
| `message.created` | `message` | 收到或发出一条消息 |
| `member.resolved` | `conversation`, `name`, `from` (syn ID), `to` (真号) | 合成身份升级为真号 |
| `login.changed` | `prev`, `state` | 登录态变化（掉线、需要扫码、恢复登录） |
| `status.heartbeat` | `login` | 不占用 seq |
| `notification.system` | `title`, `text` | QQ 系统通知原文（加群申请、好友申请等），只读，无法处理 |

`message.recalled` 已预留，v1 不推送（界面上无法可靠检测撤回）。

## 7. 动作

| action | params | 返回 |
| --- | --- | --- |
| `self.get` | — | `self`, `login` |
| `message.send` | `conversation`, `segments`, `quote?` | `message` |
| `message.recall` | `id`, `conversation` | — |
| `message.get` | `id`, `conversation` | `message` |
| `message.history` | `conversation`, `before?`, `limit?` | `messages`, `next?` — **仅包含协议端运行期间经过的消息** |
| `conversation.list` | `kind?` | `conversations` |
| `conversation.get` | `id` | `conversation` |
| `member.list` | `conversation` | `members` — 仅包含发过言的成员 |
| `member.get` | `conversation`, `user_id` | `member` |
| `member.resolve` | `conversation`, `user_id`（syn ID 或昵称） | `user`（resolved） — 通过界面实时解析，约 3–10 秒 |
| `member.mute` | `conversation`, `user_id`, `duration_ms` | — |
| `poke` | `conversation`, `user_id` | — |

## 8. 错误码

| code | retryable | 含义 |
| --- | --- | --- |
| `unauthorized` | false | 令牌错误 |
| `unsupported` | false | 动作不在 capabilities 里 |
| `unsupported_segment` | false | 消息含不支持的段 |
| `bad_params` | false | 参数缺失或格式错误 |
| `not_found` | false | 会话、消息或成员不存在（或协议端未曾见过） |
| `not_logged_in` | true | QQ 未登录 |
| `target_unverified` | true | 打开的会话没能验证是目标，**未发送** |
| `gui_busy` | true | 界面操作排队超时，**未执行** |
| `rate_limited` | true | 触发出站限速或重复内容拦截，**未发送** |
| `gui_failed` | 视情况 | 界面操作失败；`message` 中说明是否可能已部分执行 |

约定：**凡 `retryable: true` 的错误，协议端保证该操作未产生副作用**（例如消息确未发出），客户端可以安全重试。

## 9. 实现说明

- automatic 与 OneBot v11、Satori 同为核心层（`qqcache/core.py`）之上的协议适配器，共享消息读取、身份解析与发送逻辑。
- 在 WebUI「通道管理」页管理：「automatic 服务端」通道提供正向 WebSocket 与 HTTP 调用，「automatic 反向 WebSocket」通道主动连接指定地址。每条通道有独立的令牌和事件序号，可以同时存在多条，修改即时生效。监听非回环地址的服务端必须设置令牌。
- 断线续传窗口默认为 1000 条事件。协议端重启后 `session` 改变，旧 session 的续传请求返回 `resumed: false`。
- `status.heartbeat` 至多每 30 秒推送一次，不占用 `seq`，也不进入续传窗口。
- `message.history` 与 `message.get` 返回的消息均为 `delivery: backlog`。
- 发送 `mention` 只接受真实 QQ 号；`syn:` ID 需先通过 `member.resolve` 解析。

## 10. 设计取舍

1. **事件推送方式**：v1 仅通过正向与反向 WebSocket 推送事件，不提供 WebHook。两种 WebSocket 方式已能满足常见的部署需求，WebHook 可在后续版本中按需增加。
2. **未解析成员的推送时机**：由协议端全局设置决定，并通过 `ready.options.wait_resolve` 告知客户端，而不由各客户端分别选择。若允许逐客户端选择，同一条消息需要在不同时刻推送给不同客户端，会显著增加核心层的复杂度。
3. **系统通知**：提供 `notification.system` 事件。系统通知打开的不是聊天页面时，协议端会切回消息页面，并将通知原文透传给客户端。
4. **协议标识**：`automatic/1`。
