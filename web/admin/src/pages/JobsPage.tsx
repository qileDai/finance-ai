import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type JobRow } from "../api";
import { formatDateTime } from "../format";
import { StateBox, jobCanCancel, jobCanRequeue, jobStatusLabel, jobStatusTagColor } from "../components/ui";
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
import { SearchOutlined, ReloadOutlined, FolderOpenOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import {
  clearStoredDirectory,
  directoryName,
  isDirectoryPickerSupported,
  loadStoredDirectory,
  pickDirectory,
  type ShotDirHandle,
} from "../lib/saveFolder";

type Props = {
  refreshKey: number;
  onToast: (msg: string) => void;
  onRefresh: () => void;
};

const STATUS_OPTIONS = [
  { value: "", label: "全部" },
  { value: "pending", label: "待处理" },
  { value: "running", label: "进行中" },
  { value: "awaiting_review", label: "待审核" },
  { value: "succeeded", label: "已成功" },
  { value: "failed", label: "已失败" },
  { value: "cancelled", label: "已取消" },
];

export function JobsPage({ refreshKey, onToast, onRefresh }: Props) {
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

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    const dateFrom = dateRange?.[0]?.format("YYYY-MM-DD") || "";
    const dateTo = dateRange?.[1]?.format("YYYY-MM-DD") || "";
    api
      .jobs(status, 80, "", dateFrom, dateTo, companyName, directorName, idNumber)
      .then((d) => {
        if (alive) setItems(d.items || []);
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
  }, [status, refreshKey, companyName, directorName, idNumber, dateRange]);

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

  const pickSaveFolder = useCallback(async (): Promise<ShotDirHandle | null> => {
    try {
      const handle = await pickDirectory();
      if (handle) setSaveDir(handle);
      return handle;
    } catch (e) {
      onToast(e instanceof Error ? e.message : "选择文件夹失败");
      return null;
    }
  }, [onToast]);

  const clearSaveFolder = useCallback(() => {
    setSaveDir(null);
    void clearStoredDirectory();
    onToast("已清除保存文件夹");
  }, [onToast]);

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

  async function runAct(
    id: number,
    kind: "cancel" | "requeue" | "approve" | "reject",
  ) {
    const labelMap: Record<typeof kind, string> = {
      cancel: "取消",
      requeue: "重跑",
      approve: "提交审核",
      reject: "拒绝审核",
    };
    const label = labelMap[kind];
    setBusyId(id);
    try {
      let res: { message?: string } | null = null;
      if (kind === "cancel") res = await api.cancelJob(id);
      else if (kind === "requeue") res = await api.requeueJob(id);
      else if (kind === "approve") res = await api.approveJob(id);
      else if (kind === "reject") res = await api.rejectJob(id);
      onToast(res?.message || `${label}成功`);
      onRefresh();
    } catch (e) {
      onToast((e as Error).message || `${label}失败`);
    } finally {
      setBusyId(null);
    }
  }

  function act(
    id: number,
    kind: "cancel" | "requeue" | "approve" | "reject",
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
    const label = kind === "cancel" ? "取消" : "重跑";
    if (!window.confirm(`确认${label}任务 #${id}？`)) return;
    void runAct(id, kind);
  }

  const columns = [
    {
      title: "ID",
      dataIndex: "id",
      key: "id",
      width: 80,
      render: (id: number) => (
        <Link to={`/jobs/${id}`} onClick={(e) => e.stopPropagation()}>
          #{id}
        </Link>
      ),
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 80,
      render: (v: string) => v || "-",
    },
    {
      title: "公司中文名",
      key: "company_name_cn",
      width: 160,
      render: (_: unknown, r: JobRow) => r.company_name_cn || r.company_name || "-",
    },
    {
      title: "公司英文名",
      dataIndex: "company_name_en",
      key: "company_name_en",
      width: 180,
      render: (v: string) => v || "-",
    },
    {
      title: "姓名",
      dataIndex: "director_name",
      key: "director_name",
      width: 120,
      render: (v: string) => v || "-",
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
      width: 160,
      render: (v: string) => <span className="mono">{v || "-"}</span>,
    },
    {
      title: "用户名",
      dataIndex: "icris_username",
      key: "icris_username",
      width: 140,
      render: (v: string) => <span className="mono">{v || "-"}</span>,
    },
    {
      title: "密码",
      dataIndex: "icris_password",
      key: "icris_password",
      width: 140,
      render: (v: string) => <span className="mono">{v || "-"}</span>,
    },
    {
      title: "核对截图",
      key: "esubmit_screenshot",
      width: 160,
      render: (_: unknown, r: JobRow) =>
        r.esubmit_screenshot_path ? (
          <DraggableShot
            src={api.jobScreenshotUrl(r.id, "esubmit")}
            filename={jobShotFilename(r.id, "esubmit")}
            dir={saveDir}
            zoneEl={zoneEl}
            ensureFolder={pickSaveFolder}
            onSaved={(name) => onToast(`已保存 ${name}`)}
            onError={onToast}
            onHoverZone={onHoverZone}
          />
        ) : (
          "-"
        ),
    },
    {
      title: "成功截图",
      key: "success_screenshot",
      width: 160,
      render: (_: unknown, r: JobRow) =>
        r.success_screenshot_path ? (
          <DraggableShot
            src={api.jobScreenshotUrl(r.id, "success")}
            filename={jobShotFilename(r.id, "success")}
            dir={saveDir}
            zoneEl={zoneEl}
            ensureFolder={pickSaveFolder}
            onSaved={(name) => onToast(`已保存 ${name}`)}
            onError={onToast}
            onHoverZone={onHoverZone}
          />
        ) : (
          "-"
        ),
    },
    {
      title: "激活/填表",
      dataIndex: "activation_status",
      key: "activation_form",
      width: 100,
      render: (_: unknown, r: JobRow) => {
        if (r.status !== "succeeded") return "-";
        const act = r.activation_status || "";
        const form = r.form_status || "";
        if (form === "filled") return <Tag color="success">已填表</Tag>;
        if (form === "failed") return <Tag color="error">填表失败</Tag>;
        if (form === "pending") return <Tag color="warning">待填表</Tag>;
        if (act === "activated") return <Tag color="processing">已激活</Tag>;
        if (act === "pending") return <Tag color="warning">待激活</Tag>;
        if (act === "failed") return <Tag color="error">激活失败</Tag>;
        return <Tag>未激活</Tag>;
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (_: unknown, r: JobRow) => (
        <Tag color={jobStatusTagColor(r.status, r.review_status)}>
          {jobStatusLabel(r.status, r.review_status)}
        </Tag>
      ),
    },
    {
      title: "尝试",
      key: "attempts",
      width: 80,
      render: (_: unknown, r: JobRow) => (
        <span className="mono">
          {r.attempts ?? 0}/{r.max_attempts ?? 0}
        </span>
      ),
    },
    {
      title: "dry/submit",
      key: "dry_submit",
      width: 100,
      render: (_: unknown, r: JobRow) => (
        <span className="mono">
          {r.dry_run ? "Y" : "N"}/{r.allow_submit ? "Y" : "N"}
        </span>
      ),
    },
    {
      title: "错误",
      dataIndex: "last_error",
      key: "last_error",
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v || ""}>
          {String(v || "").slice(0, 60)}
        </Tooltip>
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
      width: 180,
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
          {r.status !== "pending" &&
          r.status !== "running" &&
          r.status !== "failed" &&
          r.status !== "cancelled" &&
          r.status !== "awaiting_review"
            ? "-"
            : null}
        </Space>
      ),
    },
  ];

  return (
    <>
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
          <Button icon={<ReloadOutlined />} onClick={onReset}>
            重置
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
          message="当前浏览器不支持保存到本地文件夹，请使用 Chrome 或 Edge"
        />
      ) : (
        <div
          ref={setZone}
          className={`shot-save-zone${zoneHover ? " is-hover" : ""}`}
        >
          {saveDir
            ? `把核对/成功截图拖到这里，保存到「${directoryName(saveDir)}」；也可点缩略图旁的保存`
            : "请先点「选择保存文件夹」，再把截图拖到这里或点保存"}
        </div>
      )}
      <StateBox loading={loading} error={error} empty={!sorted.length}>
        <Table
          dataSource={sorted}
          columns={columns}
          rowKey="id"
          size="small"
          scroll={{ x: "max-content", y: 500 }}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          style={{ marginBottom: 20 }}
        />
      </StateBox>
    </>
  );
}
