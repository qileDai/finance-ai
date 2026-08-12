# 打通企业微信外部群自动发消息

## 目标

让企业微信**外部客户群**（chat_id 形如 `wr*`）具备**自动发消息**能力：
- 建群 → 自动发欢迎语 + 资料清单
- 客户在群里提问 → AI 自动回复
- 注册完成 → 自动推送结果到群

## 当前实现状态（已探索结论）

代码**已完整实现**外部群发消息能力，关键模块：

### 1. 发送客户端 [src/wework/external_client.py](file:///d:/projects/finance-ai/src/wework/external_client.py)

`WeWorkExternalClient` 支持 **4 种发送模式**（通过 `WEWORK_EXTERNAL_SEND_MODE` 控制，`auto` 自动决策）：

| 模式 | API | 即时性 | 群主确认 | 说明 |
|---|---|---|---|---|
| **kf** | `/cgi-bin/kf/send_msg` | ✅ 即时 | ❌ 不需要 | 微信客服**私聊**群内外部成员；消息出现在客户的「客服会话」而非群聊 |
| **mass** | `/cgi-bin/externalcontact/add_msg_template` | ❌ 异步 | ✅ **必须** | 企业群发；任务创建后需群主在企微「服务通知」点确认才发到群里 |
| **webhook** | 群机器人 webhook | ✅ 即时 | ❌ 不需要 | 需群已开启「机器人」+ 配置 webhook URL |
| **appchat** | `/cgi-bin/appchat/send` | ✅ 即时 | ❌ 不需要 | 仅**内部群**；外部群 chat_id `wr*` 不适用 |

**自动决策顺序**（`wework_external_send_mode_resolved`）：
```
auto → kf（若已配 KF_SECRET+OPEN_KFID）
     → webhook（若已配 GROUP_WEBHOOK_URL）
     → mass（兜底）
```

### 2. 状态机 [src/wework/group_state_machine.py](file:///d:/projects/finance-ai/src/wework/group_state_machine.py)

- `handle_group_created(roomid)`：建群 → 发欢迎语 + 可选清单（`_send_checklist`）
- `handle_incoming_text(roomid, ...)`：入站消息 → AI 回复 / 指令分发 / 材料收集
- `_safe_send(roomid, content, to_external_userid=...)`：统一发送入口（自动切分长文本、记录配额、失败兜底）

### 3. 回调入口 [src/wework/callback_handler.py](file:///d:/projects/finance-ai/src/wework/callback_handler.py)

