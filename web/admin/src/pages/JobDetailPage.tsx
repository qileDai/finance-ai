import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type JobDetailResponse, type JobField } from "../api";
import { formatDateTime } from "../format";
import { asLogText, logLineClass, normalizeLogLines } from "../jobLog";
import { StateBox, statusBadge } from "../components/ui";

type Props = {
  refreshKey: number;
  onToast: (msg: string) => void;
  onRefresh: () => void;
};

export function JobDetailPage({ refreshKey, onToast, onRefresh }: Props) {
  const { id } = useParams();
  const jobId = Number(id);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetailResponse | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!Number.isFinite(jobId) || jobId <= 0) {
      setError("invalid job id");
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .job(jobId)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e: Error) => {
        if (alive) setError(e.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [jobId, refreshKey]);

  const jobStatus = detail?.job?.status;
  useEffect(() => {
    if (!Number.isFinite(jobId) || jobId <= 0) return;
    if (jobStatus !== "pending" && jobStatus !== "running") return;
    let alive = true;
    const tick = () => {
      api
        .job(jobId)
        .then((d) => {
          if (alive) setDetail(d);
        })
        .catch(() => {
          /* keep last detail */
        });
    };
    const id = window.setInterval(tick, 3000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [jobId, jobStatus]);

  async function act(kind: "cancel" | "requeue") {
    const label = kind === "cancel" ? "取消" : "重跑";
    if (!window.confirm(`确认${label}任务 #${jobId}？`)) return;
    setBusy(true);
    try {
      const res =
        kind === "cancel" ? await api.cancelJob(jobId) : await api.requeueJob(jobId);
      onToast(res.message || `${label}成功`);
      onRefresh();
    } catch (e) {
      onToast((e as Error).message || `${label}失败`);
    } finally {
      setBusy(false);
    }
  }

  const job = detail?.job;
  const fields: JobField[] = detail?.fields || [];
  const messages = normalizeLogLines(detail?.messages);

  return (
    <>
      <div className="toolbar" style={{ gap: 12 }}>
        <Link to="/jobs" className="btn btn-sm btn-ghost">
          ← 返回列表
        </Link>
        {job?.status === "pending" ? (
          <button
            type="button"
            className="btn btn-sm btn-danger"
            disabled={busy}
            onClick={() => act("cancel")}
          >
            取消
          </button>
        ) : null}
        {job?.status === "failed" || job?.status === "cancelled" ? (
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={busy}
            onClick={() => act("requeue")}
          >
            重跑
          </button>
        ) : null}
      </div>

      <StateBox loading={loading} error={error} empty={!job}>
        {job ? (
          <div className="job-detail">
            <section className="reg-card">
              <h2>
                任务 #{job.id}{" "}
                <span className={statusBadge(job.status)}>{job.status}</span>
              </h2>
              <dl className="job-meta">
                <div>
                  <dt>来源</dt>
                  <dd className="mono">{job.source || "-"}</dd>
                </div>
                <div>
                  <dt>公司</dt>
                  <dd>{job.company_name || "-"}</dd>
                </div>
                <div>
                  <dt>roomid</dt>
                  <dd className="mono">{job.roomid}</dd>
                </div>
                <div>
                  <dt>尝试</dt>
                  <dd className="mono">
                    {job.attempts ?? 0}/{job.max_attempts ?? 0}
                  </dd>
                </div>
                <div>
                  <dt>dry / submit</dt>
                  <dd className="mono">
                    {job.dry_run ? "Y" : "N"} / {job.allow_submit ? "Y" : "N"}
                  </dd>
                </div>
                <div>
                  <dt>创建</dt>
                  <dd className="mono muted">{formatDateTime(job.created_at)}</dd>
                </div>
                <div>
                  <dt>开始</dt>
                  <dd className="mono muted">{formatDateTime(job.started_at)}</dd>
                </div>
                <div>
                  <dt>结束</dt>
                  <dd className="mono muted">{formatDateTime(job.finished_at)}</dd>
                </div>
                <div>
                  <dt>更新</dt>
                  <dd className="mono muted">{formatDateTime(job.updated_at)}</dd>
                </div>
                <div>
                  <dt>材料包</dt>
                  <dd className="mono muted">{job.package_dir || "-"}</dd>
                </div>
                <div>
                  <dt>截图</dt>
                  <dd className="mono muted">{job.screenshot_path || "-"}</dd>
                </div>
              </dl>
              {job.last_error ? (
                <div className="job-error">
                  <strong>失败原因</strong>
                  <pre>{job.last_error}</pre>
                </div>
              ) : null}
            </section>

            <section className="reg-card">
              <h2>填写字段</h2>
              {fields.length ? (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>字段</th>
                        <th>值</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fields.map((f) => (
                        <tr key={f.key}>
                          <td>{f.label || f.key}</td>
                          <td style={{ wordBreak: "break-all" }}>{f.value}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted">无字段快照（旧任务可查看会话材料）</p>
              )}
            </section>

            <section className="reg-card">
              <h2>
                步骤日志
                {job.status === "running" || job.status === "pending" ? (
                  <span className="muted" style={{ fontSize: "0.85rem", fontWeight: 400 }}>
                    {" "}
                    · 自动刷新中
                  </span>
                ) : null}
              </h2>
              {messages.length ? (
                <div className="job-log" role="log">
                  {messages.map((line, i) => {
                    const msg = asLogText(line.message);
                    const level = asLogText(line.level) || "INFO";
                    const time = asLogText(line.time);
                    return (
                      <div
                        key={`${i}-${time}-${msg.slice(0, 24)}`}
                        className={logLineClass(level)}
                      >
                        {time ? <span className="job-log-time">{time}</span> : null}
                        <span className="job-log-level">[{level}]</span>{" "}
                        <span className="job-log-msg">{msg}</span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="muted">暂无日志</p>
              )}
            </section>
          </div>
        ) : null}
      </StateBox>
    </>
  );
}
