# syntax=docker/dockerfile:1

# ---- Frontend build ----
FROM node:20-bookworm-slim AS frontend
WORKDIR /src/web/admin
COPY web/admin/package.json web/admin/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com \
    && if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY web/admin/ ./
RUN npm run build

# ---- Runtime ----
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    CHROME_USE_EXISTING=true \
    CHROME_CDP_URL=http://127.0.0.1:9222

WORKDIR /app

# 国内服务器：替换 Debian 源为清华镜像
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null \
    || true

# System libs for Pillow / ddddocr + Xvfb（容器内非 headless 运行 Chrome，绕过 ICRIS headless 检测）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        wget \
        xvfb \
        libglib2.0-0 \
        libgomp1 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome 131（ICRIS 门户 TLS 指纹绕过，与本地版本一致）
# 国内服务器 dl.google.com 可能不可达；失败时用清华 Chrome 镜像源重试
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable=131.* \
    && rm -rf /var/lib/apt/lists/* \
    || (echo "dl.google.com unreachable, trying Tsinghua mirror..." \
        && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://mirrors.tuna.tsinghua.edu.cn/google-chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends google-chrome-stable=131.* \
        && rm -rf /var/lib/apt/lists/* \
        || echo "Chrome 131 install failed on all sources, using Playwright Chromium fallback")

COPY requirements.txt ./
RUN pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && playwright install --with-deps chromium

# App source (static/admin filled from frontend stage)
COPY config ./config
COPY src ./src
COPY templates ./templates
COPY main.py ./
COPY docs ./docs
RUN mkdir -p /app/static /app/data/materials
COPY --from=frontend /src/static/admin /app/static/admin

EXPOSE 8081 8082

CMD ["python", "main.py", "wework-external-bot"]
