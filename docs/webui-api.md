# WhiskerWind WebUI 前后端通讯接口文档

本文档分两层：

- **Part A — 上游契约**：`qqcache serve`（WhiskerWind 后端）对外暴露的 OneBot v11 接口，WebUI 后端（BFF）消费它
- **Part B — 前端契约**：建议的 WebUI 前端 ↔ BFF 接口（REST + WS），实现方可调整

---

## Part A — WhiskerWind (qqcache serve) 上游契约

### A.0 传输方式总览

启动示例：

```bash
python3 -m qqcache serve \
  --db /var/lib/qqcache/qqcache.sqlite3 \
  --host 0.0.0.0 --port 5700 \
  --post-url http://bff-host:9000/ob11/events \
  --ws-reverse-url ws://bff-host:9001/onebot/v11/ws \
  --self-id 123456789 --self-name "bot昵称" \
  --access-token secrettoken
```

| 通道 | 方向 | 说明 |
|---|---|---|
| HTTP API | BFF → qqcache | `POST http://host:5700/<action>`，JSON body |
| 正向 WS | BFF → qqcache | `ws://host:5700/`（同端口，GET Upgrade），API 请求 + 事件推送 |
| HTTP POST 上报 | qqcache → BFF | 每个事件 POST JSON 到 `--post-url` |
| 反向 WS | qqcache → BFF | qqcache 主动连 `--ws-reverse-url`，同一连接双向 |
| 媒体流 | BFF → qqcache | `GET http://host:5700/media?path=...` |
| 屏幕截图 | BFF → qqcache | `GET http://host:5700/screen` → `image/png` |
| 登录二维码 | BFF → qqcache | `GET http://host:5700/qr` → `image/png`（仅 `login_required` 状态，否则 404） |
| 日志流 | BFF → qqcache | `ws://host:5700/logs/stream`（GET Upgrade），回放后实时推日志，见 A.5 |

鉴权：设置 `--access-token` 后，HTTP/WS 请求需带 `?access_token=` 或 `Authorization: Bearer <token>`；事件 POST 带 `Authorization` 头，配了 `--post-secret` 时带 `X-Signature: sha1=<hmac-sha1(body)>`。

**管理面强制鉴权**：`get_logs` / `list_transports` / `add_transport` / `update_transport` / `get_transport_token` / `del_transport` 等管理 action 和 `/logs/stream` 端点，在 `--access-token` **未配置时一律拒绝**（1403/403）——日志和通道控制是敏感面，不允许裸奔。部署 WebUI 必须配 token。

### A.1 HTTP API

**请求**：`POST /{action}`，`Content-Type: application/json`，body 为参数对象。支持 `X_async` / `X_rate_limited` 后缀（与同步版行为一致）。GET + query 也可。

**响应**：

```json
{ "status": "ok", "retcode": 0, "data": { ... }, "echo": "透传" }
{ "status": "failed", "retcode": 1400|1403|1404, "message": "原因", "data": null }
```

retcode 约定：`0` 成功；`1400` 参数错误/动作执行失败；`1403` 鉴权失败；`1404` 未实现的 action 或资源不存在。

#### 已实现 action

