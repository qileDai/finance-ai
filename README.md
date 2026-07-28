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

## 注意事项

- `DRY_RUN=true` 时不会点击提交按钮
- ICRIS 网站表单结构可能变化，浏览器自动化使用多策略选择器
- 验证码识别优先 ddddocr，失败时回退 LLM 视觉模型
- **请勿将 `.env` 提交到版本控制**

python main.py feishu-bot
python main.py --step register