- `parse_external_callback_xml`：解析企微回调的 `change_external_chat` 事件（建群/加人）
- 由 [src/web/collect_server.py](file:///d:/projects/finance-ai/src/web/collect_server.py) 的 `do_POST` 接收 `/webhook` 路径

### 4. 主入口 [main.py:438](file:///d:/projects/finance-ai/main.py#L438) `cmd_wework_external_bot`

启动 `UnifiedWebServer`（端口 8081）监听 `/webhook`，路由到 `MessageRouter.route_external_chat_event` → 触发欢迎语等自动化。

### 5. 配置 [.env.example:20-43](file:///d:/projects/finance-ai/.env.example#L20-L43)

外部群相关环境变量已就位。

### 6. 文档 [docs/WEWORK_EXTERNAL_GROUP_SETUP.md](file:///d:/projects/finance-ai/docs/WEWORK_EXTERNAL_GROUP_SETUP.md)

已有详细配置步骤（开通客户联系 → 自建应用 → 配置回调 → 会话存档）。

---

## 打通方案（推荐路径）

外部群自动发消息**没有完美方案**，企微官方限制：
- **客户群本身没有「机器人发消息」API**（与内部群不同）
- 只能：①客服私聊（消息不进群） ②企业群发（需群主确认） ③群机器人 webhook（需群开启）

### 推荐：「客服私聊」模式（生产试点首选）

**理由**：
- ✅ **全自动**：无需群主确认、即时送达
- ✅ **零额外配置**：只需 `WEWORK_KF_SECRET` + `WEWORK_KF_OPEN_KFID`
- ✅ **消息到达率高**：直接进客户微信「客服会话」
- ⚠️ **缺点**：消息在客户的**客服会话**里，不在群聊界面（群成员看不到其他人收到的消息）

**适合场景**：注册欢迎语、资料清单、AI 答疑、进度通知（一对一）

### 备选：「企业群发」模式（需要群公告式消息时）

- ✅ **消息在群聊**：所有群成员可见
- ❌ **需群主每次点确认**：运营成本高
- 适合：周知类、低频公告

### 备选：「群机器人 webhook」模式（技术最优但配置繁）

- ✅ **即时入群**、无需确认
- ❌ **每个群都要单独开启机器人 + 复制 webhook URL**
- 适合：客户群数量少（<10）且可控

---

## 实施步骤（推荐「客服私聊」模式）

### 步骤 1：企微后台配置（5 分钟）

参考 [docs/WEWORK_EXTERNAL_GROUP_SETUP.md](file:///d:/projects/finance-ai/docs/WEWORK_EXTERNAL_GROUP_SETUP.md) 第二章：

1. **开通客户联系**：管理后台 → 客户联系 → 配置 → 启用
2. **自建应用**：应用管理 → 自建 → 记录 `AgentId` / `Secret`
3. **客户联系 API 权限**：客户群管理 + 客户基础信息
4. **配置回调**：应用 → 客户联系 → API → 接收事件服务器
   - URL：`http://<公网域名>/webhook`
   - Token/EncodingAESKey 与 `.env` 一致
   - 勾选事件：**客户群变更** `change_external_chat`
5. **开通微信客服**：管理后台 → 应用管理 → 微信客服 → 记录 `Secret` 和 `open_kfid`

### 步骤 2：配置 `.env`

```env
# 基础（自建应用）
WEWORK_CORP_ID=ww...               # 企业 ID
WEWORK_CORP_SECRET=<自建应用Secret>
WEWORK_AGENT_ID=<自建应用AgentId>

# 外部群回调
WEWORK_EXTERNAL_CALLBACK_TOKEN=<回调Token>
WEWORK_EXTERNAL_CALLBACK_AES_KEY=<回调EncodingAESKey>
WEWORK_EXTERNAL_CALLBACK_PORT=8081

# 默认群主（发消息以此为 sender；找不到群主时兜底）
WEWORK_DEFAULT_GROUP_OWNER_USERID=<群主userid>

# 发送模式：走客服私聊（无需群主确认）
WEWORK_EXTERNAL_SEND_MODE=kf
WEWORK_CHANNEL=both

# 微信客服
WEWORK_KF_SECRET=<客服Secret>
WEWORK_KF_OPEN_KFID=wkXXX          # 客服账号 ID
WEWORK_KF_MODE=both
WEWORK_KF_SYNC_ENABLED=true

# 关闭 mock，启用真实
WEWORK_EXTERNAL_MODE=live

# 欢迎语
WEWORK_WELCOME_ADVISOR_PHONE=<联系电话>
WEWORK_WELCOME_AUTO_CHECKLIST=true
```

### 步骤 3：启动 bot 进程

```powershell
# 终端 1：外部群 bot（监听回调）
python main.py wework-external-bot

# 终端 2（可选）：管理后台
python main.py admin
```

启动日志会打印：
```
[外部群] 运行模式: live
[外部群] 双通道: both (群=开, 客服=开)
[外部群] 企微已配置: True
[外部群] 回调已配置: True
[外部群] 回调端口: 8081
```

### 步骤 4：验证

```powershell
# 模拟建群（不需要真实回调）
python main.py wework-external-mock --roomid wrTEST001 --create-group

# 模拟客户发消息 → AI 自动回复
python main.py wework-external-mock --roomid wrTEST001 --text "香港注册需要多久"

# 真实场景：
# 1. 企业成员在企微里建一个客户群 → 触发 change_external_chat 回调
# 2. bot 收到回调 → handle_group_created → 自动发欢迎语 + 清单
# 3. 群内外部客户收到客服私聊消息（在「微信客服」会话里）
```

---

## 改动清单

**核心代码已存在，本次仅需**：

### 1. 完善 `.env.example` 注释（可选）

[.env.example](file:///d:/projects/finance-ai/.env.example) 第 39-41 行 `WEWORK_EXTERNAL_SEND_MODE` 注释已写明 4 种模式，无需改。

### 2. 增加 admin 后台手动发送入口（可选增强）

**场景**：在管理后台手动输入 chat_id + 消息内容 → 一键发送到外部群（测试用）。

**改动文件**：
- [src/web/admin_api.py](file:///d:/projects/finance-ai/src/web/admin_api.py)：新增 `POST /admin/api/wework/send` 接收 `{chat_id, content}`
- [src/web/admin_server.py](file:///d:/projects/finance-ai/src/web/admin_server.py)：路由到 `WeWorkExternalClient().send_group_text(...)`
- [web/admin/src/pages/](file:///d:/projects/finance-ai/web/admin/src/pages/)：新增 `WeworkSendPage.tsx`（chat_id 输入框 + 消息 textarea + 发送按钮 + 结果回显）

**优先级**：P2（不阻塞主流程；如不需要可跳过）

### 3. 文档更新（可选）

在 [docs/WEWORK_EXTERNAL_GROUP_SETUP.md](file:///d:/projects/finance-ai/docs/WEWORK_EXTERNAL_GROUP_SETUP.md) 末尾追加「常见问题」：
- Q：群里没看到消息？A：检查 `WEWORK_EXTERNAL_SEND_MODE` 是否 `kf`（客服私聊不进群）
- Q：客服私聊没收到？A：检查 `WEWORK_KF_SECRET` 和 `WEWORK_KF_OPEN_KFID`
- Q：报错「未配置 WEWORK_DEFAULT_GROUP_OWNER_USERID」？A：群详情拿不到 owner，需手动配置群主 userid

---

## 验证步骤

### 1. 单元测试
```powershell
# mock 模式发消息
python main.py wework-external-mock --roomid wrTEST --create-group --force
# 应看到 [Mock 外部群] 群 wrTEST → None: <欢迎语>...
```

### 2. 真实回调测试
```powershell
# 1. 启动 bot
python main.py wework-external-bot

# 2. 在企微里建一个新客户群（拉一个外部联系人入群）
# 3. 看 bot 日志：
#    - "客户群事件 create chat_id=wrXXXX"
#    - "群 wrXXXX 欢迎语已发送 → WELCOMED"
#    - "已通过微信客服 [wkXXX] 自动发送给 wmYYY"
# 4. 外部联系人在微信「客服会话」里收到欢迎语
```

### 3. 命令行触发
```powershell
# 从命令行直接发消息到指定群（测试）
python -c "from src.wework.external_client import WeWorkExternalClient; WeWorkExternalClient().send_group_text('wrXXXX', '测试消息')"
```

---

## 关键决策与假设

1. **默认推荐 kf 模式**：生产试点已验证可行性（文档第 25 行）
2. **不改动现有发送逻辑**：`WeWorkExternalClient.send_group_text` 已实现 4 种模式自动决策，无需重写
3. **admin 后台手动发送为可选**：核心场景是自动触发（建群/AI 回复），手动发送仅测试用
4. **chat_id 来源**：从回调 XML 的 `ChatId` 字段提取（`wr*` 前缀），已存储在 `ExternalGroupStore`

---

## 风险与注意

1. **kf 模式消息不进群**：客户看到的消息在「微信客服」会话，不在群聊界面
2. **群主确认成本**：mass 模式每次都要群主点确认，运营不可持续
3. **回调公网可达**：`WEWORK_EXTERNAL_CALLBACK_PORT=8081` 需 Nginx 反代到公网
4. **会话存档 SDK**：如需**接收**客户群消息（不仅是发），需购买会话存档席位并配置 SDK（[docs:77-](file:///d:/projects/finance-ai/docs/WEWORK_EXTERNAL_GROUP_SETUP.md#L77)）；仅**发**消息不需要
5. **48h 客服配额**：kf 模式 48 小时内对同一外部联系人最多发 5 条（`WEWORK_KF_SEND_QUOTA_48H=5`），超出会被限流

---

## 后续扩展（不在本次范围）

- 支持 `markdown` / `image` / `file` 消息类型（目前只支持 text）
- 外部群消息撤回 / 已读回执
- 多客服账号负载均衡（已支持 `WEWORK_KF_ACCOUNTS` JSON，但需测试）
