# 外部群全流程验证指令手册

本文档对照外部客户群香港公司注册完整链路，提供 **Mock（本地）** 与 **Live（真实群）** 各环节验证命令。

默认在项目根目录执行：

```powershell
cd D:\projects\finance-ai
$PY = ".\.venv\Scripts\python.exe"   # 或系统 python
$ROOM = "wrTEST001"                  # Mock 测试群；Live 换成真实 roomid
```

---

## 0. 前置检查

```powershell
# 查看所有工作流步骤
& $PY main.py steps

# 检查 .env 关键项（需自行确认已填）
# WEWORK_CORP_ID / SECRET / AGENT_ID
# WEWORK_EXTERNAL_CALLBACK_TOKEN / AES_KEY
# WEWORK_ARCHIVE_*（live 收群消息）
# WEWORK_KF_*（kf 自动回复 + 入站）
# OPENAI_API_KEY（AI 问答）
# WEWORK_WELCOME_ADVISOR_PHONE
# WEWORK_WELCOME_AUTO_CHECKLIST=true

# RAG 知识库入库（首次或更新 注册.md 后）
& $PY main.py rag-ingest --file docs/knowledge/注册.md --verbose
& $PY main.py rag-status

# RAG 检索调试
& $PY main.py rag-query "香港公司注册需要什么材料？"
& $PY main.py rag-query "董事一定要香港居民吗？" --answer
```

**Qdrant 未启动时**：RAG 会降级为纯 LLM，不影响其他环节验证。

---

## 1. 启动外部群 Bot（所有 Live 验证的前提）

**终端 1 — 保持运行：**

```powershell
& $PY main.py wework-external-bot
```

启动后应看到：

- 运行模式：`mock` 或 `live`
- 回调端口：`8081`
- 存档 SDK 状态
- `kf 入站同步: 已启用`（配置了 `WEWORK_KF_*` 时）
- `建群欢迎后自动发清单: 已启用`

**管理后台：** http://127.0.0.1:8081/admin/groups

---

## 2. 环节对照验证表

| # | 环节 | Mock 验证 | Live 验证 | 预期结果 |
|---|------|-----------|-----------|----------|
| 1 | 建群欢迎语 | 见 §3.1 | 企微创建客户群 | 赢态邓老师问候 + 电话 |
| 2 | 自动发资料清单 | 见 §3.1 | 建群后约 30s | 欢迎语后第二条：材料清单 |
| 3 | `/资料` 重发清单 | §3.2 | 群内发 `/资料` | 清单 + `/填表` 引导 |
| 4 | 智能问答 RAG+LLM | §3.3 | 群内提问 | `【AI 助手】` 回复 |
| 5 | AI 回复送达 | 看启动日志 send 模式 | kf 私聊 / mass 待确认 | kf→客服会话；mass→群主确认 |
| 6 | kf 私聊全流程 | §3.4b | 客户在微信客服发消息 | 欢迎/清单/指令/QA 均在私聊 |
| 7 | kf 私聊入站 AI | §3.4 | 客户在微信客服发消息 | 私聊收到 AI 回复 |
| 8 | `/填表` 发模板 | §3.5 / §3.4b | 群内或私聊 `/填表` | 粘贴模板 |
| 8 | 键=值 粘贴入库 | §3.6 | 群内粘贴表单 | `【材料更新】` + 进度 |
| 9 | 文件上传归类 | §3.7 | 群内发图片/PDF | 归类提示 + 进度 |
| 10 | `/进度` | §3.8 | 群内 `/进度` | 必填项完成情况 |
| 11 | 材料确认摘要 | §3.9 | 必填齐后自动 | `【材料确认】` 摘要 |
| 12 | 客户「确认」 | §3.10 | 群内发 `确认` | 打包 + ICRIS dry_run |
| 13 | 转人工 | §3.11 | 群内 `转人工` | AI 停止，通知群主 |
| 14 | CLI 从群 DB 注册 | §3.12 | 确认流程跑过后 | 浏览器填 ICRIS 表单 |
| 15 | 数据归档 | §3.13 | — | DB + 本地文件 |

---

## 3. Mock 全流程（无需存档 SDK，Bot 可不启动）

> Mock 只在本进程处理消息；**发消息到企微**需配置 `WEWORK_*` 凭证。