| action | 参数 | data | 备注 |
|---|---|---|---|
| `send_msg` | `user_id` 或 `group_id`、`message`、`auto_escape?` | `{message_id}` | 按 id 推断私聊/群聊 |
| `send_private_msg` | `user_id`、`message`、`auto_escape?` | `{message_id}` | |
| `send_group_msg` | `group_id`、`message`、`auto_escape?` | `{message_id}` | |
| `delete_msg` | `message_id` | `{}` | GUI 右键菜单 → 撤回；无权撤回（他人消息且非管理员）→ 1400 |
| `send_poke` ⚠ | `group_id`、`user_id` | `{}` | go-cqhttp 式戳一戳：右键成员头像菜单，无双击兜底 |
| `set_group_ban` | `group_id`、`user_id`、`duration`（秒） | `{}` | 右键头像 →「设置群内禁言」子菜单：600/3600/43200/86400s 直选档位（10分钟/1小时/12小时/1天），其他值走「自定义时长」天/时/分三下拉（秒四舍五入到分，<30s 保底 1 分钟）；`duration≤0` 点「解除禁言」+确认 |
| `get_login_info` | — | `{user_id, nickname}` | self_id/self_name |
| `get_friend_list` | — | `[{user_id, nickname, remark}]` | 仅含已归因的 c2c 会话 |
| `get_group_list` | — | `[{group_id, group_name, member_count, max_member_count}]` | 已缓存的群 |
| `get_group_info` | `group_id` | `{group_id, group_name, member_count, max_member_count}` | |
| `get_group_member_list` | `group_id` | `[成员对象]` | **只含服务期内发过言的成员** |
| `get_group_member_info` | `group_id`、`user_id` | `成员对象` | 未出现过的成员 → 1404 |
| `get_msg` | `message_id` | 消息对象 | |
| `get_msg_history` ⚠ | `user_id` 或 `group_id`、`message_id?`、`count?(≤100)` | `{messages: [消息对象...]}` | **自定义扩展**，见 A.4 |
| `get_status` | — | `{online, good, app_good, ...}` | online=QQ 在无障碍树中 |
| `get_login_state` ⚠ | — | `{state, qr_expired, self_id, self_name}` | `state`: `logged_in`/`login_required`/`unknown`/`not_running`；`qr_expired` 标记二维码遮罩是否过期 |
| `get_system_info` ⚠ | — | `{self_id, self_name, avatar_url, login_state, versions{qqcache,onebot,qq}, system{platform,uptime_s,cpu_percent,mem{total,used,percent},disk{path,total,used,percent}}, stats{conversations,aliases,seen_messages,transports}}` | **自定义**：主页仪表盘一揽子数据。`self_name`/`avatar_url` 来自 `auth/login.enc`（明文 JSON：uin/nickName/faceUrl），faceUrl 缺失时回落 qlogo 公共头像；`system` 是**宿主机**全局值（/proc 非 cgroup 隔离），`cpu_percent` 有 ~0.15s 采样窗口 |
| `get_version_info` | — | `{app_name:"qqcache", app_version, protocol_version:"v11"}` | |
| `can_send_image` | — | `{yes: true}` | |
| `can_send_record` | — | `{yes: false}` | |
| `get_logs` ⚠🔒 | `after?`、`tail?(≤1000)`、`level?` | `{logs: [日志记录...], latest_seq}` | 见 A.5 |
| `list_transports` ⚠🔒 | — | `{transports: [传输对象...]}` | 见 A.5 |
| `add_transport` ⚠🔒 | `type` + 各类型参数 | `{id, transports}` | 见 A.5 |
| `del_transport` ⚠🔒 | `id` | `{transports}` | 仅可删 `source=runtime` |
| `update_transport` ⚠🔒 | `id` + 要修改的字段 | `{id, transports}` | 仅可改 `source=runtime` |
| `get_transport_token` ⚠🔒 | `id` | `{token}` | 读取通道的独立令牌 |

成员对象：`{group_id, user_id, nickname, card, sex, age, area, join_time, last_sent_time, level, role, title, ...}`

⚠ = 非标准 OneBot action（本服务扩展）；🔒 = 管理 action，必须配置 `--access-token` 才可用。

未列出的 action（`send_like`、`set_group_kick`、`set_group_admin`、`set_group_card`、`get_stranger_info` 等）返回 `retcode 1404`。

