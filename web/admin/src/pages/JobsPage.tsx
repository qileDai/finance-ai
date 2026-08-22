import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type JobRow } from "../api";
import { formatDateTime } from "../format";
import { StateBox } from "../components/ui";
import {
  Button,
  Card,
  DatePicker,
  Image,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { SearchOutlined, ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";

type Props = {
  refreshKey: number;
  onToast: (msg: string) => void;
  onRefresh: () => void;
};

const STATUS_OPTIONS = [
  { value: "", label: "全部" },
  { value: "pending", label: "待处理" },
  { value: "running", label: "进行中" },
  { value: "succeeded", label: "已成功" },
  { value: "failed", label: "已失败" },
  { value: "cancelled", label: "已取消" },
];

const STATUS_TAG_COLOR: Record<string, string> = {
  pending: "default",
  running: "processing",
  succeeded: "success",
  failed: "error",
  cancelled: "warning",
};

const STATUS_LABEL: Record<string, string> = {
  pending: "待处理",
  running: "进行中",
  succeeded: "已成功",
  failed: "已失败",
  cancelled: "已取消",
};

function statusText(s: string): string {
  return STATUS_LABEL[s] || s;
}

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

  async function act(id: number, kind: "cancel" | "requeue") {
    const label = kind === "cancel" ? "取消" : "重跑";
    if (!window.confirm(`确认${label}任务 #${id}？`)) return;
    setBusyId(id);
    try {
      const res =
        kind === "cancel" ? await api.cancelJob(id) : await api.requeueJob(id);
      onToast(res.message || `${label}成功`);
      onRefresh();
    } catch (e) {
      onToast((e as Error).message || `${label}失败`);
    } finally {
      setBusyId(null);
    }
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
      width: 110,
      render: (_: unknown, r: JobRow) =>
        r.esubmit_screenshot_path ? (
          <Image
            src={api.jobScreenshotUrl(r.id, "esubmit")}
            width={80}
            height={50}
            style={{ objectFit: "cover", cursor: "pointer" }}
            onClick={(e) => e.stopPropagation()}
            preview={{ mask: false }}
          />
        ) : (
          "-"
        ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => (
        <Tag color={STATUS_TAG_COLOR[s] || "default"}>{statusText(s)}</Tag>
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
      width: 100,
      fixed: "right" as const,
      render: (_: unknown, r: JobRow) => (
        <Space
          onClick={(e) => e.stopPropagation()}
        >
          {r.status === "pending" ? (
            <Button
              size="small"
              danger
              disabled={busyId === r.id}
              onClick={() => act(r.id, "cancel")}
            >
              取消
            </Button>
          ) : null}
          {r.status === "failed" || r.status === "cancelled" ? (
            <Button
              size="small"
              type="primary"
              disabled={busyId === r.id}
              onClick={() => act(r.id, "requeue")}
            >
              重跑
            </Button>
          ) : null}
          {r.status !== "pending" &&
          r.status !== "failed" &&
          r.status !== "cancelled"
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
        </Space>
      </Card>

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
