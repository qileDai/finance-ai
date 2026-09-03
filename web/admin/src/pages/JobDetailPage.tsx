import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Image, Tag, Button, Modal, Tooltip } from "antd";
import { api, type JobDetailResponse, type JobField } from "../api";
import { formatDateTime } from "../format";
import { asLogText, logLineClass, normalizeLogLines } from "../jobLog";
import {
  JOB_FIELD_GROUP_LABELS,
  JOB_SHOT_PREVIEW,
  StateBox,
  jobCanCancel,
  jobCanRequeue,
  jobProgressTagColor,
  jobProgressTooltip,
  jobStatusLabel,
  jobStatusTagColor,
} from "../components/ui";

const ACTIVATION_TAG_COLOR: Record<string, string> = {
  "": "default",
  pending: "processing",
  activated: "success",
  failed: "error",
};

const ACTIVATION_LABEL: Record<string, string> = {
  "": "未激活",
  pending: "待激活",
  activated: "已激活",
  failed: "激活失败",
};

const FORM_TAG_COLOR: Record<string, string> = {
  "": "default",
  pending: "processing",
  filled: "success",
  failed: "error",
};

const FORM_LABEL: Record<string, string> = {
  "": "未填表",
  pending: "待填表",
  filled: "已填表",
  failed: "填表失败",
};

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
  const [formRetrying, setFormRetrying] = useState(false);

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

  function act(kind: "cancel" | "requeue") {
    if (kind === "cancel") {
      Modal.confirm({
        title: "确认取消？",
        content: `确认取消任务 #${jobId}？进行中的浏览器填表会立即停止。`,
        okText: "取消任务",
        okButtonProps: { danger: true },
        cancelText: "返回",
        onOk: () => runJobAct("cancel"),
      });
      return;
    }
    if (!window.confirm(`确认重跑任务 #${jobId}？`)) return;
    void runJobAct("requeue");
  }

  async function runJobAct(kind: "cancel" | "requeue") {
    const label = kind === "cancel" ? "取消" : "重跑";
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

  async function formRetry() {
    if (!window.confirm(`确认重跑填表任务 #${jobId}？`)) return;
    setFormRetrying(true);
    try {
      const res = await api.formRetryJob(jobId);
      onToast(res.message || "已重跑填表");
      onRefresh();
    } catch (e) {
      onToast((e as Error).message || "重跑填表失败");
    } finally {
      setFormRetrying(false);
    }
  }

  const job = detail?.job;
  const fields: JobField[] = detail?.fields || [];
  const messages = normalizeLogLines(detail?.messages);
  const progress = job?.progress || detail?.progress;

  const groupedFields: { group: string; label: string; items: JobField[] }[] = [];
  if (fields.length) {
    const byGroup = new Map<string, JobField[]>();
    for (const f of fields) {
      const g = f.group || "company";
      const list = byGroup.get(g) || [];
      list.push(f);
      byGroup.set(g, list);
    }
    const order = Object.keys(JOB_FIELD_GROUP_LABELS);
    const seen = new Set<string>();
    for (const g of order) {
      const items = byGroup.get(g);
      if (!items?.length) continue;
      seen.add(g);
      groupedFields.push({
        group: g,
        label: JOB_FIELD_GROUP_LABELS[g] || g,
        items,
      });
    }
    for (const [g, items] of byGroup) {
      if (seen.has(g) || !items.length) continue;
      groupedFields.push({ group: g, label: JOB_FIELD_GROUP_LABELS[g] || g, items });
    }
  }

  return (
    <>
      <div className="toolbar" style={{ gap: 12 }}>
        <Link to="/jobs">
          <Button size="small">← 返回列表</Button>
        </Link>
        {jobCanCancel(job?.status) ? (
          <Button
            size="small"
            danger
            disabled={busy}
            onClick={() => act("cancel")}
          >
            取消
          </Button>
        ) : null}
        {jobCanRequeue(job?.status) ? (
          <Button
            size="small"
            type="primary"
            disabled={busy}
            onClick={() => act("requeue")}
          >
            重跑
          </Button>
        ) : null}
      </div>

      <StateBox loading={loading} error={error} empty={!job}>
        {job ? (
          <div className="job-detail">
            <section className="reg-card">
              <h2>
                任务 #{job.id}{" "}
                <Tag color={jobStatusTagColor(job.status, job.review_status)}>
                  {jobStatusLabel(job.status, job.review_status)}
                </Tag>
                {progress?.label ? (
                  <Tooltip
                    title={
                      <span style={{ whiteSpace: "pre-line" }}>
                        {jobProgressTooltip(progress)}
                      </span>
                    }
                  >
                    <Tag color={jobProgressTagColor(progress)} style={{ marginLeft: 4 }}>
                      {progress.label}
                    </Tag>
                  </Tooltip>
                ) : null}
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
                  <dt>核对截图</dt>
                  <dd>
                    {job.esubmit_screenshot_path ? (
                      <Image
                        src={api.jobScreenshotUrl(job.id, "esubmit")}
                        width={100}
                        height={100}
                        style={{ objectFit: "cover" }}
                        preview={JOB_SHOT_PREVIEW}
                      />
                    ) : (
                      "-"
                    )}
                  </dd>
                </div>
                <div>
                  <dt>成功截图</dt>
                  <dd>
                    {job.success_screenshot_path ? (
                      <Image
                        src={api.jobScreenshotUrl(job.id, "success")}
                        width={100}
                        height={100}
                        style={{ objectFit: "cover" }}
                        preview={JOB_SHOT_PREVIEW}
                      />
                    ) : (
                      "-"
                    )}
                  </dd>
                </div>
                <div>
                  <dt>失败截图</dt>
                  <dd>
                    {job.screenshot_path ? (
                      <Image
                        src={api.jobScreenshotUrl(job.id, "fail")}
                        width={400}
                        style={{ objectFit: "contain" }}
                        preview={JOB_SHOT_PREVIEW}
                      />
                    ) : (
                      "-"
                    )}
                  </dd>
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
              <h2>激活与填表</h2>
              <dl className="job-meta">
                <div>
                  <dt>激活状态</dt>
                  <dd>
                    <Tag
                      color={ACTIVATION_TAG_COLOR[job.activation_status || ""] || "default"}
                    >
                      {ACTIVATION_LABEL[job.activation_status || ""] || "未激活"}
                    </Tag>
                  </dd>
                </div>
                <div>
                  <dt>填表状态</dt>
                  <dd>
                    <Tag color={FORM_TAG_COLOR[job.form_status || ""] || "default"}>
                      {FORM_LABEL[job.form_status || ""] || "未填表"}
                    </Tag>
                    {job.form_filled_at ? (
                      <span className="muted" style={{ marginLeft: 8 }}>
                        {formatDateTime(job.form_filled_at)}
                      </span>
                    ) : null}
                  </dd>
                </div>
                <div>
                  <dt>填表截图</dt>
                  <dd>
                    {job.form_screenshot_path ? (
                      <Image
                        src={api.jobScreenshotUrl(job.id, "form")}
                        width={100}
                        height={100}
                        style={{ objectFit: "cover" }}
                        preview={JOB_SHOT_PREVIEW}
                      />
                    ) : (
                      "-"
                    )}
                  </dd>
                </div>
              </dl>
              {job.form_status === "failed" ? (
                <div className="toolbar" style={{ gap: 12, marginTop: 8 }}>
                  <Button
                    size="small"
                    type="primary"
                    disabled={formRetrying}
                    loading={formRetrying}
                    onClick={formRetry}
                  >
                    重跑填表
                  </Button>
                </div>
              ) : null}
            </section>

            <section className="reg-card">
              <h2>填写字段</h2>
              {groupedFields.length ? (
                <div className="job-field-groups">
                  {groupedFields.map((g) => (
                    <div key={g.group}>
                      <h3>{g.label}</h3>
                      <dl className="job-meta">
                        {g.items.map((f) => (
                          <div key={f.key}>
                            <dt>{f.label || f.key}</dt>
                            <dd style={{ wordBreak: "break-all" }}>{f.value}</dd>
                          </div>
                        ))}
                      </dl>
                    </div>
                  ))}
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
