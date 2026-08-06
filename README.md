# 香港公司工商注册智能体

基于 OpenAI 大模型的香港公司注册全流程自动化智能体，覆盖企业微信客户对接、材料收集、ICRIS 系统注册与填表。

## 功能流程

| 步骤 | 命令 | 说明 |
|------|------|------|
| ① | `--step wework` | 进企微群，发送材料清单，AI 回答客户问题 |
| ② | `--step collect` | 搜集客户材料 |
| ② | `--step confirm` | 生成材料确认摘要发给客户 |
| ③ | `--step package` | 打包材料为以公司名命名的文件夹 |
| ④ | `--step register` | ICRIS 账号注册（浏览器自动填写，**不提交**） |
| ⑤ | `--step email` | 读取邮箱获取 ICRIS 账号密码 |
| ⑥ | `--step login` | 登录 ICRIS，填写公司注册材料 |
| ⑦ | `--step notify` | 提醒同事进行后续人工操作 |

## 快速开始

### 1. 安装依赖

**Windows（推荐）：**

```powershell
cd register-ai
.\setup.ps1
```

或手动安装：

```bash
# 若 pip 遇代理 SSL 错误，先执行:
# $env:NO_PROXY='*'   (PowerShell)

pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填写：

```env
OPENAI_API_KEY=your-key
OPENAI_API_BASE=https://ai-yyds.com/v1
OPENAI_MODEL=gpt-4o-mini
DRY_RUN=true
BROWSER_HEADLESS=false
```

企业微信和邮箱留空则自动使用 Mock 模式。

### 3. 运行

```bash
# 默认：仅运行步骤④ ICRIS 注册（Mock 数据，打开浏览器填写，不提交）
python main.py

# 仅运行 ICRIS 注册
python main.py --step register

# 运行完整 7 步流程
python main.py --full

# 运行指定步骤
python main.py --step wework
python main.py --step package
python main.py --step login

# 查看所有步骤
python main.py steps
```

## 项目结构

```
register-ai/
├── main.py                 # CLI 入口
├── config/settings.py      # 配置管理
├── data/mock/              # Mock 注册数据
├── templates/              # 材料清单模板
├── output/                 # 打包输出目录
└── src/
    ├── agent/              # 智能体编排
    ├── llm/                # OpenAI 客户端
    ├── wework/             # 企业微信 API
    ├── materials/          # 材料打包
    ├── email/              # 邮箱读取
    ├── browser/            # Playwright 浏览器自动化
    └── workflow/           # 工作流步骤
```

## Mock 模式

未配置企业微信 / 邮箱凭证时，系统自动进入 Mock 模式：
- 企微消息打印到日志
- 邮箱返回 Mock ICRIS 账号
- 注册数据来自 `data/mock/company_registration.json`

## 浏览器步骤（④⑥）

ICRIS 网站加载 `disable-devtool.min.js`，检测到 Playwright/CDP 后会**强制跳转到** `cr.gov.hk/tc/home/index.htm`。  
本项目已自动拦截该脚本，正常流程：

```
e-services 门户 → 立即登记 → registration/s01.do
```

```powershell
# 推荐使用项目虚拟环境（含 ddddocr 验证码识别）
.\.venv\Scripts\pip install -r requirements.txt
run.bat --step register

# 或
.\.venv\Scripts\python.exe main.py --step register
```

ICRIS 验证码为 `data:image/gif` + 输入框 `#checkCode`，需 **ddddocr** 识别（LLM 视觉在当前 API 代理下不可用）。

## 生产入口（L1 客服机器人）

试点推荐使用统一入口（回调 + 微信客服轮询 + 状态机）：

```bash
python main.py wework-external-bot
```

运维清单见 [`docs/PRODUCTION_CHECKLIST.md`](docs/PRODUCTION_CHECKLIST.md)；systemd / Nginx / **Docker** 样例见 [`deploy/`](deploy/)（Docker 详见 [`deploy/DOCKER.md`](deploy/DOCKER.md)）。

```bash
# Docker：bot(:8081) + admin(:8082)，共享 ./data
docker compose up -d --build
```

探活：`GET /health`（或 `/healthz`），含近 24h 回答质量与注册成败聚合字段。

**双进程：**

| 角色 | 命令 | 默认地址 |
|------|------|----------|
| Agent（回调 / KF / ICRIS） | `python main.py wework-external-bot` | `:8081` `/health` |
| 管理后台 | `python main.py admin` | `:8082` `/admin` |

1. 构建前端（若尚未构建）：`cd web/admin && npm install && npm run build`
2. `.env`：`ADMIN_USERNAME`、`ADMIN_PASSWORD`（必填）、可选 `ADMIN_PORT=8082`
3. 另开终端启动后台：`python main.py admin`
4. 浏览器打开 `http://127.0.0.1:8082/admin` — 登录页填写 `ADMIN_USERNAME` / `ADMIN_PASSWORD`（Cookie 会话）

勿将 `/admin` 裸暴露公网；细则见 [`docs/PRODUCTION_CHECKLIST.md`](docs/PRODUCTION_CHECKLIST.md) 与 [`web/admin/README.md`](web/admin/README.md)。

## 注意事项

- `DRY_RUN=true` 时不会点击 ICRIS 最终提交；真实提交还需 `ICRIS_ALLOW_SUBMIT=true`
- ICRIS 网站表单结构可能变化，浏览器自动化使用多策略选择器
- 验证码识别优先 ddddocr，失败时回退 LLM 视觉模型
- 改知识文档后需执行 `python main.py rag-ingest`
- **请勿将 `.env` 提交到版本控制**

```bash
python main.py feishu-bot
python main.py --step register
python main.py wework-external-bot
```
