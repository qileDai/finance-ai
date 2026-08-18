# 宝塔 Docker 部署方案（Git 拉取部署）

## 概要

从 Git 仓库 `https://github.com/qileDai/finance-ai.git` 拉取最新代码，在宝塔面板上用 Docker Compose 构建部署。包含 bot(8081) + admin(8082) 两个容器，可选 qdrant 向量库。

## 架构

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
                     Google Chrome (容器内 headless)
                     + Playwright（ICRIS 注册填表）
```

## 现有文件状态

| 文件 | 状态 | 说明 |
|---|---|---|
| [Dockerfile](file:///d:/projects/finance-ai/Dockerfile) | ✅ 完整 | 多阶段构建：Node 前端 → Python 运行时 + Chrome + Playwright |
| [docker-compose.yml](file:///d:/projects/finance-ai/docker-compose.yml) | ✅ 完整 | bot(8081) + admin(8082) + qdrant(可选) |
| [.dockerignore](file:///d:/projects/finance-ai/.dockerignore) | ✅ 完整 | 排除 .git/.venv/data/node_modules 等 |
| [.env.example](file:///d:/projects/finance-ai/.env.example) | ✅ 完整 | 全部配置项有注释 |
| [requirements.txt](file:///d:/projects/finance-ai/requirements.txt) | ✅ 完整 | 17 个依赖 |
| Git remote | ✅ | `origin https://github.com/qileDai/finance-ai.git` |

**结论：Docker 文件齐全，无需修改。只需在宝塔上执行部署流程。**

---

## 部署步骤

### 第 1 步：服务器环境准备

```bash
# 1. 安装宝塔面板（已安装可跳过）
curl -sSO https://download.bt.cn/install/install_panel.sh && bash install_panel.sh

# 2. 宝塔后台 → 软件商店 → 搜索安装：
#    - Docker 管理器（必须）
#    - Nginx（一般宝塔自带）

# 3. 确认 Docker 可用
docker --version
docker compose version
```

### 第 2 步：从 Git 克隆项目

```bash
# 创建项目目录
mkdir -p /www/wwwroot/finance-ai
cd /www/wwwroot/finance-ai

# 克隆仓库
git clone https://github.com/qileDai/finance-ai.git .
```

> 如果仓库是私有的，需要先配置 SSH key 或使用 token：
> `git clone https://<token>@github.com/qileDai/finance-ai.git .`

### 第 3 步：配置 .env

```bash
cd /www/wwwroot/finance-ai
cp .env.example .env
```

**必填配置项**（用宝塔文件管理器或 `vi` 编辑）：

```env
# === OpenAI ===
OPENAI_API_KEY=sk-你的key
OPENAI_API_BASE=https://ai-yyds.com/v1
OPENAI_MODEL=gpt-4o-mini
OPENAI_VISION_MODEL=gpt-4o

# === 企业微信 ===
WEWORK_CORP_ID=ww你的企业ID
WEWORK_CORP_SECRET=你的自建应用Secret
WEWORK_AGENT_ID=你的AgentId
WEWORK_EXTERNAL_CALLBACK_TOKEN=你的回调Token
WEWORK_EXTERNAL_CALLBACK_AES_KEY=你的回调EncodingAESKey
WEWORK_EXTERNAL_CALLBACK_PORT=8081
WEWORK_KF_SECRET=你的客服Secret
WEWORK_KF_OPEN_KFID=wk你的客服ID

# === 管理后台 ===
ADMIN_USERNAME=admin
ADMIN_PASSWORD=你的强密码
ADMIN_PORT=8082

# === 浏览器（容器内 headless Chrome）===
BROWSER_HEADLESS=true
CHROME_USE_EXISTING=false

# === ICRIS 注册（先 dry-run）===
DRY_RUN=true
ICRIS_ALLOW_SUBMIT=false

# === 材料存储 ===
MATERIALS_DIR=data/materials
MATERIALS_DEFAULT_CONTACT_EMAIL=你的联系邮箱

# === RAG（可选）===
RAG_ENABLED=true
QDRANT_URL=http://qdrant:6333
```

### 第 4 步：Docker 构建启动

```bash
cd /www/wwwroot/finance-ai

# 构建并启动 bot + admin（首次约 5-10 分钟）
docker compose up -d --build

# 如需 RAG 知识库
docker compose --profile rag up -d --build

# 查看容器状态
docker compose ps

# 查看日志
docker compose logs -f bot
docker compose logs -f admin
```

### 第 5 步：宝塔 Nginx 反向代理

#### bot 站点
1. 宝塔 → **网站** → **添加站点**
   - 域名：`bot.yourdomain.com`
   - PHP 版本：纯静态
