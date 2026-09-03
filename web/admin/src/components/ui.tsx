import type { ReactNode } from "react";

export function pct(rate: unknown): string {
  const n = Number(rate);
  if (!Number.isFinite(n)) return "0.0%";
  return `${(n * 100).toFixed(1)}%`;
}

export function statusBadge(status: string | undefined): string {
  const s = (status || "").toLowerCase();
  if (["succeeded", "done", "completed", "reply"].includes(s)) return "badge ok";
  if (["rejected"].includes(s)) return "badge warn";
  if (["failed", "cancelled", "silent", "abstain"].includes(s)) return "badge danger";
  if (["running", "pending", "queued", "human", "awaiting_review"].includes(s))
    return "badge warn";
  return "badge info";
}

export const JOB_SHOT_PREVIEW = {
  rootClassName: "job-shot-preview",
  maskClosable: true,
  closable: true,
} as const;

export function isJobReviewRejected(
  status: string | undefined,
  reviewStatus?: string | undefined,
): boolean {
  const s = (status || "").toLowerCase();
  return (
    (reviewStatus || "").toLowerCase() === "rejected" &&
    (s === "failed" || s === "cancelled")
  );
}

export function jobCanCancel(status: string | undefined): boolean {
  const s = (status || "").toLowerCase();
  return s === "pending" || s === "running";
}

export function jobCanRequeue(status: string | undefined): boolean {
  const s = (status || "").toLowerCase();
  return s === "failed" || s === "cancelled";
}

export function jobStatusLabel(
  status: string | undefined,
  reviewStatus?: string | undefined,
): string {
  if (isJobReviewRejected(status, reviewStatus)) return "已拒绝";
  const map: Record<string, string> = {
    pending: "待处理",
    running: "进行中",
    awaiting_review: "待审核",
    succeeded: "已成功",
    failed: "已失败",
    cancelled: "已取消",
  };
  const s = (status || "").toLowerCase();
  return map[s] || status || "-";
}

export function jobStatusTagColor(
  status: string | undefined,
  reviewStatus?: string | undefined,
): string {
  if (isJobReviewRejected(status, reviewStatus)) return "default";
  const map: Record<string, string> = {
    pending: "default",
    running: "processing",
    awaiting_review: "warning",
    succeeded: "success",
    failed: "error",
    cancelled: "warning",
  };
  return map[(status || "").toLowerCase()] || "default";
}

export const JOB_PIPELINE_STEPS: { step: string; label: string }[] = [
  { step: "queued", label: "排队" },
  { step: "registering", label: "注册中" },
  { step: "review", label: "待审核" },
  { step: "registered", label: "注册完成" },
  { step: "activating", label: "待激活" },
  { step: "activated", label: "已激活" },
  { step: "form_pending", label: "待填表" },
  { step: "form_filled", label: "已填表" },
];

export const JOB_FIELD_GROUP_LABELS: Record<string, string> = {
  company: "公司",
  office: "注册办事处",
  contact: "联络",
  director: "董事兼股东",
  applicant: "申请人",
  secretary: "公司秘书",
  icris: "ICRIS 账号",
  identity: "身份证明",
};

export type JobProgressView = {
  step?: string;
  label?: string;
  failed?: boolean;
  detail?: string;
};

export function jobProgressTagColor(p?: JobProgressView | null): string {
  if (!p?.label) return "default";
  if (p.failed) return "error";
  if (p.step === "form_filled" || p.step === "activated" || p.step === "registered") {
    return "success";
  }
  if (
    p.step === "queued" ||
    p.step === "registering" ||
    p.step === "review" ||
    p.step === "activating" ||
    p.step === "form_pending"
  ) {
    return "processing";
  }
  return "default";
}

export function jobProgressTooltip(p?: JobProgressView | null): string {
  const chain = JOB_PIPELINE_STEPS.map((s) => s.label).join(" → ");
  if (!p?.label) return chain;
  const lines = [`当前：${p.label}`, chain];
  if (p.detail) lines.push(p.detail);
  return lines.join("\n");
}

export function StateBox({
  loading,
  error,
  empty,
  children,
}: {
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  children: ReactNode;
}) {
  if (loading) return <div className="state-box">加载中…</div>;
  if (error) return <div className="error-box">{error}</div>;
  if (empty) return <div className="empty-box">暂无数据</div>;
  return <>{children}</>;
}
