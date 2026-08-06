# 生产上线 Checklist（L1 企业级垂直客服）

## 上线边界

- **本期承诺**：微信客服问答 + 材料收集 + 证件识别；意图分轨可审计、可熔断、可观测  
- **不承诺**：ICRIS 无人值守自动提交（需 `DRY_RUN=false` 且 `ICRIS_ALLOW_SUBMIT=true`，默认均关闭）；全渠道工单中台

## 1. 环境与进程

- [ ] 使用入口：`python main.py wework-external-bot`（勿用旧 `wework-bot`）
- [ ] systemd 守护：参考 `[deploy/finance-ai-wework.service](../deploy/finance-ai-wework.service)`
- [ ] Nginx HTTPS 反代：参考 `[deploy/nginx-wework.conf.example](../deploy/nginx-wework.conf.example)`
- [ ] 企微后台回调 URL：`https://你的域名/webhook`
- [ ] 探活：`curl -sS https://你的域名/health` 返回 `"ok": true`

### 1.1 管理后台怎么打开（独立进程）

Agent 与管理后台**分进程**：

```bash
# 终端 1：Agent（回调端口默认 8081）
python main.py wework-external-bot

# 终端 2：管理后台（默认 ADMIN_PORT=8082）
cd web/admin && npm install && npm run build   # 首次或前端变更后
cd ../..
python main.py admin
```

| 用途 | URL |
|------|-----|
| 管理后台 SPA | `http://127.0.0.1:8082/admin` |
| Admin 探活 | `http://127.0.0.1:8082/health` |
| Agent 探活 / 指标 | `http://127.0.0.1:8081/health` |

登录：打开 `/admin` 进入登录页，使用 `ADMIN_USERNAME` / `ADMIN_PASSWORD`（Cookie 会话，默认 12h）。

Nginx：`/admin` 反代到 **8082**，`/webhook` 仍反代 **8081**（见 `deploy/nginx-wework.conf.example`）。

## 2. 密钥与安全

- [ ] `.env` 中配置 `WEWORK_KF_SECRET`、`WEWORK_KF_OPEN_KFID`（或 `WEWORK_KF_ACCOUNTS`）
- [ ] `WEWORK_EXTERNAL_CALLBACK_TOKEN` / `AES_KEY` / `WEWORK_CORP_ID`
- [ ] 转人工通知专员：配置自建应用 `WEWORK_CORP_SECRET` + `WEWORK_AGENT_ID`（**不能**用客服 Secret 顶替）；应用有「发送应用消息」权限；`WEWORK_DEFAULT_GROUP_OWNER_USERID` 为企业成员 userid。通知失败时客户侧转接仍应成功（仅打 warning）
- [ ] `ADMIN_PASSWORD` 已设置（否则 `/admin` 不可用）；勿将 admin 暴露公网
- [ ] `MATERIALS_DIR` 指向生产目录并限制文件系统权限
- [ ] `OPENAI_API_KEY`；证件视觉若开启需接受数据外发风险
- [ ] 日志勿泄露 Secret；若已泄露请在企微后台轮换



## 3. RAG / Qdrant

- [ ] Qdrant 已启动且 `QDRANT_URL` 可达
- [ ] 发布/改知识后执行：`python main.py rag-ingest`
- [ ] 冒烟：`python main.py agent-query "香港开户需要多久"`



## 4. 体验与企业级运行配置（推荐生产值）

```env
WEWORK_CHANNEL=kf
WEWORK_KF_MODE=both
WEWORK_THINKING_ACK_ENABLED=false
WEWORK_KF_SEND_QUOTA_48H=5
WEWORK_KF_MERGE_WELCOME_CHECKLIST=true
WEWORK_INBOX_STALE_SECONDS=120
WEWORK_QA_DEBOUNCE_SECONDS=1.0
WEWORK_QA_DEBOUNCE_FAST_SECONDS=0.4
WEWORK_INTENT_LLM_FALLBACK=true
WEWORK_INTENT_MODEL=
WEWORK_INTENT_TIMEOUT_SECONDS=8
WEWORK_INTENT_MIN_CONFIDENCE=0.55
# normal=对客 | shadow=审计不发送 AI | disabled=静态话术熔断
WEWORK_AGENT_MODE=normal
AGENT_SILENT_ON_NO_ANSWER=false
AGENT_ABSTAIN_MESSAGE_TO_CUSTOMER=true
AGENT_CONTEXTUAL_FALLBACK=false
AGENT_SOFT_KNOWLEDGE_MIN_SCORE=0.45
AGENT_FAQ_ENABLED=true
DRY_RUN=true
ICRIS_ALLOW_SUBMIT=false
ICRIS_WORKER_ENABLED=true
ICRIS_JOB_MAX_ATTEMPTS=3
CAPTCHA_SAVE_DEBUG=false
BROWSER_HEADLESS=true
CHROME_USE_EXISTING=false
```



