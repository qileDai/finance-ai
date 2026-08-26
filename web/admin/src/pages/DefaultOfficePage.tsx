import { useEffect, useState } from "react";
import { api } from "../api";
import { Button, Card, Input, Space, message } from "antd";

type Props = { onToast: (msg: string) => void };

export function DefaultOfficePage({ onToast }: Props) {
  const [form, setForm] = useState({
    flat_floor: "",
    building: "",
    street: "",
    district: "",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.defaultOffice
      .get()
      .then((d) =>
        setForm({
          flat_floor: d.flat_floor || "",
          building: d.building || "",
          street: d.street || "",
          district: d.district || "",
        }),
      )
      .catch(() => onToast("加载失败"))
      .finally(() => setLoading(false));
  }, []);

  function onSave() {
    setSaving(true);
    api.defaultOffice
      .update(form)
      .then(() => onToast("已保存"))
      .catch((e: Error) => onToast(`保存失败: ${e.message}`))
      .finally(() => setSaving(false));
  }

  return (
    <Card title="注册办事处默认地址" loading={loading} style={{ maxWidth: 600 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Input
          addonBefore="室/楼/座等"
          value={form.flat_floor}
          onChange={(e) => setForm({ ...form, flat_floor: e.target.value })}
        />
        <Input
          addonBefore="大厦"
          value={form.building}
          onChange={(e) => setForm({ ...form, building: e.target.value })}
        />
        <Input
          addonBefore="街道/屋苑/地段/村等"
          value={form.street}
          onChange={(e) => setForm({ ...form, street: e.target.value })}
        />
        <Input
          addonBefore="区"
          value={form.district}
          onChange={(e) => setForm({ ...form, district: e.target.value })}
        />
        <Button type="primary" loading={saving} onClick={onSave}>
          保存
        </Button>
      </Space>
    </Card>
  );
}
