import { useEffect, useState } from "react";
import { api, type EmailAccount } from "../api";
import { Button, Card, Input, Modal, Popconfirm, Select, Space, Table, Tag, message } from "antd";

type Props = { onToast: (msg: string) => void };

const IMAP_PRESETS = [
  { label: "QQ 邮箱 (imap.qq.com:993)", value: "imap.qq.com|993" },
  { label: "163 邮箱 (imap.163.com:993)", value: "imap.163.com|993" },
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
};

const EMPTY: FormState = {
  email_address: "",
  imap_host: "imap.qq.com",
  imap_port: 993,
  username: "",
  password: "",
  label: "",
};

export function EmailConfigPage({ onToast }: Props) {
  const [items, setItems] = useState<EmailAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);

  function load() {
    setLoading(true);
    api.emailAccounts
      .list()
      .then((d) => setItems(d.items || []))
      .catch((e: Error) => onToast(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  function openAdd() {
    setForm(EMPTY);
    setModalOpen(true);
  }

  function openEdit(r: EmailAccount) {
    setForm({
      email_address: r.email_address,
      imap_host: r.imap_host,
      imap_port: r.imap_port,
      username: r.username,
      password: r.password,
      label: r.label || "",
    });
    setModalOpen(true);
  }

  async function save() {
    if (!form.email_address || !form.imap_host || !form.username || !form.password) {
      onToast("邮箱地址、IMAP主机、账号、密码必填");
      return;
    }
    setSaving(true);
    try {
      await api.emailAccounts.upsert(form);
      onToast("保存成功");
      setModalOpen(false);
      load();
    } catch (e: unknown) {
      onToast((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    try {
      await api.emailAccounts.remove(id);
      onToast("删除成功");
      load();
    } catch (e: unknown) {
      onToast((e as Error).message);
    }
  }

  async function test(id: number) {
    setTestingId(id);
    try {
      await api.emailAccounts.test(id);
      onToast("连接成功");
    } catch (e: unknown) {
      onToast((e as Error).message);
    } finally {
      setTestingId(null);
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
      width: 80,
      render: (v: number) =>
        v ? <Tag color="green">启用</Tag> : <Tag>禁用</Tag>,
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
    <Card
      title="邮箱账号配置"
      extra={
        <Button type="primary" onClick={openAdd}>
          新增
        </Button>
      }
    >
      <Table
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
        scroll={{ x: 1000 }}
      />
      <Modal
        open={modalOpen}
        title={form.email_address ? "编辑邮箱" : "新增邮箱"}
        onCancel={() => setModalOpen(false)}
        onOk={save}
        confirmLoading={saving}
        width={500}
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Input
            placeholder="邮箱地址（注册时填入 ICRIS 的邮箱，如 abc@qq.com）"
            value={form.email_address}
            onChange={(e) => setForm({ ...form, email_address: e.target.value })}
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
                )?.value ?? (form.imap_host ? "" : "imap.qq.com|993")
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
            placeholder="IMAP 账号（通常与邮箱地址相同，如 abc@qq.com）"
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
          <Input.Password
            placeholder="IMAP 密码/授权码（QQ 邮箱用授权码，非登录密码）"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <Input
            placeholder="备注（可选，如 客户A注册邮箱）"
            value={form.label}
            onChange={(e) => setForm({ ...form, label: e.target.value })}
          />
        </Space>
      </Modal>
    </Card>
  );
}
