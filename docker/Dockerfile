# WhiskerWind：官方 Linux QQ + qqcache 协议端 + WebUI，单容器
# 构建：docker build -f docker/Dockerfile -t whiskerwind .

FROM node:22-bookworm-slim AS webui
WORKDIR /opt/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY webui/server ./server
COPY webui/public ./public


FROM debian:trixie-slim

# 只在 3.2.33 上验证过界面结构（AT-SPI 树随版本会变），换版本要连同 sha256 一起改
ARG QQ_VERSION=3.2.33-52892
ARG QQ_URL=https://qqdl.gtimg.cn/qqfile/QQNT/9.9.35/beta/1763096b/linuxqq_3.2.33-52892_amd64.deb
ARG QQ_SHA256=502a978f2d03af9f21acefc461f9d1d1fe09b65bad620bbfcdb589a79ac53b7e

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    DISPLAY=:99 \
    PYTHONPATH=/opt/qqcache \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        tigervnc-standalone-server tigervnc-tools openbox x11-utils x11-xserver-utils \
        dbus-x11 dbus-system-bus-common at-spi2-core \
        fonts-noto-cjk fontconfig \
        xdotool xclip xsel scrot \
        python3 python3-gi gir1.2-atspi-2.0 python3-dbus python3-loguru \
        ca-certificates curl procps psmisc tzdata libstdc++6 \
        libgtk-3-0t64 libnotify4 libnss3 libxss1 libxtst6 libatspi2.0-0 \
        libuuid1 libsecret-1-0 xdg-utils \
        libasound2t64 libgbm1 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxrandr2 libxfixes3 libxi6 libxcursor1 libxrender1 \
        libpango-1.0-0 libcairo2 libatk1.0-0t64 libatk-bridge2.0-0t64 \
        libcups2t64 libexpat1 libdbus-1-3 libglib2.0-0t64 \
    && curl -fsSL -o /tmp/qq.deb "$QQ_URL" \
    && echo "$QQ_SHA256  /tmp/qq.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends /tmp/qq.deb \
    && rm -f /tmp/qq.deb \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY --from=webui /usr/local/bin/node /usr/local/bin/node
COPY --from=webui /opt/webui /opt/webui
COPY qqcache /opt/qqcache/qqcache
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /var/lib/qqcache /root/.config/QQ \
    && ln -sf /opt/QQ/qq /usr/local/bin/qq

LABEL org.opencontainers.image.title="WhiskerWind" \
      org.opencontainers.image.description="基于官方 Linux QQ 的 OneBot v11 / Satori 协议端" \
      org.opencontainers.image.licenses="GPL-3.0-only"

VOLUME ["/var/lib/qqcache", "/root/.config/QQ"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