2. 站点设置 → **反向代理** → **添加反向代理**
   - 代理名称：`bot`
   - 目标 URL：`http://127.0.0.1:8081`
   - 发送域名：`$host`

#### admin 站点
1. 宝塔 → **网站** → **添加站点**
   - 域名：`admin.yourdomain.com`
   - PHP 版本：纯静态
2. 站点设置 → **反向代理** → **添加反向代理**
   - 代理名称：`admin`
   - 目标 URL：`http://127.0.0.1:8082`
   - 发送域名：`$host`

### 第 6 步：SSL 证书

1. 站点设置 → **SSL** → **Let's Encrypt**
2. 勾选域名 → **申请**
3. 申请成功后开启**强制 HTTPS**

> 企微回调要求 HTTPS，必须配置 SSL。

### 第 7 步：防火墙

- 宝塔 → **安全** → 放行端口：`80`、`443`
- **不要**对外暴露 8081/8082（仅本机反向代理访问）
- 云服务器安全组同样只放行 80/443

### 第 8 步：企微后台配置

1. 企微管理后台 → 应用管理 → 自建应用
2. **接收消息** → URL 填：`https://bot.yourdomain.com/webhook`
3. Token / EncodingAESKey 与 `.env` 一致
4. 客户联系 → API → 接收事件服务器 → 同样填 `https://bot.yourdomain.com/webhook`
5. 勾选事件：**客户群变更** `change_external_chat`

---

## 更新部署流程（日常迭代）

```bash
cd /www/wwwroot/finance-ai

# 1. 拉取最新代码
git pull origin main

# 2. 重新构建并启动（只重建变化的层）
docker compose up -d --build

# 3. 检查状态
docker compose ps
docker compose logs --tail 50 bot
docker compose logs --tail 50 admin
```

### 一键部署脚本（可选）

在项目根目录创建 `deploy.sh`：

```bash
#!/bin/bash
set -e
cd /www/wwwroot/finance-ai

echo "=== 拉取最新代码 ==="
git pull origin main

echo "=== 重新构建并启动 ==="
docker compose up -d --build

echo "=== 等待健康检查 ==="
sleep 10
docker compose ps

echo "=== 部署完成 ==="
echo "bot:   $(docker compose port bot 8081)"
echo "admin: $(docker compose port admin 8082)"
```

```bash
chmod +x deploy.sh
./deploy.sh
```

---

## 验证

### 健康检查
```bash
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8082/health
# 应返回 {"ok": true, ...}

# 公网验证
curl https://bot.yourdomain.com/health
curl https://admin.yourdomain.com/health
```

### 管理后台
浏览器打开 `https://admin.yourdomain.com/admin`，用 `.env` 中的账号密码登录。

### Chrome CDP 验证
```bash
docker compose exec bot google-chrome-stable --version
```

### 企微回调验证
```bash
docker compose logs -f bot | grep "客户群事件"
```

---

## 日常运维

| 操作 | 命令 |
|---|---|
| 查看实时日志 | `docker compose logs -f bot` |
| 重启 bot | `docker compose restart bot` |
| 重启 admin | `docker compose restart admin` |
| 停止全部 | `docker compose down` |
| 备份数据 | `tar -czf finance-ai-data-$(date +%Y%m%d).tar.gz data/` |
| 更新部署 | `git pull && docker compose up -d --build` |

---

## 常见问题

### Chrome 安装失败（中国大陆服务器）
Dockerfile 从 `dl.google.com` 安装 Chrome，大陆服务器不可达。解决：
1. 服务器配置代理
2. 或修改 Dockerfile 使用阿里云镜像源
3. 或注释 Chrome 安装，用 Playwright 自带 Chromium（`.env` 设 `CHROME_USE_EXISTING=false`）

### 内存不足（OOM）
Chrome + Playwright 需要 ≥4G 内存。2G 服务器加 swap：
```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
```

### 企微回调 503
1. 确认 `.env` 的 Token / AES_KEY 与企微后台一致
2. 确认回调 URL 是 `https://bot.yourdomain.com/webhook`
3. 确认 SSL 证书有效

---

## 端口对照表

| 端口 | 服务 | 对外 | 说明 |
|---|---|---|---|
| 8081 | bot | ❌ 仅本机 | 企微回调 + AI 回复 |
| 8082 | admin | ❌ 仅本机 | 管理后台 |
| 6333 | qdrant | ❌ 仅本机 | RAG 向量库（可选） |
| 80 | Nginx | ✅ 公网 | 重定向到 443 |
| 443 | Nginx | ✅ 公网 | 反代到 8081/8082 |
