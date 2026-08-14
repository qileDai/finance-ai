# 宝塔面板部署 finance-ai（Docker 方式）

## 目标

在宝塔面板（BT-Panel / aaPanel）Linux 服务器上部署整个项目：
- **后台 bot**（`python main.py wework-external-bot`，端口 8081）：企微外部群回调 + AI 回复
- **管理后台 admin**（`python main.py admin`，端口 8082）：React SPA + `/admin/api`
- 两个进程都能启动、健康检查通过、数据持久化

---

## Docker 文件检查结果

### 现有文件清单

| 文件 | 状态 | 说明 |
|---|---|---|
| [Dockerfile](file:///d:/projects/finance-ai/Dockerfile) | ✅ 基本完整 | 多阶段构建：node:20 前端 build → python:3.12 runtime + playwright chromium |
| [docker-compose.yml](file:///d:/projects/finance-ai/docker-compose.yml) | ✅ 完整 | bot(8081) + admin(8082) + qdrant(可选 profile)，共享 `./data` 卷 |
| [.dockerignore](file:///d:/projects/finance-ai/.dockerignore) | ✅ 合理 | 排除 .git/.venv/node_modules/data/.env 等 |
| [requirements.txt](file:///d:/projects/finance-ai/requirements.txt) | ✅ 完整 | 含 playwright/ddddocr/Pillow/qdrant-client 等；`msvc-runtime` 仅 win32 条件安装 |
| [.env.example](file:///d:/projects/finance-ai/.env.example) | ✅ 存在 | 需复制为 `.env` 填生产值 |

### 需修复的问题（3 项）

#### 问题 1：容器内 Chrome CDP 不可用（影响 ICRIS 注册 TLS 指纹绕过）

**现状**：[src/browser/launcher.py:60-67](file:///d:/projects/finance-ai/src/browser/launcher.py#L60-L67) 的 `_try_launch_cdp_chrome()` 只在 Windows 找 `chrome.exe`（PROGRAMFILES），Linux 容器里返回 False → 走 Playwright bundled Chromium fallback（[launcher.py:157-178](file:///d:/projects/finance-ai/src/browser/launcher.py#L157-L178)）。

**影响**：ICRIS 门户可能检测 headless Chromium 的 TLS 指纹。Fallback 也有 `--disable-blink-features=AutomationControlled` + stealth 脚本注入，但不如真实 Chrome CDP 强。

**修复方案**：在 Dockerfile 安装 Google Chrome Stable，并让 launcher 支持 Linux Chrome 路径。

- [Dockerfile](file:///d:/projects/finance-ai/Dockerfile)：在 apt-get install 后追加添加 Google Chrome 源 + 安装
- [src/browser/launcher.py:60-67](file:///d:/projects/finance-ai/src/browser/launcher.py#L60-L67)：`_try_launch_cdp_chrome()` 增加 Linux Chrome 路径候选 `/usr/bin/google-chrome-stable`

#### 问题 2：Dockerfile ENV 缺少 `CHROME_USE_EXISTING` 默认值

**现状**：[Dockerfile:14-17](file:///d:/projects/finance-ai/Dockerfile#L14-L17) 设了 `PLAYWRIGHT_BROWSERS_PATH` 但没设 `CHROME_USE_EXISTING`。settings.py 默认 `chrome_use_existing=False`，容器里不会尝试 CDP → 走 fallback。

**修复**：Dockerfile ENV 加 `CHROME_USE_EXISTING=true`（装了 Chrome 后让 launcher 尝试 CDP）。

#### 问题 3：缺少生产部署文档

**修复**：新建 [docs/DEPLOY_BAOTA.md](file:///d:/projects/finance-ai/docs/DEPLOY_BAOTA.md)，包含宝塔面板从 0 到 1 的部署步骤。

---

## 实施步骤

### 步骤 1：修复 Dockerfile（装 Google Chrome + ENV）

**文件**：[Dockerfile](file:///d:/projects/finance-ai/Dockerfile)

**改动**：
1. apt-get install 后追加添加 Google Chrome 源 + 安装 `google-chrome-stable`
2. ENV 加 `CHROME_USE_EXISTING=true`、`CHROME_CDP_URL=http://127.0.0.1:9222`
3. Playwright install 改为只装 `chromium` 依赖（Chrome 本体由 apt 装，Playwright 只需连接 CDP）

```dockerfile
# 在 apt-get install 块后追加 Chrome 源
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*
```

ENV 补充：
```dockerfile
ENV CHROME_USE_EXISTING=true \
    CHROME_CDP_URL=http://127.0.0.1:9222
```

### 步骤 2：修复 launcher.py 支持 Linux Chrome

**文件**：[src/browser/launcher.py](file:///d:/projects/finance-ai/src/browser/launcher.py)（`_try_launch_cdp_chrome` 函数，第 46-88 行）

**改动**：在 `candidates` 列表追加 Linux Chrome 路径 + macOS 路径：

```python
candidates = [
    # Windows
    Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    # Linux (容器/宝塔)
    Path("/usr/bin/google-chrome-stable"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium-browser"),
    # macOS
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
]
```

Linux Chrome 启动参数需加 `--no-sandbox --headless=new`（容器内不能开 sandbox）。

### 步骤 3：新建宝塔部署文档

**文件**：[docs/DEPLOY_BAOTA.md](file:///d:/projects/finance-ai/docs/DEPLOY_BAOTA.md)（新建）

**内容**：

#### 3.1 前置条件
- 宝塔面板已安装（7.x+）
- 宝塔「Docker 管理器」插件已安装
- 服务器有公网 IP，域名已解析（可选 SSL）

#### 3.2 上传项目
```bash
# 方式 1：Git 克隆
cd /www/wwwroot
git clone <repo-url> finance-ai
cd finance-ai

# 方式 2：上传压缩包
# 宝塔文件管理 → 上传 → 解压到 /www/wwwroot/finance-ai
```

#### 3.3 配置 .env
```bash
cp .env.example .env
# 宝塔文件管理编辑 .env，必填项：
#   OPENAI_API_KEY=sk-...
#   WEWORK_CORP_ID / WEWORK_CORP_SECRET / WEWORK_AGENT_ID
#   WEWORK_EXTERNAL_CALLBACK_TOKEN / _AES_KEY
#   WEWORK_KF_SECRET / WEWORK_KF_OPEN_KFID
#   ADMIN_PASSWORD=<强密码>
#   DRY_RUN=true（生产先 dry-run）
#   BROWSER_HEADLESS=true
```

#### 3.4 Docker Compose 部署
```bash
# 宝塔终端
cd /www/wwwroot/finance-ai
docker compose up -d --build

# 带 RAG（可选）
docker compose --profile rag up -d --build

# 查看日志
docker compose logs -f bot
docker compose logs -f admin
```

#### 3.5 宝塔反向代理
1. 宝塔 → 网站 → 添加站点 → 域名（如 `bot.yourdomain.com`）→ 无需 PHP
2. 站点设置 → 反向代理 → 添加反向代理：
   - 目标 URL：`http://127.0.0.1:8081`（bot 回调）
   - 发送域名：`$host`
3. 再建一个站点 `admin.yourdomain.com` → 反向代理 → `http://127.0.0.1:8082`
4. SSL：站点设置 → SSL → Let's Encrypt 一键申请

#### 3.6 防火墙
- 宝塔安全 → 放行端口：8081、8082（仅本机，反向代理用）→ 实际只需放行 80/443
- 企微回调地址填：`https://bot.yourdomain.com/webhook`

#### 3.7 验证
```bash
# 健康检查
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8082/health

# 管理后台
# 浏览器打开 https://admin.yourdomain.com/admin
# 用 ADMIN_USERNAME / ADMIN_PASSWORD 登录
```

#### 3.8 常见问题
- **Playwright Chromium 启动失败**：容器内装了 google-chrome-stable，走 CDP 模式；若仍失败检查 `CHROME_USE_EXISTING=true`
- **企微回调 503**：检查 `.env` 的 `WEWORK_EXTERNAL_CALLBACK_TOKEN` / `_AES_KEY` 与企微后台一致
- **admin 登录 401**：`ADMIN_PASSWORD` 不能为空
- **ICRIS 注册被检测**：`DRY_RUN=true` 先验证填表；`CHROME_USE_EXISTING=true` + Chrome CDP 绕过 TLS 指纹
- **数据丢失**：`./data` 卷挂载，SQLite 在 `data/wework_external.db`，别删

---

## 改动文件清单

| 文件 | 操作 | 改动 |
|---|---|---|
| [Dockerfile](file:///d:/projects/finance-ai/Dockerfile) | 修改 | 装 google-chrome-stable + ENV 加 CHROME_USE_EXISTING |
| [src/browser/launcher.py](file:///d:/projects/finance-ai/src/browser/launcher.py) | 修改 | `_try_launch_cdp_chrome` 加 Linux Chrome 路径 + `--no-sandbox` |
| [docs/DEPLOY_BAOTA.md](file:///d:/projects/finance-ai/docs/DEPLOY_BAOTA.md) | 新建 | 宝塔部署完整步骤 |

---

## 假设与决策

1. **部署方式选 Docker Compose**：宝塔有 Docker 插件，docker-compose.yml 已就绪，比裸 Python + supervisord 更简单
2. **装 Google Chrome 而非用 Playwright Chromium**：ICRIS 门户检测 TLS 指纹，真实 Chrome CDP 绕过能力更强
3. **反向代理用宝塔自带 Nginx**：宝塔站点反向代理，不另起 Nginx 容器
4. **qdrant 可选**：`--profile rag` 才启动；不需要 RAG 时不启动
5. **数据卷 `./data` 挂载到宿主机**：SQLite + 材料文件都在 data/，备份只需备份这个目录
6. **不改动 docker-compose.yml**：现有 bot + admin 两容器配置已满足需求

---

## 验证步骤

### 1. 本地 Docker 构建验证
```powershell
docker compose build
docker compose up -d
docker compose ps   # 两个容器都 Up
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8082/health
```

### 2. 浏览器验证
- `http://127.0.0.1:8082/admin` → 登录页
- 登录后 → 概览 / 快速注册 / 外部群发消息 页面可访问

### 3. Chrome CDP 验证
```bash
docker compose exec bot python -c "
from src.browser.launcher import _try_launch_cdp_chrome
print('CDP launch:', _try_launch_cdp_chrome())
"
# 应输出 True（容器内 google-chrome-stable 启动成功）
```

### 4. 宝塔部署后验证
```bash
curl https://bot.yourdomain.com/health
curl https://admin.yourdomain.com/health
# 企微后台配置回调 URL → 建群 → bot 日志收到回调
```
