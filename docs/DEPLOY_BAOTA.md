# 宝塔面板部署 finance-ai（Docker 方式）

## 架构概览

```
公网 443/80
    │
    ▼
宝塔 Nginx 反向代理
    ├── bot.yourdomain.com   → 127.0.0.1:8081  (企微回调 + AI 回复)
    └── admin.yourdomain.com → 127.0.0.1:8082  (管理后台 SPA + API)
                                    │
                            ┌───────┴───────┐
                            ▼               ▼
                     Docker 容器 bot    Docker 容器 admin
                     (wework-external-bot)    (admin)
                            │
                            ▼
                     Google Chrome CDP (9222)
                     + Playwright（ICRIS 注册填表）
```

- **bot 容器**：`python main.py wework-external-bot`，端口 8081，企微外部群回调 + AI 自动回复 + ICRIS 注册
- **admin 容器**：`python main.py admin`，端口 8082，React SPA + `/admin/api` + ICRIS Worker
- **qdrant 容器**（可选）：RAG 知识库向量检索
- **共享数据卷**：`./data`（SQLite + 材料文件 + RAG DB）

---

## 前置条件

1. **宝塔面板**已安装（7.x+，推荐 aaPanel 国际版或宝塔国内版）
2. **Docker 管理器**插件已安装：宝塔后台 → 软件商店 → 搜索「Docker」→ 安装
3. 服务器有**公网 IP**，两个域名已解析到该 IP：
   - `bot.yourdomain.com`（企微回调用）
   - `admin.yourdomain.com`（管理后台用）
4. 服务器配置：≥2 核 CPU、≥4G 内存、≥40G 磁盘

---

## 部署步骤

### 1. 上传项目

```bash
# 方式 1：Git 克隆（推荐）
cd /www/wwwroot
git clone <你的仓库地址> finance-ai
cd finance-ai

# 方式 2：宝塔文件管理上传
# 本地打包项目（排除 .venv/node_modules/data）→ 宝塔文件管理 → 上传 → 解压到 /www/wwwroot/finance-ai
```

### 2. 配置 .env

```bash
cp .env.example .env
```

宝塔文件管理编辑 `/www/wwwroot/finance-ai/.env`，**必填项**：

```env
# === OpenAI ===
OPENAI_API_KEY=sk-你的key
OPENAI_API_BASE=https://ai-yyds.com/v1
OPENAI_MODEL=gpt-4o-mini

# === 企业微信（自建应用）===
WEWORK_CORP_ID=ww你的企业ID
WEWORK_CORP_SECRET=你的自建应用Secret
WEWORK_AGENT_ID=你的AgentId

# === 外部群回调 ===
WEWORK_EXTERNAL_CALLBACK_TOKEN=你的回调Token
WEWORK_EXTERNAL_CALLBACK_AES_KEY=你的回调EncodingAESKey
WEWORK_EXTERNAL_CALLBACK_PORT=8081

# === 微信客服（kf 私聊模式）===
WEWORK_KF_SECRET=你的客服Secret
WEWORK_KF_OPEN_KFID=wk你的客服ID

# === 管理后台 ===
ADMIN_USERNAME=admin
ADMIN_PASSWORD=你的强密码
ADMIN_PORT=8082

# === 浏览器（容器内 Chrome CDP）===
CHROME_USE_EXISTING=true
CHROME_CDP_URL=http://127.0.0.1:9222
BROWSER_HEADLESS=true

# === ICRIS 注册（先 dry-run）===
DRY_RUN=true
ICRIS_ALLOW_SUBMIT=false

# === 材料存储 ===
MATERIALS_DIR=data/materials
MATERIALS_DEFAULT_CONTACT_EMAIL=你的联系邮箱

# === RAG（可选，需要 qdrant）===
# RAG_ENABLED=true
# QDRANT_URL=http://qdrant:6333
```

### 3. Docker Compose 构建启动

```bash
cd /www/wwwroot/finance-ai

# 构建并启动（bot + admin）
docker compose up -d --build

# 如果需要 RAG 知识库（可选）
docker compose --profile rag up -d --build

# 查看容器状态
docker compose ps

# 查看日志
docker compose logs -f bot
docker compose logs -f admin
```

首次构建约 5-10 分钟（下载 Python/Node/Chrome 镜像 + pip/npm 安装）。

### 4. 宝塔反向代理

#### 4.1 bot 反向代理

1. 宝塔 → **网站** → **添加站点**
   - 域名：`bot.yourdomain.com`
   - PHP 版本：纯静态
   - 数据库：不创建
2. 站点设置 → **反向代理** → **添加反向代理**
   - 代理名称：`bot`
   - 目标 URL：`http://127.0.0.1:8081`
   - 发送域名：`$host`
   - 启用反代：✅

#### 4.2 admin 反向代理

1. 宝塔 → **网站** → **添加站点**
   - 域名：`admin.yourdomain.com`
   - PHP 版本：纯静态
2. 站点设置 → **反向代理** → **添加反向代理**
   - 代理名称：`admin`
   - 目标 URL：`http://127.0.0.1:8082`
   - 发送域名：`$host`
   - 启用反代：✅

### 5. SSL 证书

1. 站点设置 → **SSL** → **Let's Encrypt**
2. 勾选域名 → **申请**
3. 申请成功后开启**强制 HTTPS**

