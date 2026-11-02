# 与标准协议的差异

WhiskerWind 通过操作 QQ 客户端界面来实现协议功能（见 [architecture.md](architecture.md)），因此只能提供界面上能够完成、且能够可靠完成的功能。本文列出 OneBot v11 与 Satori 两个协议中各项功能的支持情况，以及与规范行为不一致之处。

图例：✅ 支持　⚠️ 部分支持或行为有差异　❌ 不支持

## OneBot v11

### 通信方式

| 方式 | 状态 | 说明 |
|---|---|---|
| HTTP API | ✅ | `POST /<action>`，也接受 GET 与 query 参数；支持 `_async`、`_rate_limited` 后缀，行为与同步调用相同 |
| 正向 WebSocket | ✅ | 与 HTTP 共用端口，路径 `/`，同一连接收发 API 与事件 |
| 反向 WebSocket | ✅ | Universal 模式（单连接） |
| HTTP POST 上报 | ⚠️ | 支持上报与 `X-Signature` 签名；不支持快速操作（响应体中的回复指令会被忽略） |
| 鉴权 | ✅ | `access_token` 查询参数或 `Authorization: Bearer` 头 |

### API

| API | 状态 | 说明 |
|---|---|---|
| `send_private_msg` / `send_group_msg` / `send_msg` | ✅ | 每条约 1–5 秒，所有发送串行执行；受出站限速约束，超限返回 `retcode 1429` |
| `delete_msg` | ✅ | 撤回自己的消息；撤回他人消息需要管理员权限 |
| `get_msg` | ⚠️ | 仅限服务运行期间经过的消息 |
| `get_login_info` | ✅ | |
| `get_status` / `get_version_info` | ✅ | |
| `get_friend_list` | ⚠️ | 只包含服务运行期间出现过的私聊对象，不是完整好友列表 |
| `get_group_list` / `get_group_info` | ⚠️ | 只包含服务运行期间出现过的群；`member_count` 仅在群名带有人数时可得，`max_member_count` 固定为 0 |
| `get_group_member_list` | ⚠️ | 只包含服务运行期间发过言的成员 |
| `get_group_member_info` | ⚠️ | 同上；传入合成 id 时会现场解析真实 QQ 号 |
| `set_group_ban` | ⚠️ | 支持；时长按 QQ 提供的档位或「自定义时长」设置，精度为分钟 |
| `can_send_image` | ✅ | 返回 `true` |
| `can_send_record` | ✅ | 返回 `false` |
| `get_forward_msg` | ❌ | |
| `send_like` | ❌ | |
| `set_group_kick` / `set_group_whole_ban` / `set_group_admin` / `set_group_card` / `set_group_name` / `set_group_leave` / `set_group_special_title` | ❌ | |
| `set_group_anonymous` / `set_group_anonymous_ban` | ❌ | |
| `set_friend_add_request` / `set_group_add_request` | ❌ | 加好友、加群申请只能以系统通知事件的形式收到，无法处理 |
| `get_stranger_info` | ❌ | |
| `get_image` / `get_record` | ❌ | 收到的媒体经 `GET /media?path=` 获取 |
| `get_cookies` / `get_csrf_token` / `get_credentials` | ❌ | |
| `get_group_honor_info` / `set_restart` / `clean_cache` | ❌ | |

未支持的 API 统一返回 `retcode 1404`。

扩展 API（非 OneBot 标准）：`send_poke`、`get_msg_history`、`get_login_state`、`get_system_info`、`resolve_member` 等，详见 [webui-api.md](webui-api.md)。

### 事件

| 事件 | 状态 | 说明 |
|---|---|---|
| `message.private.friend` | ✅ | 私聊一律为 `friend`，不区分群临时会话 |
| `message.group.normal` | ✅ | |
| `message.group.anonymous` | ❌ | |
| `message_sent` | ✅ | 扩展事件：本账号发出的消息（包括手机端发出、被同步到客户端的消息） |
| `notice` 标准通知（入群、退群、禁言、撤回、管理员变动、文件上传、戳一戳、好友添加等） | ❌ | 界面上没有可靠的检测途径 |
| `notice.qqcache_member_resolved` | ✅ | 扩展：群成员的合成 id 解析为真实 QQ 号 |
| `notice.qqcache_system_notification` | ✅ | 扩展：QQ 系统通知原文（下线通知、加群申请、好友申请等），只读 |
| `request` | ❌ | |
| `meta_event.lifecycle` | ⚠️ | 只有 `connect` |
| `meta_event.heartbeat` | ✅ | |
| `meta_event.login_state` | ✅ | 扩展：登录态变化 |

### 事件字段

- **`user_id`**：群成员的真实 QQ 号通过界面解析获得。解析完成前为合成 id（≥ 900000000000），解析成功后推送 `qqcache_member_resolved` 通知。
- **`message_id`**：int32，由协议端映射到 QQ 原生消息 id，映射关系持久保存。事件中额外附带 `qqcache_msg_id`（原生 id）。
- **`message`**：上报格式固定为 CQ 码字符串，不支持配置为消息段数组。`raw_message` 为消息的纯文本内容。
- **`sender`**：`nickname` 为显示名（群名片优先），`role` 与 `title` 来自界面；`sex`、`age`、`area`、`level` 无法获取，为默认值。
- **`font`**：固定为 `0`。

### 消息段

