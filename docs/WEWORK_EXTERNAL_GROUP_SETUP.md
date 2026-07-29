# 企业微信外部群 AI 客服 — 配置与自动回复指南

本文说明如何在**外部客户群**中启用 Phase 1 能力：建群自动欢迎、材料清单推送、AI 文本问答、转人工。

---

## 一、能力说明

| 功能 | 触发方式 |
|------|----------|
| 欢迎语 + 材料清单 | 企业成员创建客户群 → 企微回调 `change_external_chat` |
| AI 解答材料问题 | 客户在群内发文字 → **会话内容存档**拉取（live）或 Mock 注入 |
| 重发清单 | 客户发送 `/资料` |
| 转人工 | 客户发送 `转人工` |

**不需要** 将应用发布到企业微信应用市场。

---

## 二、管理后台配置（按顺序）

### 1. 开通客户联系

1. 登录 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame)
2. **客户联系** → **配置** → 启用
3. 设置**使用范围**（群主、客服、运营人员）

### 2. 自建应用与 API 权限

1. **应用管理** → **自建** → 创建或选择应用
2. 记录 **AgentId**、**Secret** → 填入 `.env` 的 `WEWORK_AGENT_ID`、`WEWORK_CORP_SECRET`
3. **我的企业** → **企业信息** → **企业 ID** → `WEWORK_CORP_ID`
4. 在应用 **API 权限** 中开通：
   - 客户联系 - 客户群管理
   - 客户联系 - 客户基础信息
   - 通讯录（只读）
   - 发送应用消息（转人工通知群主）

### 3. 配置客户联系回调（建群欢迎语）

1. 应用 → **客户联系** → **API** → **接收事件服务器**
2. 填写：
   - **URL**：`https://<你的公网域名>/wework/external/callback`
   - **Token**：与 `.env` 中 `WEWORK_EXTERNAL_CALLBACK_TOKEN` 一致（可与 `WEWORK_TOKEN` 相同）
   - **EncodingAESKey**：与 `WEWORK_EXTERNAL_CALLBACK_AES_KEY` 一致
3. 保存前**必须先启动**本地 bot（见下文），否则 URL 验证失败
4. 勾选事件：**客户群变更**（`change_external_chat`）

### 4. 会话内容存档（live 模式收客户消息）

1. **管理工具** → **会话内容存档** → 开通并购买席位
2. 配置 **RSA 公钥**（用 openssl 生成密钥对，公钥上传，私钥本地保存）
3. 记录 **Secret** → `WEWORK_ARCHIVE_SECRET`
4. 私钥路径 → `WEWORK_ARCHIVE_PRIVATE_KEY_PATH`
5. 下载 **Finance SDK**：
   - Windows：`WeWorkFinanceSdk.dll`
   - Linux：`libWeWorkFinanceSdk.so`
6. 放到项目 `vendor/wework-sdk/`，或配置 `WEWORK_ARCHIVE_SDK_PATH`

### 5. 可见范围

**应用管理** → 你的应用 → **可见范围** → 选择群主/客服部门 → **保存**

### 6. 其他 `.env` 必填项

```env
WEWORK_CORP_ID=wwxxxxxxxx
WEWORK_CORP_SECRET=xxxxxxxx
WEWORK_AGENT_ID=1000002
WEWORK_DEFAULT_GROUP_OWNER_USERID=ZhangSan   # 群主 userid，发群消息用

OPENAI_API_KEY=sk-...
OPENAI_API_BASE=https://...
OPENAI_MODEL=gpt-4o-mini

WEWORK_EXTERNAL_MODE=auto   # 未配存档时 auto→mock；配齐后 auto→live
WEWORK_EXTERNAL_CALLBACK_PORT=8081
```

---

## 三、启动服务

```powershell
cd D:\projects\finance-ai
.\.venv\Scripts\python.exe main.py wework-external-bot
```

启动后会打印：

- 运行模式（mock / live）
- 回调端口与路径
- 已注册群数量
- 存档 SDK 是否找到

---

## 四、创建外部群并实现自动回复

### 流程概览

```
企业成员创建客户群
    → 企微推送 change_external_chat(create) 到回调 URL
    → 系统发送欢迎语 + 材料清单
客户在群内提问
    → live：存档 worker 拉取文本 → AI 回复发回群
    → mock：CLI 注入消息测试
客户发「转人工」
    → AI 暂停，通知群主
```

### 操作步骤

1. **启动 bot**（见上一节），确保回调 URL 公网可达
2. 企业成员打开 **企业微信** → **客户联系** → **创建客户群**
3. 拉入外部客户（微信联系人）
4. 约 **30 秒内**，群内应收到：
   - 欢迎说明
   - 《香港公司注册材料清单》
5. 客户在群内输入问题，例如：「董事一定要香港居民吗？」
6. **live 模式**下，数秒内收到 `【AI 助手】` 开头的回复

### 获取 roomid（群 ID）

- 管理后台客户群详情
- 或 bot 日志 / SQLite：`data/wework_external.db` → `external_groups.roomid`
- 或客户联系回调日志中的 `ChatId`

---

## 五、本地联调（Windows + ngrok）

### 1. Mock 模式（存档未开通）

