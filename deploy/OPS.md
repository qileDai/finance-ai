# 运维手册：代码更新后如何重新部署

适用环境：宝塔 + Docker Compose（`bot` 8081、`admin` 8082）。  
首次部署见 [DOCKER.md](DOCKER.md)。

项目目录默认：`/www/wwwroot/finance-ai`（按你服务器实际路径替换）。

---

## 日常更新（最常用）

代码有改动、需要重新上线时，在**服务器项目目录**执行：

```bash
cd /www/wwwroot/finance-ai

# 1. 拉最新代码（用 git 时）
git pull

# 2. 重建并后台启动（会用新代码重新 build 镜像）
docker compose up -d --build

# 3. 看状态
docker compose ps
```

首次构建很慢；日常更新也会再 build，通常几分钟。  
**反代 / SSL / 域名一般不用动**（除非改了端口或路径）。

管理后台前端打进镜像：改了 `web/admin` 后**必须** `--build`，否则接口已返回 `{level,message}` 对象、旧 JS 仍用 `messages.join` 时，步骤日志会整页变成 `[object Object]`。上线后对 `/admin/` 做一次 **Ctrl+F5**。

### 验收

```bash
# 本机（推荐，避免服务器 curl 公网域名出现 000）
curl -fsS http://127.0.0.1:8081/health
curl -fsS http://127.0.0.1:8082/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8082/admin/

# 经本机 Nginx 验反代（把 Host 换成你的域名）
curl -sk -o /dev/null -w "%{http_code}\n" -H "Host: www.szyingtai.cn" https://127.0.0.1/admin/
curl -sk -o /dev/null -w "%{http_code}\n" -H "Host: www.szyingtai.cn" https://127.0.0.1/health
```

浏览器（自己电脑）：

- `https://你的域名/health`
- `https://你的域名/admin/`

---

## 只改了配置（.env），没改代码

```bash
cd /www/wwwroot/finance-ai
# 用宝塔文件管理或 nano 编辑 .env 后：
docker compose up -d
# 若改了端口/关键环境变量仍异常，可强制重建容器：
docker compose up -d --force-recreate
```

一般**不必** `--build`。

---

## 常用命令

```bash
cd /www/wwwroot/finance-ai

docker compose ps                 # 状态
docker compose logs -f bot        # bot 实时日志
docker compose logs -f admin      # admin 实时日志
docker compose logs --tail=200 bot
docker compose restart bot admin  # 仅重启，不重建镜像
docker compose down               # 停掉容器（不删 ./data）
docker compose up -d              # 再拉起
```

---

## 更新时注意

| 项 | 说明 |
|----|------|
| `./data` | 会话库、材料等，**不要删**；`down` / 重建镜像都不会清掉挂载数据 |
| `.env` | 留在服务器，**不要**提交到 Git；更新代码后检查是否有新增必填变量 |
| `shm_size` | 已在 compose 里；若用宝塔「编排」UI 异常，继续用终端 `docker compose` |
| 反代 | 目标保持 `http://127.0.0.1:8081` / `8082`，**末尾不要多 `/`**，否则 `/admin` 会重定向死循环 |
| 公网 curl `000` | 服务器上 curl 自己域名常失败；用本机 `127.0.0.1` 或电脑浏览器测 |

---

## 回滚（可选）

若新版本有问题，且用 git：

```bash
cd /www/wwwroot/finance-ai
git log --oneline -10          # 找到上一好用的 commit
git checkout <commit或tag>
docker compose up -d --build
```

回滚后若要再跟远程分支：`git checkout main && git pull`（分支名按实际）。

---

## 反代路径备忘（一般不用改）

| 路径 | 目标 |
|------|------|
| `/webhook` | `http://127.0.0.1:8081` |
| `/wework/external/callback` | `http://127.0.0.1:8081` |
| `/health` | `http://127.0.0.1:8081` |
| `/admin` | `http://127.0.0.1:8082` |

企微回调：`https://你的域名/webhook`

---

## 一句话

```text
cd 项目目录 → git pull → docker compose up -d --build → curl 本机 8081/8082 /health → 浏览器打开 /admin/
```
