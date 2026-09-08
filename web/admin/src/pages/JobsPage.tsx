import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type JobRow } from "../api";
import { formatDateTime } from "../format";
import { StateBox, JobPipelineLights, jobCanCancel, jobCanFormRetry, jobCanRequeue, jobProgressTagColor, jobStatusLabel, jobStatusTagColor } from "../components/ui";
import { DraggableShot, jobShotFilename } from "../components/DraggableShot";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { CopyOutlined, SearchOutlined, ReloadOutlined, FolderOpenOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import {
  clearStoredDirectory,
  directoryName,
  ensureWritePermission,
  isDirectoryPickerSupported,
  loadStoredDirectory,
  pickDirectory,
  type ShotDirHandle,
} from "../lib/saveFolder";
import { useMessageApi } from "../useMessageApi";

type Props = {
  refreshKey: number;
  onRefresh: () => void;
};

const POLL_SEC = 20;

async function copyText(text: string): Promise<boolean> {
  const cleaned = (text || "").trim();
  if (!cleaned) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(cleaned);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = cleaned;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

const STATUS_OPTIONS = [
  { value: "", label: "全部" },
  { value: "pending", label: "待处理" },
  { value: "running", label: "进行中" },
  { value: "awaiting_review", label: "待审核" },
  { value: "succeeded", label: "已成功" },
  { value: "failed", label: "已失败" },
  { value: "cancelled", label: "已取消" },
];

export function JobsPage({ refreshKey, onRefresh }: Props) {
  const message = useMessageApi();
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<JobRow[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);
  const sortKey = "id" as const;
  const sortAsc = false;
  // 搜索条件（实际触发查询的值）
  const [companyName, setCompanyName] = useState("");
  const [directorName, setDirectorName] = useState("");
  const [idNumber, setIdNumber] = useState("");
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  // 输入框值（点搜索才同步）
  const [companyInput, setCompanyInput] = useState("");
  const [directorInput, setDirectorInput] = useState("");
  const [idInput, setIdInput] = useState("");
  const nav = useNavigate();
  const folderOk = isDirectoryPickerSupported();
  const [saveDir, setSaveDir] = useState<ShotDirHandle | null>(null);
  const [zoneHover, setZoneHover] = useState(false);
  const zoneRef = useRef<HTMLDivElement | null>(null);
  const [zoneEl, setZoneEl] = useState<HTMLDivElement | null>(null);
  const [countdown, setCountdown] = useState(POLL_SEC);
  const countdownRef = useRef(POLL_SEC);
  const loadGen = useRef(0);

  const loadJobs = useCallback(async (opts?: { silent?: boolean }) => {
    const silent = Boolean(opts?.silent);
    const gen = ++loadGen.current;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    const dateFrom = dateRange?.[0]?.format("YYYY-MM-DD") || "";
    const dateTo = dateRange?.[1]?.format("YYYY-MM-DD") || "";
    try {
      const d = await api.jobs(
        status,
        80,
        "",
        dateFrom,
        dateTo,
        companyName,
        directorName,
        idNumber,
      );
      if (gen !== loadGen.current) return;
      setItems(d.items || []);
      if (!silent) setError(null);
    } catch (e) {
      if (gen !== loadGen.current) return;
      if (!silent) setError((e as Error).message);
    } finally {
      if (gen === loadGen.current && !silent) setLoading(false);
    }
  }, [status, companyName, directorName, idNumber, dateRange]);

  const resetCountdown = useCallback(() => {
    countdownRef.current = POLL_SEC;
    setCountdown(POLL_SEC);
  }, []);

  useEffect(() => {
    void loadJobs({ silent: false });
    resetCountdown();
  }, [loadJobs, refreshKey, resetCountdown]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const next = countdownRef.current - 1;
      if (next <= 0) {
        countdownRef.current = POLL_SEC;
        setCountdown(POLL_SEC);
        void loadJobs({ silent: true });
      } else {
        countdownRef.current = next;
        setCountdown(next);
      }
    }, 1000);
    return () => window.clearInterval(id);
  }, [loadJobs]);

  useEffect(() => {
    let alive = true;
    if (!folderOk) return;
    loadStoredDirectory().then((handle) => {
      if (alive && handle) setSaveDir(handle);
    });
    return () => {
      alive = false;
    };
  }, [folderOk]);

  useEffect(() => {
    if (!saveDir) return;
    const grant = () => {
      void ensureWritePermission(saveDir);
    };
    window.addEventListener("pointerdown", grant, { capture: true, once: true });
    return () => window.removeEventListener("pointerdown", grant, { capture: true });
  }, [saveDir]);

  const pickSaveFolder = useCallback(async (): Promise<ShotDirHandle | null> => {
    try {
      const handle = await pickDirectory();
      if (handle) setSaveDir(handle);
      return handle;
    } catch (e) {
      message.error(e instanceof Error ? e.message : "选择文件夹失败");
      return null;
    }
  }, []);

  const clearSaveFolder = useCallback(() => {
    setSaveDir(null);
    void clearStoredDirectory();
    message.success("已清除保存文件夹");
  }, []);

  const onHoverZone = useCallback((hover: boolean) => {
    setZoneHover(hover);
  }, []);

  const setZone = useCallback((el: HTMLDivElement | null) => {
    zoneRef.current = el;
    setZoneEl(el);
  }, []);

  const sorted = useMemo(() => {
    const copy = [...items];
    copy.sort((a, b) => {
      const av = String(a[sortKey] ?? "");
      const bv = String(b[sortKey] ?? "");
      if (sortKey === "id") {
        return sortAsc ? a.id - b.id : b.id - a.id;
      }
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    return copy;
  }, [items, sortKey, sortAsc]);

  const tableHoldRef = useRef<HTMLDivElement>(null);
  const [bodyY, setBodyY] = useState<number>();
  useLayoutEffect(() => {
    const el = tableHoldRef.current;
    if (!el) return;
    const measure = () => {
      const pag = el.querySelector(".ant-table-pagination") as HTMLElement | null;
      const header =
        (el.querySelector(".ant-table-header") as HTMLElement | null) ||
        (el.querySelector(".ant-table-thead") as HTMLElement | null);
      const pagH = pag ? pag.offsetHeight + 12 : 56;
      const headerH = header?.offsetHeight ?? 40;
      setBodyY(Math.max(120, el.clientHeight - pagH - headerH - 12));
    };
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, [loading, sorted.length]);

  function onSearch() {
    setCompanyName(companyInput.trim());
    setDirectorName(directorInput.trim());
    setIdNumber(idInput.trim());
  }

  function onReset() {
    setCompanyInput("");
    setDirectorInput("");
    setIdInput("");
    setDateRange(null);
    setStatus("");
    setCompanyName("");
    setDirectorName("");
    setIdNumber("");
  }

  function onManualRefresh() {
    resetCountdown();
    void loadJobs({ silent: false });
  }

  async function runAct(
    id: number,
    kind: "cancel" | "requeue" | "approve" | "reject" | "formRetry",
  ) {
    const labelMap: Record<typeof kind, string> = {
      cancel: "取消",
      requeue: "重跑",
      approve: "提交审核",
      reject: "拒绝审核",
      formRetry: "重跑填表",
    };
    const label = labelMap[kind];
    setBusyId(id);
    try {
      let res: { message?: string } | null = null;
      if (kind === "cancel") res = await api.cancelJob(id);
      else if (kind === "requeue") res = await api.requeueJob(id);
      else if (kind === "approve") res = await api.approveJob(id);
      else if (kind === "reject") res = await api.rejectJob(id);
      else if (kind === "formRetry") res = await api.formRetryJob(id);
      message.success(res?.message || `${label}成功`);
      onRefresh();
    } catch (e) {
      message.error((e as Error).message || `${label}失败`);
    } finally {
      setBusyId(null);
    }
  }

  function act(
    id: number,
    kind: "cancel" | "requeue" | "approve" | "reject" | "formRetry",
  ) {
    if (kind === "approve") {
      Modal.confirm({
        title: "确认提交？",
        content: `确认提交任务 #${id}？审核通过后将继续 ICRIS 注册。`,
        okText: "提交",
        cancelText: "取消",
        onOk: () => runAct(id, "approve"),
      });
      return;
    }
    if (kind === "reject") {
      Modal.confirm({
        title: "确认拒绝？",
        content: `确认拒绝任务 #${id}？任务将标记为已拒绝，不会自动重跑。`,
        okText: "拒绝",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: () => runAct(id, "reject"),
      });
      return;
    }
    if (kind === "cancel") {
      Modal.confirm({
        title: "确认取消？",
        content: `确认取消任务 #${id}？进行中的浏览器填表会立即停止。`,
        okText: "取消任务",
        okButtonProps: { danger: true },
        cancelText: "返回",
        onOk: () => runAct(id, "cancel"),
      });
      return;
    }
    if (kind === "formRetry") {
      if (!window.confirm(`确认重跑填表任务 #${id}？`)) return;
      void runAct(id, "formRetry");
      return;
    }
    const label = "重跑";
    if (!window.confirm(`确认${label}任务 #${id}？`)) return;
    void runAct(id, kind);
  }

  const columns = [
    {
      title: "ID",
      dataIndex: "id",
      key: "id",
      width: 80,
      fixed: "left" as const,
      render: (id: number) => (
        <Link to={`/jobs/${id}`} onClick={(e) => e.stopPropagation()}>
          #{id}
        </Link>
      ),
    },
    {
      title: "公司名称",
      key: "company_name",
      width: 200,
      render: (_: unknown, r: JobRow) => {
        const en = (r.company_name_en || "").trim();
        const cn = (r.company_name_cn || r.company_name || "").trim();
        if (!en && !cn) return "-";
        if (en && cn) {
          return (
            <div className="job-company-name">
              <div>{en}</div>
              <div className="muted">{cn}</div>
            </div>
          );
        }
        return en || cn;
      },
    },
    {
      title: "姓名",
      dataIndex: "director_name",
      key: "director_name",
      width: 80,
      ellipsis: { showTitle: false },
      onHeaderCell: () => ({ style: { maxWidth: 80 } }),
      onCell: () => ({ style: { maxWidth: 80 } }),
      render: (v: string) => {
        const name = (v || "").trim() || "-";
        return (
          <Tooltip title={name === "-" ? "" : name}>
            <span className="job-director-name">{name}</span>
          </Tooltip>
        );
      },
    },
    {
      title: "证件类型",
      dataIndex: "id_type",
      key: "id_type",
      width: 100,
      render: (v: string) => v || "-",
    },
    {
      title: "证件号码",
      dataIndex: "id_number",
      key: "id_number",
      width: 180,
      render: (v: string, r: JobRow) => (
        <div>
          <span className="mono">{v || "-"}</span>
          {Number(r.id_already_registered) ? (
            <div>
              <Tag color="error" style={{ marginTop: 4 }}>
                已被注册
              </Tag>
            </div>
          ) : null}
        </div>
      ),
    },
    {
      title: "账号",
      key: "icris_account",
      width: 200,
      render: (_: unknown, r: JobRow) => {
        const user = (r.icris_username || "").trim();
        const pass = (r.icris_password || "").trim();
        if (!user && !pass) return "-";
        const copyLine = async (value: string, label: string) => {
          const ok = await copyText(value);
          if (ok) message.success(`已复制${label}`);
          else message.error(`复制${label}失败`);
        };
        const line = (value: string, label: string) => (
          <div className="job-cred-row">
            <span className="mono">{value}</span>
            <Tooltip title={`复制${label}`}>
              <Button
                type="text"
                size="small"
                className="job-cred-copy"
                icon={<CopyOutlined />}
                onClick={(e) => {
                  e.stopPropagation();
                  void copyLine(value, label);
                }}
              />
            </Tooltip>
          </div>
        );
        return (
          <div className="job-cred-stack">
            {user ? line(user, "用户名") : null}
            {pass ? line(pass, "密码") : null}
          </div>
        );
      },
    },
    {
      title: "进度",
      key: "progress",
      width: 88,
      onHeaderCell: () => ({ style: { maxWidth: 88 } }),
      onCell: () => ({ style: { maxWidth: 88 } }),
      render: (_: unknown, r: JobRow) => (
        <Tag color={jobProgressTagColor(r.progress)}>{r.progress?.label || "-"}</Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 80,
      onHeaderCell: () => ({ style: { maxWidth: 80 } }),
      onCell: () => ({ style: { maxWidth: 80 } }),
      render: (_: unknown, r: JobRow) => (
        <Tag color={jobStatusTagColor(r.status, r.review_status)}>
          {jobStatusLabel(r.status, r.review_status)}
        </Tag>
      ),
    },
    {
      title: "核对截图",
      key: "esubmit_screenshot",
      width: 120,
      render: (_: unknown, r: JobRow) =>
        r.esubmit_screenshot_path ? (
          <DraggableShot
            src={api.jobScreenshotUrl(r.id, "esubmit")}
            filename={jobShotFilename(r.id, "esubmit")}
            dir={saveDir}
            zoneEl={zoneEl}
            ensureFolder={pickSaveFolder}
            onSaved={(name) => message.success(`已保存 ${name}`)}
            onError={(msg) => message.error(msg)}
            onHoverZone={onHoverZone}
          />
        ) : (
          "-"
        ),
    },
    {
      title: "成功截图",
      key: "success_screenshot",
      width: 120,
      render: (_: unknown, r: JobRow) =>
        r.success_screenshot_path ? (
          <DraggableShot
            src={api.jobScreenshotUrl(r.id, "success")}
            filename={jobShotFilename(r.id, "success")}
            dir={saveDir}
            zoneEl={zoneEl}
            ensureFolder={pickSaveFolder}
            onSaved={(name) => message.success(`已保存 ${name}`)}
            onError={(msg) => message.error(msg)}
            onHoverZone={onHoverZone}
          />
        ) : (
          "-"
        ),
    },
    {
      title: "填表截图",
      key: "form_screenshot",
      width: 160,
      render: (_: unknown, r: JobRow) =>
        r.form_screenshot_path ? (
          <DraggableShot
            src={api.jobScreenshotUrl(r.id, "form")}
            filename={jobShotFilename(r.id, "form")}
            dir={saveDir}
            zoneEl={zoneEl}
            ensureFolder={pickSaveFolder}
            onSaved={(name) => message.success(`已保存 ${name}`)}
            onError={(msg) => message.error(msg)}
            onHoverZone={onHoverZone}
          />
        ) : (
          "-"
        ),
    },
    {
      title: "错误",
      dataIndex: "last_error",
      key: "last_error",
      width: 280,
      render: (v: string) =>
        v ? (
          <Tooltip
            title={
              <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {v}
              </span>
            }
          >
            <div className="job-error-cell">{v}</div>
          </Tooltip>
        ) : (
          "-"
        ),
    },
    {
      title: "到 s03a",
      dataIndex: "s03a_duration",
      key: "s03a_duration",
      width: 100,
      render: (v: string) => (
        <span className="mono muted">{v || "-"}</span>
      ),
    },
    {
      title: "整个任务",
      dataIndex: "run_duration",
      key: "run_duration",
      width: 100,
      render: (v: string) => (
        <span className="mono muted">{v || "-"}</span>
      ),
    },
    {
      title: "nnc1填表",
      dataIndex: "nnc1_duration",
      key: "nnc1_duration",
      width: 110,
      render: (v: string) => (
        <span className="mono muted">{v || "-"}</span>
      ),
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 160,
      render: (v: string) => (
        <span className="mono muted">{formatDateTime(v)}</span>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 240,
      fixed: "right" as const,
      render: (_: unknown, r: JobRow) => (
        <Space
          onClick={(e) => e.stopPropagation()}
        >
          {jobCanCancel(r.status) ? (
            <Button
              size="small"
              danger
              disabled={busyId === r.id}
              onClick={() => act(r.id, "cancel")}
            >
              取消
            </Button>
          ) : null}
          {jobCanRequeue(r.status) ? (
            <Button
              size="small"
              type="primary"
              disabled={busyId === r.id}
              onClick={() => act(r.id, "requeue")}
            >
              重跑
            </Button>
          ) : null}
          {jobCanFormRetry(r.form_status) ? (
            <Button
              size="small"
              type="primary"
              disabled={busyId === r.id}
              onClick={() => act(r.id, "formRetry")}
            >
              重跑填表
            </Button>
          ) : null}
          {r.status === "awaiting_review" ? (
            <>
              <Button
                size="small"
                type="primary"
                disabled={busyId === r.id}
                onClick={() => act(r.id, "approve")}
              >
                提交
              </Button>
              <Button
                size="small"
                danger
                disabled={busyId === r.id}
                onClick={() => act(r.id, "reject")}
              >
                拒绝
              </Button>
            </>
          ) : null}
          {jobCanCancel(r.status) ||
          jobCanRequeue(r.status) ||
          jobCanFormRetry(r.form_status) ||
          r.status === "awaiting_review"
            ? null
            : "-"}
        </Space>
      ),
    },
  ];

  return (
    <div className="jobs-page">
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap size="middle">
          <Input
            placeholder="公司名"
            value={companyInput}
            onChange={(e) => setCompanyInput(e.target.value)}
            onPressEnter={onSearch}
            style={{ width: 200 }}
            allowClear
          />
          <Input
            placeholder="姓名"
            value={directorInput}
            onChange={(e) => setDirectorInput(e.target.value)}
            onPressEnter={onSearch}
            style={{ width: 160 }}
            allowClear
          />
          <Input
            placeholder="身份证号"
            value={idInput}
            onChange={(e) => setIdInput(e.target.value)}
            onPressEnter={onSearch}
            style={{ width: 200 }}
            allowClear
          />
          <Select
            value={status}
            onChange={setStatus}
            options={STATUS_OPTIONS}
            style={{ width: 120 }}
          />
          <DatePicker.RangePicker
            value={dateRange as [dayjs.Dayjs, dayjs.Dayjs] | null}
            onChange={(range) => setDateRange(range as [dayjs.Dayjs | null, dayjs.Dayjs | null] | null)}
            style={{ width: 240 }}
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={onSearch}>
            搜索
          </Button>
          <Button onClick={onReset}>重置</Button>
          <Button icon={<ReloadOutlined />} onClick={onManualRefresh}>
            刷新 {countdown}s
          </Button>
          {folderOk ? (
            <>
              <Button icon={<FolderOpenOutlined />} onClick={() => void pickSaveFolder()}>
                {saveDir ? "更换文件夹" : "选择保存文件夹"}
              </Button>
              {saveDir ? (
                <Button type="link" onClick={clearSaveFolder}>
                  清除
                </Button>
              ) : null}
            </>
          ) : null}
        </Space>
      </Card>

      {!folderOk ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="当前不是安全连接（公网 http 或非 Chrome）。浏览器禁止选择本地文件夹。点「保存」会下载到浏览器默认下载目录；要拖到指定文件夹请用 https 或本机 http://127.0.0.1"
        />
      ) : (
        <div
          ref={setZone}
          className={`shot-save-zone${zoneHover ? " is-hover" : ""}`}
        >
          {saveDir
            ? `把核对/成功截图拖到这里，保存到「${directoryName(saveDir)}」；也可点缩略图旁的保存`
            : "点「保存」可选文件夹（选过一次后会记住，刷新不用再点同意）；也可先选文件夹再拖到这里"}
        </div>
      )}
      <div className="jobs-table-hold" ref={tableHoldRef}>
        <StateBox loading={loading} error={error} empty={!sorted.length}>
          <Table
            dataSource={sorted}
            columns={columns}
            rowKey="id"
            size="small"
            scroll={{ x: "max-content", ...(bodyY ? { y: bodyY } : {}) }}
            pagination={{ pageSize: 10, showSizeChanger: false }}
            expandable={{
              expandedRowRender: (r: JobRow) => (
                <div className="job-expand">
                  <JobPipelineLights progress={r.progress} />
                  <div className="job-expand-meta">
                    来源 {r.source || "-"}
                    {" · "}
                    尝试 {r.attempts ?? 0}/{r.max_attempts ?? 0}
                    {" · "}
                    dry/submit {r.dry_run ? "Y" : "N"}/{r.allow_submit ? "Y" : "N"}
                  </div>
                  {r.fields?.length ? (
                    <dl className="job-expand-fields">
                      {r.fields.map((f) => (
                        <div key={f.key}>
                          <dt>{f.label || f.key}</dt>
                          <dd>{f.value}</dd>
                        </div>
                      ))}
                    </dl>
                  ) : (
                    <p className="muted">无字段快照</p>
                  )}
                </div>
              ),
            }}
          />
        </StateBox>
      </div>
    </div>
  );
}