| 段 | 接收 | 发送 | 说明 |
|---|---|---|---|
| `text` | ✅ | ✅ | |
| `at` | ⚠️ | ✅ | 接收时 @ 以 `@显示名` 文本形式出现在 `text` 中，不生成 `at` 段；发送支持 QQ 号与 `all` |
| `image` | ✅ | ✅ | 接收时 `url` 为容器内路径，需经 `/media` 获取；发送支持本地路径、`file://`、`base64://`、`http(s)://` |
| `reply` | ✅ | ✅ | |
| `record` / `video` | ⚠️ | ⚠️ | 接收时提供文件路径；发送时转为文本 `[语音]` / `[视频]` |
| `face` / `mface` | ⚠️ | ⚠️ | 均转为文本 `[表情]` |
| `file` | ⚠️ | ❌ | 接收时转为文本「[文件] 文件名 (大小)」 |
| `forward` | ⚠️ | ❌ | 接收时转为「[聊天记录]」加摘要文本；发送时整条请求失败 |
| `json` / `xml` / `node` | ⚠️ | ❌ | 接收时转为卡片摘要文本；发送时整条请求失败 |
| `poke` / `dice` / `rps` / `music` / `location` / `contact` / `share` | ⚠️ | ⚠️ | 均转为占位文本 |
| `anonymous` | ❌ | ❌ | |

## Satori

### 通信方式

| 方式 | 状态 | 说明 |
|---|---|---|
| HTTP API `POST /v1/{resource}.{method}` | ✅ | |
| 事件 WebSocket `/v1/events` | ✅ | 支持 `IDENTIFY` 携带 `sn` 补发断线期间的事件 |
| WebHook | ✅ | `meta/webhook.create` / `meta/webhook.delete` |
| `meta` | ✅ | |
| 资源代理 `/v1/proxy/` | ✅ | 用于获取收到的图片等媒体 |

### 对象映射

- **login**：只有一个，即当前登录的 QQ 账号。
- **guild / channel**：一个群同时是一个 guild 和它唯一的 channel，两者 id 都是群号；私聊为 `private:<QQ号>` 频道。
- **message**：id 为 QQ 原生消息 id。
- **user**：未解析成员的 id 为合成 id，解析成功后推送 `internal` 事件 `qqcache/member-resolved`。

### API

| API | 状态 | 说明 |
|---|---|---|
| `login.get` | ✅ | |
| `message.create` | ✅ | 内容按下文元素规则转换；`<message>` 元素拆分为多条发送 |
| `message.get` / `message.delete` | ⚠️ | 仅限服务运行期间经过的消息 |
| `message.list` | ⚠️ | 只支持 `direction=before`，只包含服务运行期间的记录 |
| `guild.get` / `guild.list` / `channel.get` / `channel.list` | ⚠️ | 只包含服务运行期间出现过的群 |
| `guild.member.get` / `guild.member.list` | ⚠️ | 只包含发过言的成员 |
| `guild.member.mute` | ✅ | |
| `user.get` / `user.channel.create` / `friend.list` | ⚠️ | `friend.list` 只包含出现过的私聊对象 |
| `upload.create` | ✅ | |
| `message.update`、`channel.create/update/delete/mute`、`reaction.*`、`guild.role.*`、`guild.member.role.*` | ❌ | QQ 不具备这些功能，按规范返回 404 |
| `guild.member.kick`、`guild.member.approve`、`guild.approve`、`friend.approve`、`friend.delete` | ❌ | QQ 具备但尚未实现，返回 501 |

### 事件

| 事件 | 状态 | 说明 |
|---|---|---|
| `message-created` | ✅ | |
| `login-updated` | ✅ | 登录态变化时推送 |
| `internal` | ✅ | `qqcache/member-resolved`：成员身份更新 |
| `message-deleted` / `message-updated` | ❌ | |
| `guild-*` / `guild-member-*` / `guild-role-*` | ❌ | |
| `friend-request` / `guild-request` / `guild-member-request` | ❌ | |
| `reaction-*` / `interaction/*` | ❌ | |

### 消息元素

| 元素 | 接收 | 发送 | 说明 |
|---|---|---|---|
| 纯文本 | ✅ | ✅ | |
| `<at>` | ⚠️ | ✅ | 接收时 @ 为文本；发送支持 `id` 与 `type="all"`，只有 `name` 时按文本 `@名字` 发送 |
| `<sharp>` | — | ⚠️ | 按文本 `#名称` 发送 |
| `<a>` | — | ⚠️ | 按文本发送，链接附在括号内 |
| `<img>` | ✅ | ✅ | `src` 支持 `http(s)://`、`data:`（base64）、`internal:` |
| `<audio>` / `<video>` / `<file>` | ⚠️ | ⚠️ | 接收时提供资源地址；发送时转为文本占位 |
| `<quote>` | ✅ | ✅ | 发送时只取 `id`，子元素忽略 |
| `<br>` / `<p>` | — | ✅ | 转为换行 |
| `<message>` | — | ⚠️ | 每个 `<message>` 作为一条单独的消息发送；不支持合并转发 |
| `<b>` `<i>` `<u>` `<s>` `<spl>` `<code>` `<sup>` `<sub>` 等修饰元素 | — | ⚠️ | 只保留文本内容 |
| `<button>` / `<author>` | — | ❌ | 忽略 |

## 共同的限制

以下限制源于实现方式，对所有协议都成立：

- **历史范围**：消息、会话与成员数据只来自服务运行期间经过的内容，无法获取 QQ 的完整历史记录、完整好友列表或完整群成员列表。
- **免打扰会话**：设置为免打扰或「接收不提醒」的会话不会产生事件。
- **串行与延迟**：所有界面操作串行执行，发送一条消息约 1–5 秒，调用超时建议不少于 60 秒。
- **出站限速**：同一会话 10 秒内最多 3 条、60 秒内最多 12 条，全局 60 秒内最多 20 条，30 秒内重复内容会被拒绝。
- **撤回与系统提示**：灰条提示（如撤回提示）不产生事件。
- **客户端版本**：界面结构随 QQ 版本变化，仅在镜像固定的 QQ 3.2.33 上验证。