管理类 action（`delete_msg`/`send_poke`/`set_group_ban`）前置要求 `login_state == logged_in`，未登录直接 1400 不碰 GUI；执行路径与 `send_msg` 同一 `gui_lock` 串行，调用方超时建议 ≥60s。QQ 窗口唤回失败（托盘/最小化态 `windowmap` 无效）前置报 1400「QQ 主窗口无法唤回」；GUI 操作后窗口留 20s 窗口期再收，避免连续操作被收窗打断。

#### 出站 message 格式（BFF → qqcache）

`message` 字段收 **CQ 字符串**或**段数组**，支持：

| 段 | CQ 码 | 数组形式 | 说明 |
|---|---|---|---|
| 文本 | 原文 | `{type:"text",data:{text}}` | `&` `[` `]` `,` 需转义（`&amp;` `&#91;` `&#93;` `&#44;`） |
| @成员 | `[CQ:at,qq=UIN]` | `{type:"at",data:{qq}}` | `qq=数字uin` 或 `qq=all`（全体） |
| @全体 | `[CQ:at,qq=all]` | 同上 | 需要 bot 有权限，否则失败 |
| 图片 | `[CQ:image,file=...]` | `{type:"image",data:{file}}` | file 支持：本地路径、`file://`、`base64://`、`http(s)://` |
| 引用 | `[CQ:reply,id=N]` | `{type:"reply",data:{id}}` | id 是 ob11 message_id（事件里的那个 int） |
| 表情等 | `[CQ:face,...]` | `{type:"face",...}` | 降级为占位文本 `[表情]`，见下 |

降级段（不报错，转占位文本）：`face/mface/bface/sface → [表情]`、`record → [语音]`、`video → [视频]`、`share → [链接分享]`、`location/contact/music/poke/dice/rps` 同理。

拒绝段（整个请求 1400 失败）：`forward`、`json`、`xml`、`share`、`node` 等结构性段（会丢语义的不静默降级）。

`auto_escape=true` 时 `message` 整体当纯文本，不解析 CQ 码。

### A.2 事件（qqcache → BFF）

每个通道收到的事件 JSON 结构一致：

#### message / message_sent

```json
{
  "time": 1793278201,
  "self_id": 123456789,
  "post_type": "message",              // 或 "message_sent"（bot 自己发的，含其他端发的）
  "message_type": "group",             // "group" | "private"
  "sub_type": "normal",                // 群 normal；私聊 friend
  "message_id": 123,                   // ob11 int id，可用于 [CQ:reply,id=] 和 get_msg
  "user_id": 987654321,                // 发送者；合成 id 见"限制"
  "group_id": 456789,                  // 仅群消息
  "message": "[CQ:image,file=x.png,url=file:///nt/pic/x.png]看看",
  "raw_message": "看看",               // 无 CQ 码的纯文本
  "font": 0,
  "sender": {
    "user_id": 987654321,
    "nickname": "群员显示名",
    "card": "",
    "role": "member",                  // owner/admin/member（从头衔标签推断）
    "title": "LV5 管理员",
    "sex": "unknown", "age": 0
  },
  "qqcache_msg_id": "7701234567890",   // 扩展字段：QQ 内部消息 id
  "qqcache_kind": "text"               // 扩展字段：text/image/voice/file/reply/forward/card
}
```

入站 CQ 段规则：

- `text` → CQ 转义后的纯文本（@他人在文本里只剩 `@昵称`，无结构）
- `image` → `[CQ:image,file=<basename>,url=file://<nt_data绝对路径>]`，经 `/media` 取内容
- `voice` → `[CQ:record,file=file://...]` 或占位 `[语音]`
- `video` → `[CQ:video,file=...]` 或 `[视频]`
- `file` → 文本 `[文件] 文件名 (大小)`
- `reply` → 若能在已发事件流中匹配到被引消息：`[CQ:reply,id=N]` 前缀 + 正文
- `forward` → 文本 `[聊天记录]\n...`
- `system`（撤回/进群提示）→ **不上报**（已入库但不发事件）
- `message_sent`：bot 账号发出的消息（API 或手机端），`target_id` = 会话 uin

