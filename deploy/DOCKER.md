# Docker + 宝塔部署

同一镜像跑两个进程：**bot**（企微回调 / KF / ICRIS）与 **admin**（管理后台），共享项目目录下 `./data`。

推荐在 **宝塔面板** 中：Docker 编排起容器 + 网站反向代理 + SSL。不必手改系统 Nginx 为主。

**代码更新后如何重新部署** → 见 [OPS.md](OPS.md)。

| 服务 | 本机端口 | 说明 |
|------|----------|------|
| `bot` | 8081 | `python main.py wework-external-bot` |
| `admin` | 8082 | `python main.py admin` → `/admin` |
| `qdrant` | 6333 | 可选，`docker compose --profile rag` |

公网只开放 **80 / 443**；8081、8082 仅本机给宝塔 Nginx 反代即可。

---

## 一、宝塔准备

1. 安装 **Docker**（软件商店 → Docker）
2. 建议云主机内存 **≥ 4GB**（镜像含 Playwright Chromium，构建与 ICRIS 较吃内存）
3. 防火墙 / 安全组放行 **80、443**
4. 将代码放到例如：`/www/wwwroot/finance-ai`（上传或 `git clone`）

---

## 二、环境变量

```bash
cd /www/wwwroot/finance-ai
cp .env.example .env          # 或参考 deploy/env.docker.example
# 编辑 .env：至少设置 ADMIN_PASSWORD、企微密钥、LLM Key 等
mkdir -p data/materials
```

容器内推荐（compose 已写入部分）：

- `BROWSER_HEADLESS=true`
- `CHROME_USE_EXISTING=false`（不要复用宿主机 Chrome）
- `WEWORK_EXTERNAL_CALLBACK_PORT=8081`
- `ADMIN_PORT=8082`
- `MATERIALS_DIR=data/materials`
- 启用 RAG 时：`QDRANT_URL=http://qdrant:6333`

**不要**把含密钥的 `.env` 打进镜像；compose 以只读方式挂载。

---

## 三、启动容器

在项目目录：

```bash
cd /www/wwwroot/finance-ai
docker compose up -d --build
```

或宝塔：**Docker → 编排**，选择本目录的 `docker-compose.yml` 启动。

注意：

- 首次构建较久，属正常
- compose 已设 `shm_size: "2gb"`（Chromium 需要）。若宝塔编排 UI **不识别** `shm_size`，请用终端执行上面的 `docker compose up -d --build`
- 同时启 Qdrant：`docker compose --profile rag up -d --build`

探活（本机）：

```bash
curl -fsS http://127.0.0.1:8081/health
curl -fsS http://127.0.0.1:8082/health
```

---

## 四、宝塔网站反向代理 + SSL

1. 宝塔 → **网站** → 添加站点（绑定你的域名，如 `szyingtai.cn`）
2. 站点 → **SSL** → Let's Encrypt 申请并强制 HTTPS
3. 站点 → **反向代理**（或「配置文件」）按下面规则配置

反代目标必须用 **`127.0.0.1`**（宝塔 Nginx 在宿主机，不要用 Docker 容器名）。

| 路径 | 目标 | 用途 |
|------|------|------|
| `/webhook` | `http://127.0.0.1:8081` | 企微回调 |
| `/wework/external/callback` | `http://127.0.0.1:8081` | 企微回调（备用路径） |
| `/health` | `http://127.0.0.1:8081` | 探活 |
| `/admin` | `http://127.0.0.1:8082` | 管理后台（建议加 IP 白名单） |

### 配置文件粘贴示例（可并入站点配置）

```nginx
# 企微回调
location /webhook {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}

location /wework/external/callback {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /health {
    proxy_pass http://127.0.0.1:8081;
    access_log off;
}

# 管理后台（建议取消注释 allow/deny 限制来源 IP）
location /admin {
    # allow 你的办公网段;
    # deny all;
    proxy_pass http://127.0.0.1:8082;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

更完整的独立 Nginx 样例见 [`nginx-wework.conf.example`](nginx-wework.conf.example)。

4. 企微后台回调 URL 填：`https://你的域名/webhook`
5. 验证：
   - `https://你的域名/health`
   - `https://你的域名/admin`（`ADMIN_USERNAME` / `ADMIN_PASSWORD`）

---

## 五、数据与运维

`./data` 挂载到容器 `/app/data`：

- `data/wework_external.db` — 会话 / 队列
- `data/materials/` — 证件与材料

备份该目录即可；`docker compose down` **不会**删除 `./data`。

```bash
docker compose logs -f bot
docker compose logs -f admin
docker compose restart bot admin
docker compose ps
```

知识入库（需 `--profile rag` 且 `.env` 中 `QDRANT_URL=http://qdrant:6333`）：

```bash
docker compose exec bot python main.py rag-ingest
```

### 会话存档（可选）

若使用企微会话存档，将官方 **Linux** `libWeWorkFinanceSdk.so` 放到 `vendor/wework-sdk/`，并在 compose 中取消 SDK volume 注释，设置 `WEWORK_ARCHIVE_SDK_PATH`。Windows 的 `.dll` 不能在 Linux 容器使用。

---

## 六、构建说明

- 多阶段：Node 打 `static/admin` → Python 运行时 + Playwright Chromium（体积大属预期）
- 默认 `CMD` 为 bot；admin 服务覆盖 `command`
- `shm_size: "2gb"` 为 ICRIS 浏览器自动化所必需
