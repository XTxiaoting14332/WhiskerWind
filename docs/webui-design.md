# WhiskerWind WebUI 设计文档

## 定位

WhiskerWind（后端包名 qqcache）的 WebUI 是一个**监控台**，不是 QQ 客户端的替代品。它回答两件事：

1. bot 现在还活着吗（QQ 进程、通知通道、事件流是否正常）
2. 谁发来了什么消息（实时事件流）

不做会话面板（会话列表/消息浏览/发送）：录入的消息流本就不完整，做成聊天界面会误导；收发消息走协议端 OneBot API。

底层能力全部由 `qqcache serve`（OneBot v11）提供。WebUI 自身只做聚合和呈现，**不直接碰 QQ GUI、不直接读 AT-SPI**。

## 架构

```
┌────────────┐   REST + WS    ┌─────────────────┐   OneBot v11    ┌──────────────┐
│  前端 SPA   │ ◄───────────► │  WebUI 后端(BFF) │ ◄─────────────► │ qqcache serve │
│ (浏览器)    │               │  (聚合/代理/缓存) │  HTTP+WS/反WS   │ (容器内 5700) │
└────────────┘               └─────────────────┘                 └──────┬───────┘
                                                                        │ GUI 自动化
                                                                   Linux QQ (官方)
```

为什么需要 BFF 而不是前端直连 qqcache：

- qqcache 的事件推送是**单向的**（POST 到配置 URL，或 WS 客户端连接）——浏览器不能当被推端，BFF 收事件后经自己的 WS/SSE 扇出给前端
- 出站图片需要本地文件路径——前端上传图片到 BFF，BFF 落盘后把 `file://` 路径传给 qqcache（或直接用 `base64://`，qqcache 已支持）
- 历史消息在 sqlite 里，BFF 可以直接读库做富查询（分页/搜索），比挨个调 API 快
- access_token 不该下发到浏览器——**鉴权在 BFF 收口**：前端用面板密码换 BFF session，上游 token 只存在于 BFF 侧（接口契约见 webui-api.md B.0）

BFF 也可以薄到只代理：转发 REST 请求 + 把 `post_url` 指向自己再 SSE 转发。两种都合法，看实现方的取舍。

## 页面

### 0. 登录页

- 单密码门：输入面板密码 → `POST /api/login` → 拿 session token，之后请求头带上
- 未登录/401 时全站重定向回此页；不做注册、不做多用户
- 登录失败提示要区分「密码错误」和「BFF 连不上上游」

### 1. 概览（Dashboard）

- 服务状态卡：`get_status` → QQ 是否在线（`online/good`）、协议版本、self_id
- 通道状态：HTTP API 地址、已配置的 post_url、正向/反向 WS 连接数
- 今日事件计数：message / message_sent / meta_event
- 最近事件滚动条（最近 20 条）

### 2. 事件日志

- 全量事件流（原始 JSON，可折叠），按 post_type / 会话过滤
- 调试用途：每条事件的到达时间、走哪个通道

### 3. 通道管理

对应上游 `list_transports` / `add_transport` / `update_transport` / `get_transport_token` / `del_transport`：

- 通道列表：协议徽标（OneBot v11 / Satori / automatic）、类型、地址（服务端另显示接入地址）、状态（监听中 / 已连接 / 连接中 / 启用，服务端附带客户端数）
- 新建与编辑共用一个弹窗：先选协议，再选类型，字段随类型切换（监听类：地址 + 端口；连接类：目标地址；Satori 另有 platform）
- 带独立令牌的通道（OneBot 反向 WS、Satori、automatic）在编辑时可显示、复制或重新生成令牌；令牌不随列表下发
- `source=startup` 的行标注「启动参数」，编辑与删除按钮置灰
- 新建、修改、删除即时生效并自动保存，服务重启后自动恢复

### 4. 服务日志

对应上游 `get_logs` / `/logs/stream`：

- 实时滚动日志窗（BFF 桥接上游 `/logs/stream`，或前端轮询 `get_logs?after=<seq>`）
- 级别过滤 chip（debug/info/success/warning/error）、关键字过滤（前端侧做，不用惊动上游）
- 暂停滚动按钮 + "回到底部"；每条显示 时间 级别 模块 消息
- 连接断开时显示重连提示，用 `after=<最后seq>` 回补缺口

### 5. 设置/状态

- 只读展示 serve 启动参数（API 地址、self_id、上报目标）
- **登录状态**：`get_login_state` 轮询——`logged_in` 显示绿点+账号；`login_required` 显示二维码（`GET /qr`，用户点"刷新"按钮才重新请求，不过期不自动刷；`qr_expired` 时叠"已过期"徽标）；`unknown`/`not_running` 提示异常；**容器离线**（BFF 连不上上游）是第四种状态，单独展示
- 调试用：`GET /screen` 整屏截图入口（排查 GUI 状态时一眼看到容器里真实画面）
- 不需要配置写回（配置改在容器/启动侧；通道的新增、修改与删除在「通道管理」页做）

## 关键 UX 细节

- **合成 ID 标记**：群成员 user_id ≥ 900000000000 的是合成 ID（该成员真实 QQ 号未解析），UI 上标个 "?"，避免用户误以为是真号
- **断线提示**：BFF↔qqcache 的连接状态要在头部常驻显示（绿/红点）

## 技术栈建议（不强制）

- 前端：任意 SPA（React/Vue/Svelte），无框架约束
- BFF：Node (Fastify/Hono) 或 Python (FastAPI) 都行；要能和前端 WS
- 与 qqcache 对接：HTTP API 调出站，事件接收用 `post_url`（POST 到 BFF 的 `/ob11/events`）或反向 WS（BFF 起 WS server，qqcache 连过来）

## 明确不做的

- 不做好友/群管理操作（协议动作超出 GUI 自动化能力）
- 不做消息撤回/管理员操作（未实现）
- 不承诺全量历史（见"消息边界提示"）
- 不做账密登录（密码输入自动化可以做但没必要——扫码已够用）