#### meta_event

```json
{ "post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "time":..., "self_id":... }
{ "post_type": "meta_event", "meta_event_type": "heartbeat", "interval": 30000,
  "status": {"online":true,"good":true,"login_state":"logged_in"}, "time":..., "self_id":... }
{ "post_type": "meta_event", "meta_event_type": "login_state",
  "prev_state": "logged_in", "state": "login_required", "time":..., "self_id":... }
```

- `heartbeat.status.login_state`：每次心跳附带的登录态（`logged_in`/`login_required`/`unknown`/`not_running`）
- `login_state` 事件（自定义）：登录态**变化**时即发——被踢下线/会话过期时 QQ 掉回登录窗，这就是 BFF 的主动告警通道；收到 `login_required` 后可引导用户去 `/qr`。首个心跳也会发一次（`prev_state: null`）

### A.3 WS 协议细节

**正向 WS**（BFF 连 qqcache）：`ws://host:5700/`（可带 `?access_token=`）。连接后：

- 客户端发：`{"action":"send_msg","params":{...},"echo":"t1"}` → 服务端回 `{"status","retcode","data","echo":"t1"}`
- 服务端随时推事件 JSON（无 echo 字段的消息即是事件）

**反向 WS**（qqcache 连 BFF）：BFF 起 ws server，qqcache 带 `X-Self-ID`/`X-Client-Role: Universal`/`Authorization` 头连接。同一连接上：qqcache 推事件；BFF 发 api 请求（同格式），qqcache 回响应。断线自动重连（3s）。

### A.4 限制与坑（BFF 必须处理）

1. **历史窗口**：`get_msg_history` 只返回服务运行以来**经过事件流**的消息，不是 QQ 全量历史。翻到底要给"更早请去客户端看"的提示。
2. **合成 user_id → 升级为真实 uin**：群成员首次发言时先分配 `≥900000000000` 的稳定合成 id（按 群:显示名 哈希）。**事件 emit 前会对未解析发言人现场解析**（头像开卡；节点已出视口就回落成员列表右键「查看资料」）——所以 `event.user_id` 大概率直接就是真号，下游插件（NoneBot 生态）裸读也能绑。代价：每个未解成员的第一条消息延迟 ~3-6s（一次性）。合成号只在解不动时漏出（退群/同名/卡超时）。其余路径：
   - **按需**：`get_group_member_info` 传合成号现场解析；`resolve_member` action 是显式版本，返回 `{"user_id": <真号>, "was_user_id": <合成>, "nickname"}`——绑定数据库这类「立刻要真号」的场景用它；
   - **通知**：任何路径解析成功都推 `notice.qqcache_member_resolved` 事件 `{"group_id","user_id":<真号>,"was_user_id":<合成>,"nickname"}`，下游按它合并身份。
   - **系统通知**：QQ 系统通知（下线通知、加群申请、好友申请…）推 `notice.qqcache_system_notification` 事件 `{"title","text"}`，只读。被踢下线时 `get_login_state` 额外返回 `notice: {"title","text","time"}`，直到重新登录；WebUI 据此弹窗。
   - **出站限速**：同一会话 10s 内最多 3 条、60s 内最多 12 条，全局 60s 内最多 20 条；30s 内向同一会话发相同内容会被拒绝。超限返回 `retcode 1429`，保证没有发出（Satori 为 HTTP 429，automatic 为 `rate_limited`，`retryable: true`）。
   `member_info` 的 `real_uin` 字段判断当前是合成还是真实。同名成员（同群同显示名）拒绝归因、保持合成——名字键模型分不出谁是谁。仍不能把合成 id 当真实 QQ 号展示。
