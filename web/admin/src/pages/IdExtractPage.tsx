import { useMemo, useState } from "react";
import { api } from "../api";

type Props = {
  onToast: (msg: string) => void;
};

type IdKind = "PRC_ID" | "HKID" | "PASSPORT" | "TW_ID";

const ID_OPTIONS: { value: IdKind; label: string; desc: string }[] = [
  {
    value: "PRC_ID",
    label: "中国身份证",
    desc: "姓名、身份证号码、住址中文 → 自动译住址英文",
  },
  {
    value: "HKID",
    label: "香港身份证",
    desc: "中文名、英文名、香港身份证号码",
  },
  {
    value: "PASSPORT",
    label: "护照",
    desc: "中文名（可空）、英文名、护照号码；台湾护照可再传台湾身份证取住址",
  },
  {
    value: "TW_ID",
    label: "台湾身份证",
    desc: "配合台湾护照：姓名、号码、住址中文 → 译英文",
  },
];

type DisplayRow = { key: string; label: string; value: string };

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

async function copyText(text: string): Promise<boolean> {
  const t = (text || "").trim();
  if (!t) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(t);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = t;
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

export function IdExtractPage({ onToast }: Props) {
  const [idType, setIdType] = useState<IdKind>("PRC_ID");
  const [file, setFile] = useState<File | undefined>();
  const [previewUrl, setPreviewUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [display, setDisplay] = useState<DisplayRow[]>([]);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [hints, setHints] = useState<string[]>([]);
  const [meta, setMeta] = useState<string>("");

  const option = useMemo(
    () => ID_OPTIONS.find((o) => o.value === idType) || ID_OPTIONS[0],
    [idType]
  );

  const copyBlock = useMemo(() => {
    return display
      .map((d) => `${d.label}：${fields[d.key] || d.value || ""}`)
      .filter((line) => !line.endsWith("："))
      .join("\n");
  }, [display, fields]);

  function onPickFile(f: File | undefined) {
    setFile(f);
    setDisplay([]);
    setFields({});
    setHints([]);
    setMeta("");
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      setPreviewUrl("");
    }
    if (f && f.type.startsWith("image/")) {
      setPreviewUrl(URL.createObjectURL(f));
    }
  }

  async function onRecognize() {
    if (!file) {
      onToast("请先上传证件图片");
      return;
    }
    if (!file.type.startsWith("image/")) {
      onToast("仅支持图片（JPG/PNG/WEBP）");
      return;
    }
    setLoading(true);
    try {
      const dataUrl = await readFileAsDataUrl(file);
      const res = await api.idExtract({
        expected_id_type: idType,
        data_url: dataUrl,
        filename: file.name,
      });
      const rows = res.display || [];
      const nextFields = { ...(res.fields || {}) };
      for (const row of rows) {
        if (!(nextFields[row.key] || "").trim() && row.value) {
          nextFields[row.key] = row.value;
        }
      }
      setDisplay(rows);
      setFields(nextFields);
      setHints(res.hints || []);
      const conf =
        typeof res.vision?.confidence === "number"
          ? `置信度 ${(res.vision.confidence * 100).toFixed(0)}%`
          : "";
      setMeta(
        [res.vision?.type_label || idType, conf, res.type_mismatch ? "类型可能不符" : ""]
          .filter(Boolean)
          .join(" · ")
      );
      onToast(
        rows.length ? `识别成功：${rows.length} 项` : "识别完成，未得到字段"
      );
    } catch (e) {
      setDisplay([]);
      setFields({});
      onToast((e as Error).message || "识别失败");
    } finally {
      setLoading(false);
    }
  }

  async function onCopyAll() {
    const ok = await copyText(copyBlock);
    onToast(ok ? "已复制全部识别结果" : "复制失败，请手动全选下方文本框");
  }

  async function onCopyOne(label: string, value: string) {
    const ok = await copyText(value);
    onToast(ok ? `已复制「${label}」` : "复制失败，请手动选择");
  }

  return (
    <div className="register-page">
      <div className="reg-grid">
        <section className="reg-card">
          <h2>证件识别</h2>
          <small className="muted">
            先选择证件类型，再上传图片识别。结果可逐项复制或一键复制全部。
          </small>

          <div className="reg-form" style={{ marginTop: 16 }}>
            <div className="reg-field">
              <span>证件类型</span>
              <div className="id-type-grid">
                {ID_OPTIONS.map((o) => (
                  <button
                    key={o.value}
                    type="button"
                    className={
                      idType === o.value
                        ? "btn btn-primary id-type-btn"
                        : "btn id-type-btn"
                    }
                    disabled={loading}
                    onClick={() => {
                      setIdType(o.value);
                      setDisplay([]);
                      setFields({});
                      setHints([]);
                      setMeta("");
                    }}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
              <small className="muted">{option.desc}</small>
            </div>

            <label className="reg-field">
              <span>
                上传图片
                <em>*</em>
              </span>
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp,image/jpg"
                disabled={loading}
                onChange={(e) => onPickFile(e.target.files?.[0])}
              />
              {file ? <small className="muted">{file.name}</small> : null}
            </label>

            {previewUrl ? (
              <div className="id-preview-wrap">
                <img src={previewUrl} alt="证件预览" className="id-preview" />
              </div>
            ) : null}

            <div className="reg-actions">
              <button
                type="button"
                className="btn btn-primary"
                disabled={loading || !file}
                onClick={onRecognize}
              >
                {loading ? "识别中…" : "开始识别"}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={loading || !copyBlock}
                onClick={onCopyAll}
              >
                复制全部
              </button>
            </div>
          </div>
        </section>

        <section className="reg-card">
          <h2>识别结果</h2>
          {meta ? (
            <div className="muted" style={{ marginBottom: 12 }}>
              {meta}
            </div>
          ) : null}
          {hints.length ? (
            <div className="muted" style={{ marginBottom: 12 }}>
              {hints.map((h) => (
                <div key={h}>· {h}</div>
              ))}
            </div>
          ) : null}
          {!display.length && !loading ? (
            <div className="empty-box">选择类型并上传图片后点「开始识别」</div>
          ) : null}
          {loading ? <div className="muted">正在调用视觉模型…</div> : null}
          {display.length ? (
            <>
              <div className="reg-form">
                {display.map((row) => {
                  const val = fields[row.key] || row.value || "";
                  return (
                    <label key={row.key} className="reg-field">
                      <span className="id-result-label">
                        {row.label}
                        <button
                          type="button"
                          className="btn btn-ghost id-copy-btn"
                          onClick={() => onCopyOne(row.label, val)}
                          disabled={!val}
                        >
                          复制
                        </button>
                      </span>
                      <input
                        type="text"
                        value={val}
                        onChange={(e) =>
                          setFields((p) => ({ ...p, [row.key]: e.target.value }))
                        }
                        onFocus={(e) => e.target.select()}
                      />
                    </label>
                  );
                })}
              </div>
              <label className="reg-field" style={{ marginTop: 12 }}>
                <span>全部结果（可全选复制）</span>
                <textarea
                  className="reg-paste-area"
                  rows={Math.max(4, display.length + 1)}
                  readOnly
                  value={copyBlock}
                  onFocus={(e) => e.target.select()}
                />
              </label>
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}
