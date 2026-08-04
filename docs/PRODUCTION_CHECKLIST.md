# 生产上线 Checklist（L1 客服机器人）

## 上线边界

- **本期承诺**：微信客服问答 + 材料收集 + 证件识别  
- **不承诺**：ICRIS 无人值守自动提交（需 `DRY_RUN=false` 且 `ICRIS_ALLOW_SUBMIT=true`，默认均关闭）

## 1. 环境与进程

- [ ] 使用入口：`python main.py wework-external-bot`（勿用旧 `wework-bot`）
- [ ] systemd 守护：参考 [`deploy/finance-ai-wework.service`](../deploy/finance-ai-wework.service)
- [ ] Nginx HTTPS 反代：参考 [`deploy/nginx-wework.conf.example`](../deploy/nginx-wework.conf.example)
- [ ] 企微后台回调 URL：`https://你的域名/webhook`
- [ ] 探活：`curl -sS https://你的域名/health` 返回 `"ok": true`

## 2. 密钥与安全

- [ ] `.env` 中配置 `WEWORK_KF_SECRET`、`WEWORK_KF_OPEN_KFID`（或 `WEWORK_KF_ACCOUNTS`）
- [ ] `WEWORK_EXTERNAL_CALLBACK_TOKEN` / `AES_KEY` / `WEWORK_CORP_ID`
- [ ] `ADMIN_PASSWORD` 已设置（否则 `/admin` 不可用）；勿将 admin 暴露公网
- [ ] `MATERIALS_DIR` 指向生产目录并限制文件系统权限
- [ ] `OPENAI_API_KEY`；证件视觉若开启需接受数据外发风险

## 3. RAG / Qdrant

- [ ] Qdrant 已启动且 `QDRANT_URL` 可达
- [ ] 发布/改知识后执行：`python main.py rag-ingest`
- [ ] 冒烟：`python main.py agent-query "香港开户需要多久"`

## 4. 体验相关配置（推荐生产值）

```env
WEWORK_CHANNEL=kf
WEWORK_KF_MODE=both
WEWORK_THINKING_ACK_ENABLED=false
AGENT_SILENT_ON_NO_ANSWER=false
AGENT_ABSTAIN_MESSAGE_TO_CUSTOMER=true
AGENT_SOFT_KNOWLEDGE_MIN_SCORE=0.35
DRY_RUN=true
ICRIS_ALLOW_SUBMIT=false
ICRIS_WORKER_ENABLED=true
ICRIS_JOB_MAX_ATTEMPTS=3
CAPTCHA_SAVE_DEBUG=false
BROWSER_HEADLESS=true
CHROME_USE_EXISTING=false
```

## 5. 验证

- [ ] 发文本业务问题：直接正式答复（无「思考中」提示）  
- [ ] 发材料键值：材料更新，无 RAG  
- [ ] 发无关图片：提示未存档  
- [ ] 发身份证：识别类型/号码（中文提示）  
- [ ] 故意断 OpenAI：客户收到兜底/转人工提示，而非完全无声  
- [ ] `/health` 含 `icris_worker.alive=true`、`pending_count`  

## 6. L2 自动注册（队列）

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