## 5. 发布闸门（企业级必做）



### 5.1 离线回归

- [ ] `PYTHONPATH=. .venv/Scripts/python.exe scripts/smoke_context_answer.py` 全绿  
- [ ] 黄金集 ≥40（`tests/fixtures/intent_routing_cases.json`），含混句/否决/QUEUED+业务  



### 5.2 运行模式放量

1. [ ] `WEWORK_AGENT_MODE=disabled`：验证静态话术；「转人工」仍可用
2. [ ] `WEWORK_AGENT_MODE=shadow`：内测 1–3 天，查 `intent_routes` 分布与 veto_rate，**不对客发 AI**
3. [ ] `WEWORK_AGENT_MODE=normal`：先 KF 小流量，再开群
4. [ ] 事故回滚：分钟级改 `WEWORK_AGENT_MODE=disabled` 并重启/热加载进程



### 5.3 SLO / 观测

- [ ] `/admin` React 后台可打开：概览 KPI、会话材料、注册任务取消/重跑、回答质量与低置信表；无数据时显示空态不报错  
- [ ] `/health` 含：`agent_mode`、`conversation`（`reply_rate` / `silent_rate` / `abstain_rate` / `human_transfer_rate` / `avg_confidence` / `low_confidence_count` / `qa_latency_ms` / `intent_routes`）、`registration`（`success_rate` / `window_counts` / `recent_failures`）  
- [ ] 首周抽检「路径正确率」≥ 90%（进度/清单/业务是否走对轨优先于文案）  
- [ ] 对客静默率 ≈ 0（`AGENT_SILENT_ON_NO_ANSWER=false`）  



## 6. 功能验证

- [ ] 发文本业务问题：正式答复（无「思考中」提示）  
- [ ] 「香港注册需要哪些资料」→ 知识总清单（非「您还缺」）  
- [ ] 「还缺啥」→ 会话待收；「收集到哪些」→ 已收带值（群聊脱敏）  
- [ ] 「还缺啥」+「开户要多久」→ 进度直答 + 单独 QA  
- [ ] QUEUED 问「开户要多久」→ 能答业务；闲聊 → 办理中提示  
- [ ] 发材料键值 /「邮箱改成 xx」：材料更新，无 RAG  
- [ ] 收集中问「董事资料怎么填」：走 QA  
- [ ] 发无关图片 / 非文本：可读提示  
- [ ] 发身份证：识别类型/号码  
- [ ] 故意断 OpenAI：客户收到兜底/转人工，而非完全无声  
- [ ] HUMAN 态：转接提示只发一次；仍可继续业务问答/交材料/查进度；「继续咨询」可恢复助手优先  

- [ ] FAILED 态可「重新办理」/继续问答  



## 7. L2 自动注册（队列）

确认后**入队**（`registration_jobs`），由串行 Worker 执行，不再直起线程抢浏览器。

```env
ICRIS_WORKER_ENABLED=true
ICRIS_WORKER_POLL_SECONDS=3
ICRIS_JOB_MAX_ATTEMPTS=3
# 仅验收通过后开放真实提交：
DRY_RUN=false
ICRIS_ALLOW_SUBMIT=true
BROWSER_HEADLESS=true
CHROME_USE_EXISTING=false
```

- [ ] 两客户先后确认：两条 pending，Worker 串行执行  
- [ ] 同客户重复确认：不建第二活跃任务  
- [ ] `/admin` 可取消 pending / 重跑 failed；失败任务可见截图路径  
- [ ] 成功后再确认需回复「重新办理」；QUEUED 态闲聊会收到「办理中」提示  
- [ ] `needs_review` 材料不可直接确认；超限/非法扩展名上传被拒  
- [ ] **ICRIS 真提交与问答放量解耦**：问答 `normal` 稳定前保持 `DRY_RUN=true`  