### 3.1 建群 + 欢迎语 + 自动清单

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --create-group --force
```

**预期：** 日志 `欢迎语已发送`；若 `WEWORK_WELCOME_AUTO_CHECKLIST=true`，还有 `已自动发送注册资料清单`。

### 3.2 手动重发清单

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "/资料"
```

### 3.3 智能问答

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "香港公司注册地址可以用大陆地址吗？"
```

**说明：** debounce 约 5 秒后发回复；kf 模式下回复在客服私聊，不在 Mock 日志的「群消息」里。

查看发送计划（使用 `wm` 开头外部联系人 ID）：

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "测试" --from-id wmMockUser001
```

### 3.4 kf 私聊入站（Live：需 Bot 运行 + `WEWORK_KF_*`）

Bot 终端 1 保持运行，客户在微信客服会话发消息；观察 Bot 日志：

```
kf 收到客户消息 wmXXX: ...
```

### 3.4b kf 私聊 Mock 全流程（无需 Bot）

```powershell
& $PY main.py wework-kf-mock --open-kfid wkTEST --from-id wmTEST001 --first-contact
& $PY main.py wework-kf-mock --open-kfid wkTEST --from-id wmTEST001 --text "/填表"
& $PY main.py wework-kf-mock --open-kfid wkTEST --from-id wmTEST001 --text "香港开户需要多久"
& $PY main.py wework-kf-mock --simulate-callback --open-kfid wkTEST --token mock_token
```

**预期：** Mock 日志显示 kf 出站；首次 `--first-contact` 含欢迎语+清单；问答含 `【AI 助手】`。

### 3.5 获取填写模板

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "/填表"
# 或
& $PY main.py wework-external-mock --roomid $ROOM --text "/模板"
```

### 3.6 粘贴表单（覆盖全部必填文本项）

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text @"
公司英文名=ABC Limited
公司中文名=测试有限公司
注册地址=香港中环皇后大道中1号
联络邮箱=test@example.com
联络电话=85212345678
股东资料=CHAN Tai Man 100%
董事资料=CHAN Tai Man
秘书资料=赢态秘书公司
业务性质=贸易
商业登记证年限=1
申请人姓名=CHAN Tai Man
申请人电邮=test@example.com
申请人电话=85212345678
"@
```

**预期：** `【材料更新】` + 进度（文本项已收，文件项仍缺）。

### 3.7 上传证件（Mock 文件）

```powershell
# 文件名含「身份证」会归类为 id_card_front
& $PY main.py wework-external-mock --roomid $ROOM --file "D:\path\to\身份证_front.jpg"
```

**预期：** 归类提示 + 进度更新；必填齐后触发确认摘要。

### 3.8 查进度

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "/进度"
```

### 3.9 材料确认摘要

必填项齐全后，上一步会自动发 `【材料确认】`；也可再粘贴缺项后观察是否重新触发。

### 3.10 确认 → 打包 → ICRIS

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "确认"
```

**预期：**

- 群消息：`已收到确认，正在打包...`
- 材料包：`data/packages/` 下新生成目录
- ICRIS 浏览器打开填表（`DRY_RUN=true` 不提交）
- 群状态 → `HANDOFF`

### 3.11 转人工

```powershell
& $PY main.py wework-external-mock --roomid $ROOM --text "转人工"
```

**预期：** `已为您转接人工专员`；之后同群 AI 不再回复。

### 3.12 CLI 从群 DB 跑 ICRIS（运维重试）

```powershell
# 需该 roomid 在 wework_external.db 中已有材料
& $PY main.py run --step register --roomid $ROOM

# 仅打包
& $PY main.py run --step package --roomid $ROOM

# 完整流程（用群 DB 材料，跳过 mock）
& $PY main.py run --full --roomid $ROOM
```

### 3.13 检查归档数据

```powershell
# SQLite 查看群状态
sqlite3 data\wework_external.db "SELECT roomid, status, company_name, package_dir FROM external_groups;"

# 材料条数
sqlite3 data\wework_external.db "SELECT roomid, field_key, status FROM group_materials WHERE roomid='$ROOM';"

# 本地证件文件
dir data\materials\$ROOM
```

---

## 4. Live 环境一条龙（真实外部群）

**终端 1：**

```powershell
& $PY main.py wework-external-bot
```

**终端 2 / 企微客户端：**

