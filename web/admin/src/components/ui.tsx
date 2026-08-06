import type { ReactNode } from "react";

export function pct(rate: unknown): string {
  const n = Number(rate);
  if (!Number.isFinite(n)) return "0.0%";
  return `${(n * 100).toFixed(1)}%`;
}

export function statusBadge(status: string | undefined): string {
  const s = (status || "").toLowerCase();
  if (["succeeded", "done", "completed", "reply"].includes(s)) return "badge ok";
  if (["failed", "cancelled", "silent", "abstain"].includes(s)) return "badge danger";
  if (["running", "pending", "queued", "human"].includes(s)) return "badge warn";
  return "badge info";
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
