# WhiskerWind WebUI

qqcache 的轻量监控台（前端 SPA + BFF 后端）。收发消息是协议端 API 的事，WebUI 不提供会话浏览/发送。

设计风格完全对齐 Vercel Console，支持亮色/暗色/跟随系统三态主题。

---

## 架构

```
浏览器 SPA ──面板密码/Session Token──► BFF (Node.js) ──access_token──► qqcache serve (容器内:5700)
```

- **前端绝不直连 qqcache**：上游 `access_token` 仅保存在 BFF 环境变量中，绝不下发至浏览器。
- **鉴权机制**：前端使用面板密码登录换取 BFF 签发的 Session Token，未授权请求全站拦截并返回 401。
- **实时通道**：BFF 建立 WebSocket 服务（`/ws`），桥接上游 OneBot v11 事件流与服务实时日志，实时推送到前端。

---

## 核心功能与页面

0. **登录页**：单密码防护，支持区分「密码错误」与「上游容器离线」。
1. **运行概览**：QQ 在线状态、今日收发消息量、运行时长、通道概况与最近事件流。
2. **事件日志**：全量 OneBot v11 事件流，支持按类型（`message`、`meta_event` 等）及 UIN 过滤，可一键展开 JSON 原始数据。
3. **通道管理**：在一张表中管理所有协议的通道，支持新建、编辑与删除，修改即时生效：
   - OneBot v11：HTTP 监听、反向 WebSocket、HTTP POST 上报；
   - Satori：服务端（可配置 platform）；
   - automatic：服务端、反向 WebSocket。

   Satori 与 automatic 的通道各有独立令牌，可在编辑弹窗中显示、复制或重新生成。由容器环境变量配置的通道标记为「启动参数」，不能修改或删除。
4. **服务日志**：实时滚动查看服务内部日志，支持日志级别过滤（DEBUG/INFO/WARNING/ERROR）与关键词过滤，支持锁定滚动。
5. **状态与设置**：
   - 四态登录卡片：已登录（显示账号昵称）、需要扫码登录（显示二维码及过期重新加载）、状态异常、容器离线；
   - 容器屏幕实时截图（`/screen`）：支持一键抓取并预览容器真实 X11 画面。

---

## 本地开发

WebUI 已经内置在 WhiskerWind 镜像里，正常使用不需要单独部署。以下是本地开发 BFF 的步骤（需要 Node.js ≥ 18）：

```bash
cd webui
npm install
cp .env.example .env    # 填 WEBUI_PASSWORD 和 QQCACHE_TOKEN
npm run dev             # 热重载
```

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `PORT` | 监听端口 | `3000` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `WEBUI_PASSWORD` | 面板登录密码，**必填** | — |
| `QQCACHE_UPSTREAM` | 上游 qqcache serve 地址 | `http://127.0.0.1:5700` |
| `QQCACHE_TOKEN` | 上游 access_token（容器数据卷里的 `access-token` 文件） | — |
| `SESSION_TTL_MS` | 会话有效期（毫秒） | `604800000`（7 天） |