> 企微回调要求 HTTPS，必须配置 SSL。

### 6. 防火墙

- 宝塔 → **安全** → 放行端口：`80`、`443`
- **不要**对外暴露 8081/8082（仅本机反向代理访问）
- 云服务器安全组同样只放行 80/443

### 7. 企微后台配置

1. 企微管理后台 → 应用管理 → 自建应用 → 你的应用
2. **接收消息** → API 接收消息 → URL 填：`https://bot.yourdomain.com/webhook`
3. Token / EncodingAESKey 与 `.env` 一致
4. 客户联系 → API → 接收事件服务器 → 同样填 `https://bot.yourdomain.com/webhook`
5. 勾选事件：**客户群变更** `change_external_chat`

---

## 验证

### 健康检查

```bash
# 容器内
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8082/health

# 公网（配置完反代+SSL 后）
curl https://bot.yourdomain.com/health
curl https://admin.yourdomain.com/health
```

应返回 `{"ok": true, ...}`。

### 管理后台

浏览器打开 `https://admin.yourdomain.com/admin`：
- 用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录
- 概览页 → 可见容器状态
- 快速注册 → 可填表上传证件
- 外部群发消息 → 可测试发消息

### Chrome CDP 验证

```bash
docker compose exec bot python -c "
from src.browser.launcher import _try_launch_cdp_chrome
print('CDP launch:', _try_launch_cdp_chrome())
"
# 应输出 True
```

### 企微回调验证

1. 在企微里建一个客户群（拉一个外部联系人入群）
2. 查看日志：
```bash
docker compose logs -f bot | grep "客户群事件"
```
3. 应看到 `客户群事件 create chat_id=wrXXXX` + 欢迎语发送日志

---

## 日常运维

### 查看日志

```bash
# 实时日志
docker compose logs -f bot
docker compose logs -f admin

# 最近 100 行
docker compose logs --tail 100 bot
```

### 重启服务

```bash
cd /www/wwwroot/finance-ai
docker compose restart bot
docker compose restart admin
```

### 更新代码

```bash
cd /www/wwwroot/finance-ai
git pull
docker compose up -d --build
```

### 备份数据

```bash
# 备份 data 目录（SQLite + 材料文件 + RAG DB）
tar -czf finance-ai-data-$(date +%Y%m%d).tar.gz data/
```

### 停止服务

```bash
docker compose down
# 停止并删除卷（⚠️ 会删数据，谨慎）
# docker compose down -v
```

---

## 常见问题

### Q：docker compose build 失败（Chrome 安装报错）

```
E: Failed to fetch http://dl.google.com/linux/chrome/deb/...
```

服务器在中国大陆，Google 源不可达。解决：
1. 换用阿里云 Chrome 镜像源
2. 或 Dockerfile 注释掉 Chrome 安装，用 Playwright 自带 Chromium（`CHROME_USE_EXISTING=false`）

### Q：bot 容器健康检查失败

```bash
docker compose logs bot | tail -50
```

常见原因：
- `.env` 缺少 `WEWORK_CORP_ID` / `WEWORK_CORP_SECRET` → bot 启动但回调 503
- 端口被占用 → `netstat -tlnp | grep 8081`

### Q：admin 登录 401

`ADMIN_PASSWORD` 不能为空。编辑 `.env` 填密码后 `docker compose restart admin`。

### Q：ICRIS 注册被检测为自动化

1. 确认 `.env`：`CHROME_USE_EXISTING=true`（走真实 Chrome CDP）
2. 确认 `BROWSER_HEADLESS=true`（容器无显示）
3. `DRY_RUN=true` 先验证填表不提交
4. 容器内验证 Chrome 启动：
```bash
docker compose exec bot google-chrome-stable --version
```

### Q：企微回调 503 / Token 验证失败

1. 确认 `.env` 的 `WEWORK_EXTERNAL_CALLBACK_TOKEN` / `_AES_KEY` 与企微后台**完全一致**
2. 确认回调 URL 填的是 `https://bot.yourdomain.com/webhook`（不是 `/wework/external/callback`）
3. 确认 SSL 证书有效（企微不信任自签名证书）

### Q：数据丢失

`./data` 目录挂载到宿主机，SQLite 在 `data/wework_external.db`。如果数据丢失：
1. 检查 `docker compose down` 是否误加了 `-v`（会删卷）
2. 检查 `./data` 目录权限：`chown -R 1000:1000 data/`

### Q：内存不足（OOM）

Chrome + Playwright 占内存大。≥4G 内存推荐；2G 服务器加 swap：
```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
```

---

## 端口对照表

| 端口 | 服务 | 容器 | 对外 | 说明 |
|---|---|---|---|---|
| 8081 | bot HTTP | finance-ai-bot | ❌ 仅本机 | 企微回调 + AI 回复 |
| 8082 | admin HTTP | finance-ai-admin | ❌ 仅本机 | 管理后台 SPA + API |
| 9222 | Chrome CDP | bot 容器内 | ❌ 容器内部 | ICRIS 注册浏览器 |
| 6333 | qdrant | finance-ai-qdrant | ❌ 仅本机 | RAG 向量库（可选） |
| 80 | Nginx HTTP | 宝塔宿主机 | ✅ 公网 | 重定向到 443 |
| 443 | Nginx HTTPS | 宝塔宿主机 | ✅ 公网 | 反代到 8081/8082 |