3. **消息级发送者**：群消息 `user_id` 在成员归因完成前是合成 id + `nickname` 显示名；同名/改名成员在归因完成前无法区分（归因按显示名缓存）。
4. **图片 url** 是容器内路径：必须经 `GET /media?path=<url的file://之后部分>` 代理取字节，`file://` 客户端不可达。
5. **串行 GUI**：所有出站动作排队过同一个窗口，并发请求会串行执行；`send_msg` 延迟 1~5s 是正常的，超时建议 ≥60s。发送失败一律 `retcode=1400` + `message` 原因文本；bot 被禁言/输入框不可用时 message 形如 `输入框不可用（<QQ的禁言提示>）`，BFF 可以把这类错误直接透传给用户。
6. **system 消息不上报**：撤回等灰条消息不产生事件。
7. **message_id** 只在服务期内有效，重启不重置但旧 id 对应的 QQ 消息可能已不可定位（引用回复会 1400 失败）。
8. **self_id 自动发现**：不传 `--self-id` 时从 `~/.config/QQ/Partitions/qqnt_<uin>` 目录检测登录账号；检测不到则 `self_id=0`。
9. **`/qr` 是主动作而不是查询**：内部会跑 `login.prepare()`——自动收掉「登录失败」类模态框 → 点「其他登录方式」→ 等二维码出现 → **如果 QR 已过期则自动点 QQ 的「刷新」按钮重生**，再裁图返回。已实测：手机退出登录 → QQ 进程退出（它是 PID 1，容器随之停止）→ 重启容器 → 登录窗 → `/qr` 一次调用拿到二维码 PNG；`expired-btn` 刷新路径也已实测。WebUI 的"刷新二维码"按钮就是直接再 GET 一次 `/qr`，服务端自愈。
10. **二维码会过期**：`get_login_state` 的 `qr_expired` 字段可用来显示"已过期，点刷新"徽标；状态变 `logged_in` 即收尾。**WebUI 必须先查 `get_login_state`，只在 `login_required` 时拉 `/qr`**——否则拿到的是 JSON 错误体（`{retcode:1404,"message":"当前状态 <state>，无可取二维码"}`），当 `<img>` 渲染就是裂图；BFF 代理时看 Content-Type 分流。另外注意：**手机端主动退出会让容器整个停掉**（旧镜像里 QQ 是 PID 1；新 entrypoint 已改为守护循环自动重拉），WebUI 需要把「容器离线」也当成一种状态；带缓存凭据的重启可能自动登录回来。
11. **`#ww` 测试触发器**：`--test-trigger`（env `QQCACHE_TEST_TRIGGER`，**默认关**）开启后，任何会话里收到内容恰为 `#ww` 的消息（含 bot 自己手机端发出的）会原地回复一条服务状态（版本/平台/运行时长）。用于验证「通知→读消息→回消息」全链路是否活着。
12. **打开会话的盲区已用轮询补上**：QQ 对**当前打开的会话**不发桌面通知，所以服务每 `--poll-interval` 秒（默认 2s，env `QQCACHE_POLL_INTERVAL`，0 关闭）轮询一次当前打开的会话。代价是每 tick 一次 a11y 读取；消息到事件的延迟上限 ≈ 轮询间隔。免打扰会话仍然永远收不到（它们既不通知也不会被主动打开）。

### A.5 管理接口：日志 + 通道管理

均为自定义扩展，**必须配置 `--access-token`**，未配置时返回 1403/403。所有 action 走 `POST /<action>`（和正向/反向 WS 请求格式一致）。

#### 日志

**`get_logs`** — 环形缓冲（容量 2000 条）里的服务日志，每条记录带单调 `seq`：

```json
请求:  { "tail": 200, "after": 0, "level": "warning" }
响应:  {
  "logs": [
    {"seq": 137, "time": 1793278201.5, "level": "WARNING", "levelno": 30,
     "module": "qqcache.serve", "message": "通知处理失败 ..."}
  ],
  "latest_seq": 140
}
```