```powershell
# 终端 1：启动 bot
python main.py wework-external-bot

# 终端 2：模拟建群（触发欢迎语，需企微凭证或 mock 发消息）
python main.py wework-external-mock --roomid wrMOCK001 --create-group

# 终端 2：模拟客户提问
python main.py wework-external-mock --roomid wrMOCK001 --text "香港公司注册地址可以用大陆地址吗？"
```

Mock 模式下发消息会打印到日志（`[Mock 外部群]`），AI 回复同样为 Mock 发送。

### 2. 回调联调（ngrok）

```powershell
# 终端 1
python main.py wework-external-bot

# 终端 2
ngrok http 8081
```

将 ngrok 给出的 HTTPS 地址配置到管理后台：

```
https://xxxx.ngrok-free.app/wework/external/callback
```

保存后日志应出现：`[外部群] URL 验证成功`

然后真实创建客户群，验证欢迎语是否发出。

---

## 六、切换 live 模式（生产）

1. 完成会话存档 RSA + Secret + SDK 部署
2. `.env` 配置：

```env
WEWORK_ARCHIVE_SECRET=xxx
WEWORK_ARCHIVE_PRIVATE_KEY_PATH=D:/keys/archive_private.pem
WEWORK_ARCHIVE_SDK_PATH=D:/projects/finance-ai/vendor/wework-sdk/WeWorkFinanceSdk.dll
WEWORK_EXTERNAL_MODE=live
```

3. Linux 服务器部署 bot，Nginx 反向代理 HTTPS 到 `8081`
4. 重启 bot，日志应出现：`存档 worker 已启动`
5. 在真实客户群发消息，验证 AI 回复

---

## 七、故障排查

| 现象 | 检查项 |
|------|--------|
| 建群无欢迎语 | 回调 URL 是否验证成功；是否勾选客户群变更事件；bot 是否运行 |
| URL 验证失败 | Token/AESKey 与 `.env` 一致；bot 先启动再点保存 |
| 客户说话无回复 | 是否为 live 模式；存档 Secret/私钥/SDK；OpenAI Key |
| Mock 正常 live 不行 | SDK 路径；存档席位；RSA 密钥是否配对 |
| 能收不能发 | `WEWORK_DEFAULT_GROUP_OWNER_USERID`；客户群 API 权限；群主是否在可见范围 |
| 重复回复 | 正常；系统用 msgid 幂等，不应重复（若重复检查存档 seq） |

---

## 八、命令速查

| 命令 | 说明 |
|------|------|
| `python main.py wework-external-bot` | 启动外部群 bot |
| `python main.py wework-external-mock --roomid X --create-group` | 模拟建群 |
| `python main.py wework-external-mock --roomid X --text "问题"` | 模拟客户消息 |

---

## 九、数据与隐私

- 群状态与消息记录：`data/wework_external.db`
- 生产环境请限制数据库与日志访问权限
- 证件类文件收集在 Phase 2 实现

---

## 十、Phase 2/3 功能（已实现）

### 新增命令与入口

| 功能 | 用法 |
|------|------|
| H5 表单 | 欢迎语链接或 `/填表` → `http://<host>:8081/collect/form/<token>` |
| 材料进度 | 群内 `/进度` 或 Mock 注入 |
| 文件上传 | 群内发图片/PDF（live 存档）或 `--file path` |
| 材料确认 | 必填齐全后发摘要，客户回复「确认」 |
| 管理后台 | `http://127.0.0.1:8081/admin/groups` |

### Mock 验证全流程

```powershell
python main.py wework-external-mock --roomid wrTEST001 --create-group
python main.py wework-external-mock --roomid wrTEST001 --text "公司英文名=ABC Limited
注册地址=香港中环
联络邮箱=test@example.com
董事资料=CHAN Tai Man"
python main.py wework-external-mock --roomid wrTEST001 --file D:\path\to\id.jpg
python main.py wework-external-mock --roomid wrTEST001 --text "确认"
```

### 状态流转

`WELCOMED → COLLECTING → REVIEW → CONFIRMED → HANDOFF`

确认后自动：打包材料 → ICRIS 注册（dry_run）→ 通知群主。

### 配置

```env
COLLECT_FORM_BASE_URL=https://your-domain.com
WEWORK_DEFAULT_GROUP_OWNER_USERID=群主userid
```

---

## 附录：原实施计划

开通客户联系、会话存档、API 权限
存档拉取 + 解密 + 文本消息入库
客户群创建事件 → 欢迎语 + 清单
AI 文本问答（复用 LLMClient）
客户群 API 发消息回群
基础状态机：WELCOMED / QA / HUMAN
验收：外部客户在群里提问，AI 30 秒内回群；转人工有效

Phase 2（3 周）— 材料收集闭环
H5 表单 + group_materials
/进度、缺失项追问
LLM 确认摘要 + 客户「确认」
对接 step_package
验收：表单提交后群内进度正确；确认后生成材料包

Phase 3（3–4 周）— 文件与自动化
群图片/PDF 下载 + OSS
文件分类 + checklist 自动勾选
对接 step_icris_register
管理后台（群列表、材料审核、人工接管）
验收：证件上传后可自动归类；全流程可跑通（dry_run）