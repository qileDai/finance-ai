import { useEffect, useState } from "react";
import { api, type EmailAccount } from "../api";
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
import { useMessageApi } from "../useMessageApi";

const IMAP_PRESETS = [
  { label: "163 邮箱 (imap.163.com:993)", value: "imap.163.com|993" },
  { label: "QQ 邮箱 (imap.qq.com:993)", value: "imap.qq.com|993" },
  { label: "Gmail (imap.gmail.com:993)", value: "imap.gmail.com|993" },
  { label: "Outlook (outlook.office365.com:993)", value: "outlook.office365.com|993" },
  { label: "阿里企业邮 (imap.qiye.aliyun.com:993)", value: "imap.qiye.aliyun.com|993" },
  { label: "自定义", value: "" },
];

type FormState = {
  email_address: string;
  imap_host: string;
  imap_port: number;
  username: string;
  password: string;
  label: string;
  enabled: boolean;
};

const EMPTY: FormState = {
  email_address: "",
  imap_host: "imap.163.com",
  imap_port: 993,
  username: "",
  password: "",
  label: "",
  enabled: true,
};

export function EmailConfigPage() {
  const message = useMessageApi();
  const [items, setItems] = useState<EmailAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [probeUser, setProbeUser] = useState("");
  const [probeEmail, setProbeEmail] = useState("");
  const [probePassword, setProbePassword] = useState("");
  const [probing, setProbing] = useState(false);

  function load() {
    setLoading(true);
    api.emailAccounts
      .list()
      .then((d) => setItems(d.items || []))
      .catch((e: Error) => message.error(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  function openAdd() {
    setEditingId(null);
    setForm(EMPTY);
    setModalOpen(true);
  }

  function openEdit(r: EmailAccount) {
    setEditingId(r.id);
    setForm({
      email_address: r.email_address,
      imap_host: r.imap_host,
      imap_port: r.imap_port,
      username: r.username,
      password: r.password,
      label: r.label || "",
      enabled: Boolean(r.enabled),
    });
    setModalOpen(true);
  }

  async function save() {
    if (!form.email_address || !form.imap_host || !form.username) {
      message.warning("邮箱地址、IMAP主机、账号必填");
      return;
    }
    if (!editingId && !form.password) {
      message.warning("新增时密码/授权码必填");
      return;
    }
    setSaving(true);
    try {
      await api.emailAccounts.upsert({
        ...(editingId ? { id: editingId } : {}),
        email_address: form.email_address,
        imap_host: form.imap_host,
        imap_port: form.imap_port,
        username: form.username,
        password: form.password,
        label: form.label,
        enabled: form.enabled,
      });
      message.success("保存成功");
      setModalOpen(false);
      load();
    } catch (e: unknown) {
      message.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    try {
      await api.emailAccounts.remove(id);
      message.success("删除成功");
      load();
    } catch (e: unknown) {
      message.error((e as Error).message);
    }
  }

  async function test(id: number) {
    setTestingId(id);
    try {
      const res = await api.emailAccounts.test(id);
      message.success(res.message || "IMAP 登录成功，已打开收件箱");
    } catch (e: unknown) {
      message.error((e as Error).message);
    } finally {
      setTestingId(null);
    }
  }

  async function runProbe() {
    if (!probeUser.trim() || !probeEmail.trim()) {
      message.warning("账号和邮箱必填");
      return;
    }
    setProbing(true);
    try {
      const res = await api.emailAccounts.activateProbe({
        username: probeUser.trim(),
        email: probeEmail.trim(),
        password: probePassword,
      });
      const src =
        res.password_source === "manual"
          ? "手填密码"
          : res.password_source === "job"
            ? "任务密码"
            : "无密码";
      if (res.activated) {
        message.success(res.detail || res.message || "激活成功");
      } else if (res.found) {
        message.warning(`${res.detail || "已找到信但激活未成功"}（${src}）`);
      } else {
        message.info(res.detail || res.message || "暂无激活邮件");
      }
    } catch (e: unknown) {
      message.error((e as Error).message);
    } finally {
      setProbing(false);
    }
  }

  async function toggleEnabled(r: EmailAccount, enabled: boolean) {
    setTogglingId(r.id);
    try {
      await api.emailAccounts.upsert({
        id: r.id,
        email_address: r.email_address,
        imap_host: r.imap_host,
        imap_port: r.imap_port,
        username: r.username,
        password: r.password || "",
        label: r.label || "",
        enabled,
      });
      message.success(enabled ? "已启用" : "已禁用");
      load();
    } catch (e: unknown) {
      message.error((e as Error).message);
    } finally {
      setTogglingId(null);
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", width: 60 },
    {
      title: "邮箱地址",
      dataIndex: "email_address",
      width: 220,
    },
    { title: "IMAP 主机", dataIndex: "imap_host", width: 180 },
    { title: "端口", dataIndex: "imap_port", width: 80 },
    { title: "账号", dataIndex: "username", width: 180 },
    { title: "备注", dataIndex: "label", width: 120 },
    {
      title: "状态",
      dataIndex: "enabled",
      width: 110,
      render: (v: number, r: EmailAccount) => (
        <Space>
          <Switch
            size="small"
            checked={Boolean(v)}
            loading={togglingId === r.id}
            onChange={(checked) => toggleEnabled(r, checked)}
          />
          {v ? <Tag color="green">启用</Tag> : <Tag>禁用</Tag>}
        </Space>
      ),
    },
    {
      title: "操作",
      width: 220,
      render: (_: unknown, r: EmailAccount) => (
        <Space>
          <Button
            size="small"
            loading={testingId === r.id}
            onClick={() => test(r.id)}
          >
            测试
          </Button>
          <Button size="small" onClick={() => openEdit(r)}>
            编辑
          </Button>
          <Popconfirm
            title="确定删除?"
            onConfirm={() => remove(r.id)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Card title="激活探测" style={{ marginBottom: 16 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="只认标题「電子服務 - 用戶登記及啟動」"
          description="打开信里的 s06 启动帐户链接，填用户名/密码后点确认。不改注册任务状态。密码选填：填了用手填的，没填只读查任务里的密码。"
        />
        <Space wrap>
          <Input
            placeholder="ICRIS 账号"
            style={{ width: 180 }}
            value={probeUser}
            onChange={(e) => setProbeUser(e.target.value)}
          />
          <Select
            placeholder="邮箱账号"
            style={{ width: 260 }}
            allowClear
            value={probeEmail || undefined}
            options={items
              .filter((r) => r.enabled)
              .map((r) => ({
                value: r.email_address,
                label: r.label
                  ? `${r.email_address}（${r.label}）`
                  : r.email_address,
              }))}
            onChange={(v) => setProbeEmail(v || "")}
          />
          <Input.Password
            placeholder="密码（选填）"
            style={{ width: 180 }}
            value={probePassword}
            onChange={(e) => setProbePassword(e.target.value)}
          />
          <Button type="primary" loading={probing} onClick={runProbe}>
            拉信激活
          </Button>
        </Space>
      </Card>
    <Card
      title="邮箱账号配置"
      extra={
        <Button type="primary" onClick={openAdd}>
          新增
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="按快速注册填入的电邮收 ICRIS 确认信并激活账号"
        description="每个会用到的注册电邮配一条 IMAP。163/QQ 请填授权码，不是登录密码。激活后用该 ICRIS 账号登录填 NNC1。"
      />
      <Table
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
        scroll={{ x: 1100 }}
      />
      <Modal
        open={modalOpen}
        title={editingId ? "编辑邮箱" : "新增邮箱"}
        onCancel={() => setModalOpen(false)}
        onOk={save}
        confirmLoading={saving}
        width={500}
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Input
            placeholder="邮箱地址（须与快速注册/S03 填入 ICRIS 的电邮一致）"
            value={form.email_address}
            onChange={(e) => {
              const email = e.target.value;
              setForm((prev) => ({
                ...prev,
                email_address: email,
                username:
                  !prev.username || prev.username === prev.email_address
                    ? email
                    : prev.username,
              }));
            }}
          />
          <div>
            <div style={{ marginBottom: 4, color: "#666", fontSize: 13 }}>
              IMAP 主机（选择邮箱服务商）
            </div>
            <Select
              style={{ width: "100%" }}
              value={
                IMAP_PRESETS.find(
                  (p) =>
                    p.value &&
                    p.value.startsWith(form.imap_host + "|") &&
                    p.value.endsWith("|" + form.imap_port),
                )?.value ?? (form.imap_host ? "" : "imap.163.com|993")
              }
              options={IMAP_PRESETS}
              onChange={(val) => {
                if (val && val.includes("|")) {
                  const [host, port] = val.split("|");
                  setForm({
                    ...form,
                    imap_host: host,
                    imap_port: Number(port) || 993,
                  });
                } else {
                  setForm({ ...form, imap_host: "" });
                }
              }}
            />
            {!IMAP_PRESETS.some(
              (p) =>
                p.value &&
                p.value.startsWith(form.imap_host + "|") &&
                p.value.endsWith("|" + form.imap_port),
            ) && (
              <Space style={{ marginTop: 8, width: "100%" }}>
                <Input
                  placeholder="自定义 IMAP 主机，如 imap.example.com"
                  style={{ width: 320 }}
                  value={form.imap_host}
                  onChange={(e) =>
                    setForm({ ...form, imap_host: e.target.value })
                  }
                />
                <Input
                  placeholder="端口"
                  style={{ width: 100 }}
                  type="number"
                  value={form.imap_port}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      imap_port: Number(e.target.value) || 993,
                    })
                  }
                />
              </Space>
            )}
          </div>
          <Input
            placeholder="IMAP 账号（通常与邮箱地址相同）"
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
          <Input.Password
            placeholder={
              editingId
                ? "IMAP 授权码（留空则保持原密码；163/QQ 用授权码）"
                : "IMAP 密码/授权码（163/QQ 用授权码，非登录密码）"
            }
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <Input
            placeholder="备注（可选，如 客户A注册邮箱）"
            value={form.label}
            onChange={(e) => setForm({ ...form, label: e.target.value })}
          />
          <Space>
            <span style={{ color: "#666" }}>启用</span>
            <Switch
              checked={form.enabled}
              onChange={(checked) => setForm({ ...form, enabled: checked })}
            />
          </Space>
        </Space>
      </Modal>
    </Card>
    </>
  );
}
