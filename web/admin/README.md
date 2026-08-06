# Finance AI Admin

React + Vite 运营后台。构建产物输出到仓库根目录 `static/admin/`。

与 Agent **分进程**启动：

| 进程 | 命令 | 默认端口 |
|------|------|----------|
| Agent | `python main.py wework-external-bot` | 8081 |
| 本后台 | `python main.py admin` | 8082（`ADMIN_PORT`） |

## 开发

```bash
cd web/admin
npm install
npm run dev
```

开发服默认 `http://127.0.0.1:5173/admin/`，API 代理到 `ADMIN_PORT`（默认 8082，需先 `python main.py admin`）。

## 构建与生产访问

```bash
cd web/admin
npm install
npm run build

# 另开终端
python main.py admin
```

浏览器：`http://127.0.0.1:8082/admin` → 登录页（`ADMIN_USERNAME` / `ADMIN_PASSWORD`，Cookie 会话）。
