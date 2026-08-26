import { useEffect, useState } from "react";
import { api } from "../api";
import { Button, Card, Divider, Input, Space } from "antd";

type Props = { onToast: (msg: string) => void };

const emptyForm = {
  flat_floor: "",
  building: "",
  street: "",
  district: "",
  secretary_br_no: "",
  secretary_license_no: "",
  secretary_company_no: "",
};

export function DefaultOfficePage({ onToast }: Props) {
  const [form, setForm] = useState({ ...emptyForm });
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
          secretary_br_no: d.secretary_br_no || "",
          secretary_license_no: d.secretary_license_no || "",
          secretary_company_no: d.secretary_company_no || "",
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
    <Card title="注册配置" loading={loading} style={{ maxWidth: 640 }}>
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <Divider orientation="left" plain>
          办事处地址
        </Divider>
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

        <Divider orientation="left" plain>
          公司秘书（法人团体）
        </Divider>
        <Input
          addonBefore="商业登记证"
          value={form.secretary_br_no}
          onChange={(e) =>
            setForm({ ...form, secretary_br_no: e.target.value })
          }
          placeholder="商業登記號碼，如 78090873"
        />
        <Input
          addonBefore="牌照号"
          value={form.secretary_license_no}
          onChange={(e) =>
            setForm({ ...form, secretary_license_no: e.target.value })
          }
          placeholder="如 TC010510"
        />
        <Input
          addonBefore="公司号码"
          value={form.secretary_company_no}
          onChange={(e) =>
            setForm({ ...form, secretary_company_no: e.target.value })
          }
          placeholder="如 0852-52667282"
        />

        <Button type="primary" loading={saving} onClick={onSave}>
          保存
        </Button>
      </Space>
    </Card>
  );
}
