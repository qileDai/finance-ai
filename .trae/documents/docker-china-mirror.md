# Docker 构建使用清华源加速

## 目标

修改 Dockerfile，让 Docker 构建时使用国内镜像源（清华 / 淘宝），解决服务器上 pip/npm/apt 包安装慢的问题。

## 现状

[Dockerfile](file:///d:/projects/finance-ai/Dockerfile) 中有 5 个安装步骤都走默认源：

| 行号 | 安装内容 | 默认源 | 慢 |
|---|---|---|---|
| 7 | `npm ci` / `npm install` | registry.npmjs.org | ✅ 慢 |
| 24-35 | `apt-get install` (系统库) | deb.debian.org | ✅ 慢 |
| 38-42 | Google Chrome 安装 | dl.google.com | ✅ 慢/不可达 |
| 45-46 | `pip install -r requirements.txt` | pypi.org | ✅ 慢 |
| 47 | `playwright install chromium` | playwright.azureedge.net | ✅ 慢 |

项目根目录无 `pip.conf` / `.npmrc` / `sources.list`。

## 改动方案

只修改 **1 个文件**：[Dockerfile](file:///d:/projects/finance-ai/Dockerfile)

### 改动 1：npm 淘宝源

第 7 行前加 `RUN npm config set registry https://registry.npmmirror.com`：

```dockerfile
COPY web/admin/package.json web/admin/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com \
    && if [ -f package-lock.json ]; then npm ci; else npm install; fi
```

### 改动 2：apt 清华源

第 24 行前替换 Debian sources.list 为清华源：

```dockerfile
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        wget \
        libglib2.0-0 \
        libgomp1 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*
```

> 注：`python:3.12-slim-bookworm` 使用 `/etc/apt/sources.list.d/debian.sources`（DEB822 格式），不是传统的 `/etc/apt/sources.list`。如果该文件不存在则 fallback 到 sed 替换 `sources.list`。

### 改动 3：pip 清华源

第 45 行加 `-i` 参数：

```dockerfile
RUN pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && playwright install --with-deps chromium
```

### 改动 4：Playwright 下载加速

第 14-19 行 ENV 区加 `PLAYWRIGHT_DOWNLOAD_HOST`：

```dockerfile
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    CHROME_USE_EXISTING=true \
    CHROME_CDP_URL=http://127.0.0.1:9222
```

### 改动 5（可选）：Chrome 源不可达处理

如果服务器在中国大陆且无代理，`dl.google.com` 不可达。在第 38-42 行 Chrome 安装前加判断：

```dockerfile
# 尝试 Google 源；不可达则跳过（用 Playwright Chromium 兜底）
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    || echo "Chrome install skipped (dl.google.com unreachable), using Playwright Chromium fallback"
```

> `|| echo` 让 Chrome 安装失败时不中断构建，运行时用 Playwright Chromium（需 `.env` 设 `CHROME_USE_EXISTING=false`）。

## 改动文件清单

| 文件 | 改动 |
|---|---|
| [Dockerfile](file:///d:/projects/finance-ai/Dockerfile) | npm 清华源 + apt 清华源 + pip 清华源 + Playwright 下载源 + Chrome 不可达容错 |

## 验证

```powershell
# 本地验证 Dockerfile 语法
docker build --no-cache -t finance-ai:test .
```

检查构建日志：
- npm 输出应显示 `registry=https://registry.npmmirror.com`
- apt 输出应显示 `mirrors.tuna.tsinghua.edu.cn`
- pip 输出应显示 `pypi.tuna.tsinghua.edu.cn`
- Playwright 下载应从 `npmmirror.com` 拉取