| 步骤 | 操作 | 验证点 |
|------|------|--------|
| 1 | 企微创建客户群，拉入外部客户 | 欢迎语 + 自动清单 |
| 2 | 客户群内提问 | 存档 worker 日志 `route_archive_text` |
| 3 | 等 5–10 秒 | kf→客服私聊有回复；mass→群主确认后群内有回复 |
| 4 | 发 `/填表` | 收到粘贴模板 |
| 5 | 粘贴键=值表单 | `【材料更新】` |
| 6 | 上传身份证照片 | 文件入库 + 归类 |
| 7 | 发 `/进度` | 进度文本 |
| 8 | 发 `确认` | 打包 + ICRIS dry_run |
| 9 | 打开 http://127.0.0.1:8081/admin/groups | 群列表与状态 |

**真实群 roomid 示例（替换为你的）：**

```powershell
$ROOM = "wrSvUmCQAAaOZaSBI-sv_4HZBvLOa4VQ"
```

---

## 5. 发送模式专项验证

```powershell
# 查看当前发送模式（启动 bot 或 mock 时日志会打印）
# WEWORK_EXTERNAL_SEND_MODE=auto|kf|mass
```

| 模式 | 验证方法 | 成功标志 |
|------|----------|----------|
| `kf` | mock 提问后查客户微信「客服会话」 | 自动收到 `【AI 助手】` |
| `mass` | mock 提问后查群主「服务通知」 | 点确认后群内有消息 |
| kf 入站 | 客户主动给客服发消息 | Bot 日志 + 私聊回复 |

---

## 6. 推荐完整 Mock 脚本（复制执行）

将 `$ROOM` 和证件路径改好后，按顺序执行：

```powershell
cd D:\projects\finance-ai
$PY = ".\.venv\Scripts\python.exe"
$ROOM = "wrTEST001"
$ID = "D:\projects\finance-ai\data\mock\sample_id.jpg"   # 自备测试图

& $PY main.py rag-ingest
& $PY main.py wework-external-mock --roomid $ROOM --create-group --force
& $PY main.py wework-external-mock --roomid $ROOM --text "/资料"
& $PY main.py wework-external-mock --roomid $ROOM --text "香港公司注册需要哪些材料？"
Start-Sleep -Seconds 6
& $PY main.py wework-external-mock --roomid $ROOM --text "/填表"
& $PY main.py wework-external-mock --roomid $ROOM --text "公司英文名=ABC Limited`n注册地址=香港中环`n联络邮箱=test@example.com`n联络电话=85212345678`n股东资料=CHAN Tai Man`n董事资料=CHAN Tai Man`n秘书资料=赢态秘书`n业务性质=贸易`n申请人姓名=CHAN Tai Man`n申请人电邮=test@example.com`n申请人电话=85212345678"
& $PY main.py wework-external-mock --roomid $ROOM --file $ID
& $PY main.py wework-external-mock --roomid $ROOM --text "/进度"
& $PY main.py wework-external-mock --roomid $ROOM --text "确认"
& $PY main.py run --step register --roomid $ROOM
```

---

## 7. 状态流转

```
WELCOMED → QA → COLLECTING → REVIEW → CONFIRMED → HANDOFF
                                              ↘ HUMAN（转人工）
```

---

## 8. 常见问题快速定位

| 现象 | 检查命令/配置 |
|------|----------------|
| 建群无欢迎语 | 回调 URL 是否验证；bot 是否运行；日志有无 `change_external_chat` |
| 收不到客户消息 | `WEWORK_EXTERNAL_MODE=live`；存档 Secret/私钥/SDK |
| AI 不回复 | `OPENAI_API_KEY`；日志有无 `_flush_batch` 异常 |
| 群里有问无答（kf 模式） | 正常：回复在客服私聊；让客户打开微信客服会话 |
| mass 模式 API 成功但群里无消息 | 群主需在「服务通知」点确认 |
| 确认后 ICRIS 未启动 | Playwright/Chrome；`DRY_RUN`；日志 `handoff 失败` |
| `--roomid register` 报错 | 该群 DB 无材料：先跑 mock 收集或 Live 收集 |

---

## 9. 相关文档

- [WEWORK_EXTERNAL_GROUP_SETUP.md](WEWORK_EXTERNAL_GROUP_SETUP.md) — 配置与部署
- [templates/material_checklist.md](../templates/material_checklist.md) — 材料清单模板
- [templates/company_registration_form.md](../templates/company_registration_form.md) — 群内填表模板
