# ICRIS CDP 排队与会话约定

注册（ICRIS 开户）与 NNC1 填表共用一台真实 Chrome（端口 **9222**、用户目录 `icris-chrome-cdp-profile`）。同一时刻只能有一方占用，否则会互相杀进程、把审核页或填到一半的表冲掉。

激活邮件（IMAP + 点链接）走**独立浏览器**，不占 9222。

## 谁占用 9222

| 阶段 | 浏览器 | CDP 锁 |
|------|--------|--------|
| 注册（s01–s03a） | CDP Chrome（9222） | 持锁 |
| 人工审核 `awaiting_review` | 同一 CDP 会话保持打开 | 继续持锁 |
| 激活（点邮件链接） | 独立 Chrome，不连 9222 | 不持锁 |
| NNC1 填表 | CDP Chrome（9222） | 持锁；有注册排队则让路 |

实现：[`src/browser/cdp_lock.py`](../src/browser/cdp_lock.py)、[`src/browser/cdp_session.py`](../src/browser/cdp_session.py)。

## 心跳锁

锁文件在 `data/`：

- `icris-cdp.lock`：短临界区互斥（只保护 lease 读写）
- `icris-cdp.lease.json`：持有者 `owner` / `pid` / `heartbeat`

心跳约 **10 秒**写一次；心跳超过约 **90 秒**视为过期。

**活锁**必须同时满足：心跳仍新，**且** lease 上的进程还在（Windows：`OpenProcess` + `GetExitCodeProcess == STILL_ACTIVE`；其它：`os.kill(pid, 0)`）。

- pid 已死：立刻清 lease，可杀残留 CDP；Worker 启动时若锁已死，会把卡住的 `awaiting_review` 标失败（`fail_orphan_awaiting_review`）。不必空等 90 秒。
- pid 还在且心跳新：后来者排队等待，**禁止**杀健康的填表或审核页。

`should_skip_kill_cdp_chrome()` 走同一套判定：本线程未持锁、但别人活锁还在时，不杀 9222（也不会误杀激活用的独立 Chrome）。

## 排队规则

- **注册 FIFO**：`claim_next_job` 按 `id ASC`，先登记先跑。失败重试沿用同一 `id`。
- **注册最多 3 次**（`ICRIS_JOB_MAX_ATTEMPTS`，含首次）。NNC1 **不自动重试**。
- **NNC1 让路**：存在 running、`awaiting_review`、或已到期的 pending 注册时，不启动填表。未来的 `available_at`（退避中）**不挡**填表。
- 拿锁后会再查一次队列（避免 TOCTOU）：注册又来了就放锁离开，不把浏览器拉起来。
- 健康进行中的 NNC1：新来的注册任务等待，**不要**中途杀掉 Chrome。

激活成功只把任务标成**待填表**（`form_status=pending`），同一轮循环里不接着填 NNC1。待填表约每 60 秒扫一次（`ICRIS_FORM_POLL_SECONDS`）；激活邮箱约每小时一轮。

## 看门狗

墙钟超时默认 **1500 秒**（`ICRIS_CDP_SESSION_TIMEOUT_SECONDS`，不含人工审核等待）。任务处于 `awaiting_review` 时**暂停计时**。

超时后：

1. 尽量截一张图（限时约 8 秒，失败只打日志）。路径：`data/icris_failures/watchdog_{jobId}_{stamp}.png`（无 jobId 则为 `watchdog_{stamp}.png`）。看门狗在独立线程，用 sync Playwright `connect_over_cdp` 连 9222，截完断开，不关激活用的独立浏览器。
2. 再结束 CDP Chrome（`icris-chrome-cdp-profile` / 9222）并放锁，任务按失败处理。

## 管理后台「重跑填表」

`form_status=failed` 时，**任务列表**和**详情页**都显示「重跑填表」（确认文案一致）。只把状态改回待填表，不立刻开浏览器；若此时有注册排队，Worker 仍会让路。

注册失败请用原来的「重跑」（改注册 `status`），不要和填表重跑混用。

## 相关配置

| 变量 | 默认 | 作用 |
|------|------|------|
| `ICRIS_WORKER_ENABLED` | `true` | 队列 Worker |
| `ICRIS_WORKER_POLL_SECONDS` | `3` | 注册队列轮询 |
| `ICRIS_JOB_MAX_ATTEMPTS` | `3` | 注册总次数（含首次） |
| `ICRIS_CDP_SESSION_TIMEOUT_SECONDS` | `1500` | CDP 会话看门狗墙钟（秒） |
| `ICRIS_FORM_POLL_SECONDS` | `60` | 待填表轮询间隔 |
| `CHROME_CDP_URL` | `http://127.0.0.1:9222` | CDP 地址 |

Worker 应只在 **bot** 进程启用；admin 容器请关 `ICRIS_WORKER_ENABLED`，避免两处抢同一把锁和同一台 Chrome。
