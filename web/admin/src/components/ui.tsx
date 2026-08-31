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
