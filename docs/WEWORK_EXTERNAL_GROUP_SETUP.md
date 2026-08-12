# 企业微信外部群 AI 客服 — 配置与自动回复指南

本文说明如何在**外部客户群**中启用 Phase 1 能力：建群自动欢迎、材料清单推送、AI 文本问答、转人工。

**全流程验证命令见：** [EXTERNAL_GROUP_VERIFICATION.md](EXTERNAL_GROUP_VERIFICATION.md)

---

## 一、能力说明

| 功能 | 触发方式 |
|------|----------|
| 欢迎语 | 企业成员创建客户群 → 发送赢态财务集团邓老师问候语（含联系电话） |
| 注册资料清单 | 建群欢迎后**自动发送**（`WEWORK_WELCOME_AUTO_CHECKLIST=true`）；也可客户发 `/资料` |
| AI 解答材料问题 | 客户在群内发文字 → **会话内容存档**拉取（live）或 Mock 注入 |
| **微信客服私聊全流程** | 客户在微信客服会话发消息 → **kf/sync_msg** → 欢迎/清单/指令/材料/QA/确认 |
| 客服私聊 AI | 同上（与全流程共用统一状态机） |
| 转人工 | 客户发送 `转人工` |

### 双通道架构（群 + 微信客服）

| 通道 | roomid | 收消息 | 发消息 |
|------|--------|--------|--------|
| 外部客户群 | `wr*` | 会话存档 | kf 私聊客户 或 mass 进群（需确认） |
| 微信客服私聊 | `kf:wm*` | kf/sync_msg | kf/send_msg（全自动） |

`.env` 中 `WEWORK_CHANNEL=both`（默认）同时启用两通道；`group` / `kf` 可单独开关。

```env
WEWORK_CHANNEL=both
WEWORK_EXTERNAL_SEND_MODE=kf
WEWORK_KF_SYNC_ENABLED=true
```

Mock 验证：

```powershell
python main.py wework-external-mock --roomid wrTEST --create-group
python main.py wework-kf-mock --from-id wmTEST001 --first-contact
python main.py wework-kf-mock --from-id wmTEST001 --text "香港开户需要多久"
```

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
   - **URL**：`http://szyingtai.cn/webhook`（须 Nginx 反代到本机 `8081/webhook`；建议后续升 https）  
     兼容路径：`/wework/external/callback`
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
WEWORK_WELCOME_ADVISOR_PHONE=13800138000     # 欢迎语服务老师电话，空则显示【待补充】
WEWORK_WELCOME_AUTO_CHECKLIST=true           # 建群后自动发资料清单

WEWORK_KF_SYNC_ENABLED=true                  # kf 私聊入站智能回复
WEWORK_KF_POLL_INTERVAL=3
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
    → 系统发送赢态财务集团服务老师欢迎语
    → 若 WEWORK_WELCOME_AUTO_CHECKLIST=true，紧接着自动发送注册资料清单
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
4. 约 **30 秒内**，群内应收到 **赢态财务集团邓老师** 欢迎问候（含联系电话），以及注册资料清单（默认自动发送）
5. 若关闭了自动清单，客户可发送 **`/资料`** 获取材料清单；**`/填表`** 获取填写模板
6. 客户在群内输入问题，例如：「董事一定要香港居民吗？」
7. **live 模式**下，数秒内收到 `【AI 助手】` 开头的回复（kf 模式下回复在客服私聊，不在群里）

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
http://szyingtai.cn/webhook
# 或本地穿透: https://xxxx.ngrok-free.app/webhook
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

## 六点五、消息发送模式（重要）

**企微官方限制：外部客户群没有「API 直发进群、无需确认」的接口。**

原先使用的 `add_msg_template`（企业群发）创建任务后，**群主必须在企微里点确认**，消息才会出现在群里。

本项目支持三种发送策略（`.env` 中 `WEWORK_EXTERNAL_SEND_MODE`）：

| 模式 | 行为 | 是否需要群主确认 | 消息出现在 |
|------|------|------------------|------------|
| `kf` | 微信客服 `kf/send_msg` | **否（自动）** | 客户微信「客服会话」（私聊，不在群里） |
| `mass` | 企业群发 `add_msg_template` | **是** | 外部客户群 |
| `webhook` | 群 Webhook | **否** | 仅**内部群**（外部客户群不支持） |
| `auto` | 优先 kf → webhook → mass | 视情况 | — |

### 推荐：微信客服自动回复（无需群主确认）

> **重要**：外部客户群**没有**「消息直接出现在群里且无需确认」的官方 API。  
> `kf` 模式 = 自动私聊该群内的外部客户（消息在**微信客服会话**，不在群聊里）。  
> `mass` 模式 = 消息出现在**群聊**，但需群主点确认。  
> 带 `--roomid` 时，两种模式都**只作用于该群**，不会波及其他群。

