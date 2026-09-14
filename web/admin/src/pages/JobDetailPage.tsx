import { useEffect, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { Image, Tag, Button, Modal, Tooltip } from "antd";
import { api, type JobDetailResponse, type JobField, type JobLogLine } from "../api";
import { formatDateTime } from "../format";
import { asLogText, logLineClass, normalizeLogLines, splitLogPhases } from "../jobLog";
import {
  JOB_FIELD_GROUP_LABELS,
  JOB_SHOT_PREVIEW,
  StateBox,
  jobCanCancel,
  jobCanFormRetry,
  jobCanRequeue,
  jobProgressTagColor,
  jobProgressTooltip,
} from "../components/ui";
import { useMessageApi } from "../useMessageApi";

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
  onRefresh: () => void;
};

function JobLogBlock({
  title,
  lines,
  live,
}: {
  title: string;
  lines: JobLogLine[];
  live?: boolean;
}) {
  return (
    <section className="reg-card">
      <h2>
        {title}
        {live ? (
          <span className="muted" style={{ fontSize: "0.85rem", fontWeight: 400 }}>
            {" "}
            · 自动刷新中
          </span>
        ) : null}
      </h2>
      {lines.length ? (
        <div className="job-log" role="log">
          {lines.map((line, i) => {
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
  );
}

function SummaryPairs({
  items,
}: {
  items: Array<{ k: string; v: ReactNode; mono?: boolean }>;
}) {
  return (
    <dl>
      {items.map((it) => (
        <div key={it.k}>
          <dt>{it.k}</dt>
          <dd className={it.mono ? "mono" : undefined}>{it.v || "-"}</dd>
        </div>
      ))}
    </dl>
  );
}

function SummaryShot({
  label,
  src,
}: {
  label: string;
  src: string | null;
}) {
  return (
    <div className="job-summary-shot">
      <span>{label}</span>
      {src ? (
        <Image
          src={src}
          width={96}
          height={96}
          style={{ objectFit: "cover" }}
          preview={JOB_SHOT_PREVIEW}
        />
      ) : (
        <span className="job-summary-shot-empty">无</span>
      )}
    </div>
  );
}

export function JobDetailPage({ refreshKey, onRefresh }: Props) {
  const message = useMessageApi();
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
  const activationStatus = detail?.job?.activation_status;
  const formStatus = detail?.job?.form_status;
  const logsLive =
    jobStatus === "pending" ||
    jobStatus === "running" ||
    activationStatus === "pending" ||
    activationStatus === "activating" ||
    formStatus === "pending";
  useEffect(() => {
    if (!Number.isFinite(jobId) || jobId <= 0) return;
    if (!logsLive) return;
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
  }, [jobId, logsLive]);

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
    Modal.confirm({
      title: "确认重跑？",
      content: `确认重跑任务 #${jobId}？`,
      okText: "重跑",
      cancelText: "取消",
      onOk: () => runJobAct("requeue"),
    });
  }

  async function runJobAct(kind: "cancel" | "requeue") {
    const label = kind === "cancel" ? "取消" : "重跑";
    setBusy(true);
    try {
      const res =
        kind === "cancel" ? await api.cancelJob(jobId) : await api.requeueJob(jobId);
      message.success(res.message || `${label}成功`);
      onRefresh();
    } catch (e) {
      message.error((e as Error).message || `${label}失败`);
    } finally {
      setBusy(false);
    }
  }

  function confirmFormRetry() {
    Modal.confirm({
      title: "确认重跑填表？",
      content: `确认重跑填表任务 #${jobId}？`,
      okText: "重跑填表",
      cancelText: "取消",
      onOk: () => formRetry(),
    });
  }

  async function formRetry() {
    setFormRetrying(true);
    try {
      const res = await api.formRetryJob(jobId);
      message.success(res.message || "已重跑填表");
      onRefresh();
    } catch (e) {
      message.error((e as Error).message || "重跑填表失败");
    } finally {
      setFormRetrying(false);
    }
  }

  const job = detail?.job;
  const fields: JobField[] = detail?.fields || [];
  const messages = normalizeLogLines(detail?.messages);
  const logPhases = splitLogPhases(messages, {
    formStatus: job?.form_status,
    activationStatus: job?.activation_status,
  });
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
            <section className="reg-card job-summary-card">
              <h2>
                任务 #{job.id}{" "}
                {progress?.label ? (
                  <Tooltip
                    title={
                      <span style={{ whiteSpace: "pre-line" }}>
                        {jobProgressTooltip(progress)}
                      </span>
                    }
                  >
                    <Tag color={jobProgressTagColor(progress)}>
                      {progress.label}
                    </Tag>
                  </Tooltip>
                ) : null}
              </h2>
              <div className="job-summary">
                <div className="job-summary-row">
                  <span>概要</span>
                  <SummaryPairs
                    items={[
                      { k: "来源", v: job.source || "-", mono: true },
                      { k: "公司", v: job.company_name || "-" },
                      {
                        k: "尝试",
                        v: `${job.attempts ?? 0}/${job.max_attempts ?? 0}`,
                        mono: true,
                      },
                      {
                        k: "dry / submit",
                        v: `${job.dry_run ? "Y" : "N"} / ${job.allow_submit ? "Y" : "N"}`,
                        mono: true,
                      },
                    ]}
                  />
                </div>
                <div className="job-summary-row">
                  <span>时间</span>
                  <SummaryPairs
                    items={[
                      { k: "创建", v: formatDateTime(job.created_at), mono: true },
                      { k: "开始", v: formatDateTime(job.started_at), mono: true },
                      { k: "结束", v: formatDateTime(job.finished_at), mono: true },
                      { k: "更新", v: formatDateTime(job.updated_at), mono: true },
                    ]}
                  />
                </div>
                <div className="job-summary-row">
                  <span>耗时</span>
                  <SummaryPairs
                    items={[
                      { k: "到 s03a", v: job.s03a_duration || "-", mono: true },
                      { k: "整个任务", v: job.run_duration || "-", mono: true },
                      { k: "nnc1填表", v: job.nnc1_duration || "-", mono: true },
                    ]}
                  />
                </div>
                <div className="job-summary-row">
                  <span>路径</span>
                  <SummaryPairs
                    items={[
                      { k: "roomid", v: job.roomid || "-", mono: true },
                      { k: "材料包", v: job.package_dir || "-", mono: true },
                    ]}
                  />
                </div>
                <div className="job-summary-shots">
                  <SummaryShot
                    label="核对截图"
                    src={
                      job.esubmit_screenshot_path
                        ? api.jobScreenshotUrl(job.id, "esubmit")
                        : null
                    }
                  />
                  <SummaryShot
                    label="成功截图"
                    src={
                      job.success_screenshot_path
                        ? api.jobScreenshotUrl(job.id, "success")
                        : null
                    }
                  />
                  <SummaryShot
                    label="失败截图"
                    src={
                      job.screenshot_path
                        ? api.jobScreenshotUrl(job.id, "fail")
                        : null
                    }
                  />
                </div>
              </div>
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
              {jobCanFormRetry(job.form_status) ? (
                <div className="toolbar" style={{ gap: 12, marginTop: 8 }}>
                  <Button
                    size="small"
                    type="primary"
                    disabled={formRetrying}
                    loading={formRetrying}
                    onClick={confirmFormRetry}
                  >
                    重跑填表
                  </Button>
                </div>
              ) : null}
            </section>

            <section className="reg-card job-fields-card">
              <h2>填写字段</h2>
              {groupedFields.length ? (
                <div className="job-field-groups">
                  {groupedFields.map((g) => (
                    <div key={g.group} className="job-field-group">
                      <h3>{g.label}</h3>
                      <dl className="job-field-list">
                        {g.items.map((f) => (
                          <div key={f.key}>
                            <dt>{f.label || f.key}</dt>
                            <dd>{f.value}</dd>
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

            <JobLogBlock
              title="注册日志"
              lines={logPhases.register}
              live={job.status === "pending" || job.status === "running"}
            />
            <JobLogBlock
              title="账号激活日志"
              lines={logPhases.activation}
              live={
                job.activation_status === "pending" ||
                job.activation_status === "activating"
              }
            />
            <JobLogBlock
              title="NNC1 填表日志"
              lines={logPhases.nnc1}
              live={job.form_status === "pending"}
            />
          </div>
        ) : null}
      </StateBox>
    </>
  );
}
