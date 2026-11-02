---
layout: home

hero:
  name: WhiskerWind
  text: 风须
  tagline: 基于官方 Linux QQ 客户端的 Bot 协议端，提供 OneBot v11 与 Satori 接口
  image:
    src: /whiskerwind-sticker.svg
    alt: WhiskerWind
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/getting-started
    - theme: alt
      text: GitHub
      link: https://github.com/XTxiaoting14332/WhiskerWind

features:
  - title: 不修改客户端
    details: 消息通过系统通知与无障碍接口（AT-SPI）获取，发送通过模拟界面操作完成。项目不注入代码，也不实现 QQ 的私有协议。
  - title: 单容器部署
    details: 镜像内置 QQ 客户端、协议端与 WebUI。登录通过 WebUI 扫码完成，登录状态保存在数据卷中。
  - title: 多协议接口
    details: 提供 OneBot v11（HTTP API、正向与反向 WebSocket、HTTP POST 上报）和 Satori 接口，并支持自定义的 automatic 协议。
  - title: 发送前校验目标
    details: 发送前核对会话名称与 QQ 号，校验不通过时拒绝发送。内置出站限速与重复内容拦截。
---

## 项目简介

WhiskerWind 是 Bot 协议端的一种实现。它在 Docker 容器中运行官方 Linux QQ 桌面客户端，并通过两个标准桌面接口与之交互：

- 桌面通知（D-Bus `org.freedesktop.Notifications`）：获知哪个会话收到了新消息。
- 无障碍接口（AT-SPI）：读取会话界面中的消息，并在界面上完成发送、撤回、禁言等操作。

由于操作的对象是客户端界面，消息的接收与发送都存在一定延迟，可执行的操作也受限于界面能够完成的范围。协议之间的功能差异见[功能差异](/compatibility)，实现原理见[实现原理](/architecture)。

::: warning
本项目仅作为概念验证（PoC），用于验证「只通过系统通知和无障碍接口驱动官方 QQ」这一路线是否可行。它不是成熟的生产级协议端，不保证稳定性和长期维护，请勿用于重要账号或生产环境。
:::