1. 管理后台 → **微信客服** → 创建客服账号，记下 `open_kfid`（`wk` 开头）
2. **微信客服 → API** → 将自建应用加入「可调用接口的应用」
3. 获取微信客服专用 Secret（不是应用 Secret）
4. `.env` 配置：

```env
WEWORK_EXTERNAL_SEND_MODE=kf
WEWORK_KF_SECRET=微信客服Secret
WEWORK_KF_OPEN_KFID=wkxxxxxxxx
WEWORK_DEFAULT_GROUP_OWNER_USERID=YingTaiJiTuanDengXianSheng
```

5. 客户需先进入该客服（扫码或从群名片），之后对该群 `--roomid` 的操作会自动私聊该群外部成员
6. **kf 双向**：bot 启动后通过 **回调推送 + sync_msg 拉取**（推荐 `WEWORK_KF_MODE=both`）或轮询接收客户私聊，AI 自动回复

#### 微信客服推送模式（kf_msg_or_event）

官方流程（[接收消息和事件](https://kf.weixin.qq.com/api/doc/path/94745)）：

1. 客户发消息 → 企微向回调 URL 推送 `kf_msg_or_event`（含 `Token` + `OpenKfId`，**不含正文**）
2. 服务立即返回 200 → 后台调用 `kf/sync_msg` 拉取完整消息
3. AI 处理 → `kf/send_msg` 回复（[发送消息](https://kf.weixin.qq.com/api/doc/path/94744)）

**企微后台**：微信客服 → 开发配置 → 回调 URL 填 `http://szyingtai.cn/webhook`（Nginx 反代到 bot 的 `8081/webhook`），Token/AESKey 与 `.env` 中 `WEWORK_EXTERNAL_CALLBACK_*` 一致。本服务同时兼容 `/wework/external/callback`。

```env
WEWORK_KF_MODE=both          # push=仅回调 | poll=仅轮询 | both=推荐
WEWORK_KF_SECRET=...
WEWORK_KF_ACCOUNTS=[{"open_kfid":"wkAAA","name":"香港注册","label":"hk"}]
WEWORK_KF_POLL_INTERVAL=120  # both 模式兜底轮询间隔（秒）
WEWORK_EXTERNAL_CALLBACK_TOKEN=...
WEWORK_EXTERNAL_CALLBACK_AES_KEY=...
```

#### 多客服账号

使用 JSON 配置多个 `open_kfid`，每个账号独立游标；会话 roomid 为 `kf:{open_kfid}:{wm}`：

```env
WEWORK_KF_ACCOUNTS=[
  {"open_kfid":"wkAAA","name":"香港注册","label":"hk"},
  {"open_kfid":"wkBBB","name":"国内咨询","label":"cn"}
]
```

仍支持旧单账号 `WEWORK_KF_OPEN_KFID=wkxxx`（自动转为单元素列表）。

7. Mock 测试：

```powershell
python main.py wework-kf-mock --open-kfid wkAAA --from-id wmTEST --first-contact
python main.py wework-kf-mock --open-kfid wkAAA --from-id wmTEST --text "香港开户需要多久"
python main.py wework-kf-mock --simulate-callback --open-kfid wkAAA --token mock_token
```

群 Mock 测试会自动选用群内外部成员 `wm` ID：

命令会打印 `[Mock] 发送计划: ...` 说明是 kf 自动还是 mass 需确认。

### 若必须在「群里」自动出现消息

官方仅两种方式：

1. **入群欢迎语**（管理后台 → 客户联系 → 入群欢迎语）：群主在手机群设置里开启一次，新人入群自动发（不支持 AI 自由回复）
2. **群自动回复小助理**：群主开启后，客户 `@小助理 + 关键词` 触发（需管理员预配规则）

---

## 七、故障排查

| 现象 | 检查项 |
|------|--------|
| 建群无欢迎语 | 回调 URL 是否验证成功；是否勾选客户群变更事件；bot 是否运行 |
| URL 验证失败 | Token/AESKey 与 `.env` 一致；bot 先启动再点保存 |
| 客户说话无回复 | 是否为 live 模式；存档 Secret/私钥/SDK；OpenAI Key |
| Mock 正常 live 不行 | SDK 路径；存档席位；RSA 密钥是否配对 |
| 能收不能发 | `WEWORK_DEFAULT_GROUP_OWNER_USERID`；客户群 API 权限；群主是否在可见范围 |
| API 成功但群里无消息 | 当前为 `mass` 模式，需群主在【服务通知】确认；或改 `WEWORK_EXTERNAL_SEND_MODE=kf` |
| 消息发到群主所有群 | 已修复：群发必须带 `chat_id_list`；sender 自动取当前群群主 |
| kf 收不到消息 | `WEWORK_KF_MODE` 与回调 URL 是否配置；push 模式需公网回调；both 模式有轮询兜底 |
| kf 模式发送失败 | 客户是否已联系过该客服；`WEWORK_KF_SECRET/OPEN_KFID` 是否正确；多账号时检查 roomid 对应 open_kfid |
| 重复回复 | 正常；系统用 msgid 幂等，不应重复（若重复检查存档 seq） |
| 群里没看到 AI 回复 | 若 `WEWORK_EXTERNAL_SEND_MODE=kf`，回复在客户的**微信客服会话**而非群聊界面；要让消息出现在群里需改 `mass`（需群主每次点确认） |
| 客服会话也没收到 | 检查 `WEWORK_KF_SECRET` 和 `WEWORK_KF_OPEN_KFID`；客户是否已进入过该客服（首次需扫码或点群名片）；48h 内是否超 5 条配额（`WEWORK_KF_SEND_QUOTA_48H`） |
| 报错「未配置 WEWORK_DEFAULT_GROUP_OWNER_USERID」 | 群详情 API 拿不到 owner；在 `.env` 里手动填群主 userid（企微后台 → 客户联系 → 群主详情） |
| mock 模式发消息打印但无真实发送 | 正常；`WEWORK_EXTERNAL_MODE=mock` 时只打印日志不真实调企微 API，需切 `live` |

### 管理后台手动发消息（测试用）

启动 admin：

```powershell
python main.py admin
```

打开 `http://127.0.0.1:8082/admin` → 左侧导航「**外部群发消息**」：
- 输入外部群 `chat_id`（`wr*` 前缀）和消息内容 → 点「发送到外部群」
- 右侧实时显示当前 `WEWORK_EXTERNAL_SEND_MODE`、企微/客服配置状态
- 发送后展示**发送计划**（将走哪条通道）和**企微返回**（errcode/errmsg）

---

## 八、命令速查

| 命令 | 说明 |
|------|------|
| `python main.py wework-external-bot` | 启动外部群 bot |
| `python main.py wework-external-mock --roomid X --create-group` | 模拟建群 |
| `python main.py wework-external-mock --roomid X --create-group --force` | 强制重发欢迎语 |
| `python main.py wework-kf-mock --open-kfid X --text "问题"` | Mock 注入 kf 私聊 |
| `python main.py wework-kf-mock --simulate-callback --open-kfid X` | Mock kf 回调触发 sync |
| `python main.py run --step register --roomid wrXXX` | 从群 DB 加载材料跑 ICRIS 注册 |

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
| 群内填表 | `/填表` 或 `/模板` → 发送粘贴模板，客户填好后粘贴到本群 |
| 材料进度 | 群内 `/进度` 或 Mock 注入 |
| 文件上传 | 群内发图片/PDF（live 存档）或 `--file path` |
| 材料确认 | 必填齐全后发摘要，客户回复「确认」 |
| 管理后台 | `http://127.0.0.1:8081/admin/groups` |
| H5 表单（可选） | 设置 `COLLECT_FORM_ENABLED=true` 后，`/填表` 改为发在线链接 |

### Mock 验证全流程

完整分环节命令见 [EXTERNAL_GROUP_VERIFICATION.md](EXTERNAL_GROUP_VERIFICATION.md)。快速脚本：

```powershell
python main.py wework-external-mock --roomid wrTEST001 --create-group --force
python main.py wework-external-mock --roomid wrTEST001 --text "/填表"
python main.py wework-external-mock --roomid wrTEST001 --text "公司英文名=ABC Limited
注册地址=香港中环
联络邮箱=test@example.com
董事资料=CHAN Tai Man"
python main.py wework-external-mock --roomid wrTEST001 --file D:\path\to\id.jpg
python main.py wework-external-mock --roomid wrTEST001 --text "/进度"
python main.py wework-external-mock --roomid wrTEST001 --text "确认"
```

### 状态流转

`WELCOMED → COLLECTING → REVIEW → CONFIRMED → HANDOFF`

确认后自动：打包材料 → ICRIS 注册（dry_run）→ 通知群主。

### 配置

```env
# 默认关闭 H5，使用群内粘贴收集
COLLECT_FORM_ENABLED=false

# 若需启用 H5 在线表单：
# COLLECT_FORM_ENABLED=true
# COLLECT_FORM_BASE_URL=https://your-domain.com

WEWORK_DEFAULT_GROUP_OWNER_USERID=群主userid
```

---

## 附录：原实施计划

开通客户联系、会话存档、API 权限
存档拉取 + 解密 + 文本消息入库
客户群创建事件 → 赢态欢迎语（/资料、/填表 按需获取清单与模板）
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