- `after`：只返回 `seq > after` 的记录（增量轮询游标）
- `tail`：取最后 N 条（默认 200，上限 1000）
- `level`：最低级别过滤（`debug`/`info`/`success`/`warning`/`error`/`critical`）

**`GET /logs/stream`**（WS upgrade）— 实时尾随：握手成功后先回放最近 `tail` 条（query 参数，默认 200），然后逐条推新记录。每条消息：

```json
{"type": "log", "data": {"seq": 141, "time": ..., "level": "INFO", "levelno": 20, "module": "qqcache.serve", "message": "..."}}
```

25s 无新记录会发 ws ping 保活。断线重连用 `after=<最后收到的seq>` 回放即可补缺口（`get_logs` 同理）。`access_token` 可放 query 或 `Authorization` 头。

#### 通道管理

通道 = 协议端对外的一个连接端点。所有协议的通道统一管理：

| type | 协议 | 作用 | 参数 | 状态 |
|---|---|---|---|---|
| `http` | OneBot v11 | API 监听端口（HTTP + 正向 WS 同端口），使用全局 access_token | `host`（默认 127.0.0.1）、`port` | `listening` |
| `ws_reverse` | OneBot v11 | 反向 WS 客户端（连框架 universal 端点） | `url`、`token?` | `connecting`/`connected` |
| `http_post` | OneBot v11 | 事件 POST 上报地址 | `url` | `active` |
| `satori` | Satori | Satori 服务端（`/v1` + `/v1/events`） | `host`（默认 0.0.0.0）、`port`（默认 5500）、`token?`、`platform?`（默认 qq） | `listening` |
| `automatic` | automatic | automatic 服务端（`/automatic/v1`） | `host`（默认 0.0.0.0）、`port`（默认 5600）、`token?` | `listening` |
| `automatic_reverse` | automatic | automatic 反向 WS 客户端 | `url`、`token?` | `connecting`/`connected` |

**`list_transports`** → `{transports: [...]}`，通道对象：

```json
{
  "id": "t4", "type": "satori", "protocol": "satori", "title": "Satori 服务端",
  "source": "runtime", "created_at": 1793278201,
  "host": "0.0.0.0", "port": 5500, "platform": "qq",
  "address": "0.0.0.0:5500", "endpoint": "http://0.0.0.0:5500/v1",
  "status": "listening", "clients": 1, "webhooks": 0, "token_set": true
}
```

**`add_transport {type, ...}`** → `{id: "t4", transports: [...]}`（顺带返回全量列表）

**`update_transport {id, <字段>..., regenerate_token?}`** → `{id, transports}`：原地修改，id 不变。新配置启动失败时恢复旧配置并返回错误。

**`get_transport_token {id}`** → `{token}`：读取通道的独立令牌（`ws_reverse`、`satori`、`automatic`、`automatic_reverse`）。列表中只给出 `token_set`，不含令牌明文。

**`del_transport {id}`** → `{transports: [...]}`

语义：

- `source`：`startup`（启动参数或环境变量 `QQCACHE_POST_URL` / `QQCACHE_WS_REVERSE_URL` 配置的，**不可经 API 修改或删除**，防自断通道）/ `runtime`（API 创建的）
- `runtime` 通道**持久化**到 `<db>.transports.json`（0600），服务重启自动恢复；令牌也会持久化（明文落盘，注意安全边界）
- 令牌规则：`satori` 与 `automatic` 监听非回环地址时必须有令牌——未提供时自动生成，显式传空字符串返回 1400
- 每条 Satori / automatic 通道是独立的适配器实例，事件序号（`sn` / `seq`）各自独立；Satori 通过 `meta/webhook.create` 注册的 WebHook 保存在对应通道中
- `http_post` 去重：已在生效的 url 再 `add_transport` 返回 1400
- 删除 `http` 监听只关端口——已建立的 ws 连接不受影响（活到对端断开或进程退出）
- 删除反向 WS 类通道会断开当前连接并停止重连
- 旧版本的 `<db>.protocols.json`（每个协议一份配置）在启动时自动迁移为通道，原文件改名为 `.migrated`；只迁移已启用的协议

