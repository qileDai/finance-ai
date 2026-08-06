# Docker 部署

同一镜像跑两个进程：**bot**（企微回调 / KF / ICRIS）与 **admin**（管理后台），共享 `./data` 卷中的 SQLite 与材料目录。

| 服务 | 端口 | 说明 |
|------|------|------|
| `bot` | 8081 | `python main.py wework-external-bot` |
| `admin` | 8082 | `python main.py admin` → `/admin` 登录页 |
| `qdrant` | 6333 | 可选，`docker compose --profile rag` |

## 前置

1. 安装 Docker / Docker Compose
2. 项目根目录准备 `.env`（可从 `.env.example` 复制），至少设置：
   - `ADMIN_PASSWORD`（管理后台登录）
   - 企微 / 客服相关密钥
   - `WEWORK_EXTERNAL_CALLBACK_PORT=8081`
   - `ADMIN_PORT=8082`
   - `MATERIALS_DIR=data/materials`
   - `BROWSER_HEADLESS=true`（容器内推荐）
3. 若启用 RAG profile，在 `.env` 中设：`QDRANT_URL=http://qdrant:6333`

**不要**把含密钥的 `.env` 打进镜像；compose 以只读方式挂载。

## 启动

```bash
# 构建并后台启动 bot + admin
docker compose up -d --build

# 同时启动 Qdrant（RAG）
docker compose --profile rag up -d --build
```

探活：

- Agent：`http://127.0.0.1:8081/health`
- Admin：`http://127.0.0.1:8082/health`
- 管理后台：`http://127.0.0.1:8082/admin`

## 数据持久化

宿主机目录 `./data` 挂载到容器 `/app/data`：

- `data/wework_external.db` — 会话 / 队列 / 质量统计
- `data/materials/` — 证件与材料文件

重启或重建容器不会清空该目录（勿把 `data/` 提交到 git）。

## Nginx

宿主机 Nginx 仍可按 [`nginx-wework.conf.example`](nginx-wework.conf.example)：

- `/webhook`、`/health` → `127.0.0.1:8081`
- `/admin` → `127.0.0.1:8082`（建议 IP 白名单）

与 Docker 端口映射一致。

## 常用命令

```bash
docker compose logs -f bot
docker compose logs -f admin
docker compose restart bot admin
docker compose down          # 不停删 ./data
```

知识入库（需容器内网络可达 Qdrant）：

```bash
docker compose exec bot python main.py rag-ingest
```

## 构建说明

- 多阶段构建：先用 Node 打 `static/admin`，再打 Python 运行时
- 镜像内已安装 Playwright Chromium（ICRIS 自动化）；体积较大属预期
- 默认 `CMD` 为 bot；admin 服务在 compose 中覆盖 `command`
