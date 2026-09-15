import type { JobRow } from "./api";
import { formatDateTime } from "./format";

export type JobTimelineItem = {
  time: string;
  text: string;
  tone?: "ok" | "warn" | "fail" | "muted";
};

const MAIL_STUCK_S = 2 * 3600;

export function parseJobTimeMs(raw?: string | null): number | null {
  const s = String(raw || "").trim();
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

export function formatDurationWait(seconds: number): string {
  const sec = Math.max(0, Math.floor(seconds));
  if (sec < 60) return `已等 ${sec}s`;
  if (sec < 3600) return `已等 ${Math.floor(sec / 60)} 分钟`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return m ? `已等 ${h}h${m}m` : `已等 ${h}h`;
}

export function formatAgeAgo(seconds: number): string {
  const sec = Math.max(0, Math.floor(seconds));
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return m ? `${h}h${m}m 前` : `${h}h 前`;
}

export function isActivationWaiting(
  job: Pick<JobRow, "activation_status">,
): boolean {
  const act = String(job.activation_status || "").toLowerCase();
  return act === "pending" || act === "activating";
}

export function activationWaitSeconds(
  job: JobRow,
  now = Date.now(),
): number | null {
  if (!isActivationWaiting(job)) return null;
  const start =
    parseJobTimeMs(job.activation_pending_at) ?? parseJobTimeMs(job.finished_at);
  if (start == null) return null;
  return Math.max(0, (now - start) / 1000);
}

export function isActivationMailStuck(job: JobRow, now = Date.now()): boolean {
  if (!isActivationWaiting(job)) return false;
  if (String(job.activation_url || "").trim()) return false;
  const sec = activationWaitSeconds(job, now);
  return sec != null && sec > MAIL_STUCK_S;
}

export function activationWaitHint(job: JobRow, now = Date.now()): string {
  const sec = activationWaitSeconds(job, now);
  if (sec == null) return "";
  return formatDurationWait(sec);
}

export function buildJobTimeline(job: JobRow): JobTimelineItem[] {
  const items: JobTimelineItem[] = [];
  const push = (
    raw: string | undefined,
    text: string,
    tone?: JobTimelineItem["tone"],
  ) => {
    if (!text) return;
    items.push({ time: formatDateTime(raw), text, tone });
  };

  if (job.created_at) push(job.created_at, "入队");
  if (job.started_at) push(job.started_at, "注册开始");

  const status = String(job.status || "").toLowerCase();
  const review = String(job.review_status || "").toLowerCase();
  if (status === "awaiting_review") {
    push(job.updated_at, "待审核", "warn");
  } else if (review === "rejected") {
    push(job.finished_at || job.updated_at, "审核拒绝", "fail");
  } else if (status === "succeeded") {
    const s03a = String(job.s03a_duration || "").trim();
    push(
      job.finished_at || job.updated_at,
      s03a ? `注册完成（到 s03a ${s03a}）` : "注册完成",
      "ok",
    );
  } else if (status === "failed") {
    push(job.finished_at || job.updated_at, "注册失败", "fail");
  } else if (status === "cancelled") {
    push(job.finished_at || job.updated_at, "已取消", "muted");
  }

  const act = String(job.activation_status || "").toLowerCase();
  if (
    job.activation_pending_at ||
    ["pending", "activating", "activated", "failed"].includes(act)
  ) {
    push(job.activation_pending_at || job.finished_at, "起等激活邮件");
  }
  if (job.activation_checked_at) {
    push(job.activation_checked_at, "上次扫信");
  }
  if (job.activation_url_saved_at) {
    push(job.activation_url_saved_at, "链接已入库", "ok");
  }

  if (isActivationWaiting(job) && !String(job.activation_url || "").trim()) {
    const wait = activationWaitHint(job);
    if (wait) {
      items.push({
        time: formatDateTime(
          job.activation_checked_at || job.activation_pending_at,
        ),
        text: `${wait}、尚无链接`,
        tone: isActivationMailStuck(job) ? "warn" : "muted",
      });
    }
  }

  const attempts = Number(job.activation_attempts || 0);
  const attemptText = attempts > 0 ? `第 ${attempts} 次尝试` : "尚未尝试";
  if (act === "activating") {
    push(job.updated_at, `激活中（${attemptText}）`);
  }
  if (act === "activated") {
    push(
      job.activation_activated_at || job.updated_at,
      attempts > 0 ? `激活成功（第 ${attempts} 次尝试）` : "激活成功",
      "ok",
    );
  }
  if (act === "failed") {
    push(
      job.updated_at,
      attempts > 0 ? `激活失败（第 ${attempts} 次尝试）` : "激活失败",
      "fail",
    );
  }

  const form = String(job.form_status || "").toLowerCase();
  const nnc1 = String(job.nnc1_duration || "").trim();
  if (form === "pending") {
    push(job.activation_activated_at || job.updated_at, "待填表", "muted");
  }
  if (form === "filled") {
    push(
      job.form_filled_at || job.updated_at,
      nnc1 ? `填表完成（nnc1 ${nnc1}）` : "填表完成",
      "ok",
    );
  }
  if (form === "failed") {
    push(
      job.updated_at,
      nnc1 ? `填表失败（nnc1 ${nnc1}）` : "填表失败",
      "fail",
    );
  }
  return items;
}