---

## Part B — 前端 ↔ BFF 建议契约

BFF 实现细节自定，下面是建议的最小契约，字段尽量对齐上游减少翻译。

### B.0 鉴权（面板密码）

两层鉴权，职责分开：

```
浏览器 ──面板密码/session──► BFF ──access_token──► qqcache
```

- **前端 ↔ BFF**：面板密码由 BFF 持有（env 如 `WEBUI_PASSWORD`），**不下发** qqcache 的 access_token
- **BFF ↔ qqcache**：BFF 在所有上游请求上带 `Authorization: Bearer <access_token>`

建议的最小登录契约：

```
POST /api/login    body: {password}
  → 200 {token: "<session>"}          （BFF 自签 session token / JWT，或 Set-Cookie）
  → 401 {error: "wrong password"}

之后所有 /api/* 和 /ws 请求带 Authorization: Bearer <session>（或 cookie）
GET  /api/logout（或前端本地丢 token 即可）
```

单用户场景可以极简：session token 就是密码本身经 BFF 校验后下发的固定签名串；重点是 **access_token 绝不进浏览器**，且未登录时 /api/* 一律 401。

### B.1 REST

> 会话面板（会话列表/消息浏览/发送/图片上传/未读计数）已移除：录入的消息流本就不完整（只覆盖收到通知的会话、只到服务启动后），做成聊天界面会误导；收发消息走协议端 OneBot API。

```
GET  /api/status
  → { online, self_id, self_name, version, uptime_s, transports: {http, ws_clients, post_urls, reverse_ws} }

GET  /api/media?path=<上游file://路径>
  → 透传 qqcache /media 的字节，前端 <img src="/api/media?path=...">

# —— 管理面（对应上游 A.5）——
GET  /api/logs?tail=200&after=<seq>&level=warning
  → { logs: [...], latest_seq }        （透传 get_logs；前端轮询补增量）

GET  /api/transports
  → [ ...通道对象 ]                     （透传 list_transports）

POST /api/transports
  body: {type, host?, port?, url?, token?, platform?}（字段见 A.5 通道类型表）
  → { id, transports }

PATCH /api/transports/{id}
  body: 要修改的字段，或 {regenerate_token: true}
  → { id, transports }

GET  /api/transports/{id}/token
  → { token }                          （只在用户点「显示」「复制」时请求）

DELETE /api/transports/{id}
  → { transports }
```

### B.2 实时通道（WS 或 SSE）

`WS /ws` —— BFF 把上游事件翻译/扇出给前端，建议包一层信封：

```json
// 服务端 → 前端
{ "type": "event", "data": <A.2 的完整事件> }
{ "type": "message", "data": <message/message_sent 事件> }        // 便捷别名，可用可不用
{ "type": "status", "data": { "online": true } }                  // 上游连接状态变化
{ "type": "log", "data": { "seq":..., "level":..., "message":... } }   // 可选：BFF 桥接上游 /logs/stream
```

### B.3 数据模型对齐说明

- 前端会话 id 直接用 **uin 字符串**（不要 int，合成 id 也是 int 但 uin 本身是字符串存储的）
- `message_id`（int）≠ `qqcache_msg_id`（string）：前者给 API/引用用，后者只作调试展示
- 渲染 `message` 字段时按 CQ 码解析；`raw_message` 可作 fallback
- 群成员 `user_id >= 900000000000` → 显示 "未解析" 标记

### B.4 BFF 需要自维护的状态

- 事件缓存（可选）：把事件落自己库里做搜索/跨重启历史，补 `get_msg_history` 窗口外的空白
- 上游健康：定期 `get_status` 或监听 heartbeat，驱动前端状态灯
