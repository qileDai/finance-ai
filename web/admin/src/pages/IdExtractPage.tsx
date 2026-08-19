import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { ImageModal } from "../components/ImageModal";

type Props = {
  onToast: (msg: string) => void;
};

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

async function readClipboardImage(): Promise<File | null> {
  try {
    if (!navigator.clipboard?.read) return null;
    const items = await navigator.clipboard.read();
    for (const item of items) {
      const type = item.types.find((t) => t.startsWith("image/"));
      if (!type) continue;
      const blob = await item.getType(type);
      const ext = (type.split("/")[1] || "png").split("+")[0];
      return new File([blob], `clipboard.${ext}`, { type });
    }
  } catch {
    return null;
  }
  return null;
}

async function blobSig(blob: Blob): Promise<string> {
  const buf = await blob.slice(0, 2048).arrayBuffer();
  const bytes = new Uint8Array(buf);
  let h = blob.size >>> 0;
  for (const b of bytes) h = (Math.imul(h, 33) + b) >>> 0;
  return `${blob.size}:${blob.type}:${h}`;
}

function launchWindowsSnip() {
  try {
    window.location.href = "ms-screenclip:";
  } catch {
    /* user can still press Win+Shift+S */
  }
}

export function IdExtractPage({ onToast }: Props) {
  const [file, setFile] = useState<File | undefined>();
  const [previewUrl, setPreviewUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [display, setDisplay] = useState<DisplayRow[]>([]);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [hints, setHints] = useState<string[]>([]);
  const [meta, setMeta] = useState<string>("");
  const [modalOpen, setModalOpen] = useState(false);
  const [snipWaiting, setSnipWaiting] = useState(false);
  const loadingRef = useRef(false);
  const previewUrlRef = useRef("");
  const snipWaitingRef = useRef(false);
  const beforeClipSigRef = useRef("");
  const onToastRef = useRef(onToast);
  onToastRef.current = onToast;

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
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = "";
      setPreviewUrl("");
    }
    if (f && f.type.startsWith("image/")) {
      const url = URL.createObjectURL(f);
      previewUrlRef.current = url;
      setPreviewUrl(url);
    }
  }

  const pickRef = useRef(onPickFile);
  pickRef.current = onPickFile;

  function applyImageOnly(f: File, toast: string) {
    pickRef.current(f);
    onToastRef.current(toast);
  }

  async function recognizeFile(f: File) {
    if (!f.type.startsWith("image/")) {
      onToastRef.current("仅支持图片（JPG/PNG/WEBP）");
      return;
    }
    loadingRef.current = true;
    setLoading(true);
    try {
      const dataUrl = await readFileAsDataUrl(f);
      const res = await api.idExtract({
        data_url: dataUrl,
        filename: f.name,
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
        [res.vision?.type_label || res.vision?.id_type || "已识别", conf]
          .filter(Boolean)
          .join(" · ")
      );
      onToastRef.current(
        rows.length ? `识别成功：${rows.length} 项` : "识别完成，未得到字段"
      );
    } catch (e) {
      setDisplay([]);
      setFields({});
      onToastRef.current((e as Error).message || "识别失败");
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  async function onRecognize() {
    if (!file) {
      onToastRef.current("请先上传或粘贴证件图片");
      return;
    }
    await recognizeFile(file);
  }

  function stopSnipWait() {
    snipWaitingRef.current = false;
    setSnipWaiting(false);
  }

  async function onScreenshot() {
    if (loadingRef.current || snipWaitingRef.current) return;
    const existing = await readClipboardImage();
    beforeClipSigRef.current = existing ? await blobSig(existing) : "";
    snipWaitingRef.current = true;
    setSnipWaiting(true);
    launchWindowsSnip();
  }

  useEffect(() => {
    if (!snipWaiting) return;
    let stop = false;

    const takeIfNew = async () => {
      if (stop || !snipWaitingRef.current) return;
      const clip = await readClipboardImage();
      if (!clip) return;
      const sig = await blobSig(clip);
      if (sig === beforeClipSigRef.current) return;
      stopSnipWait();
      applyImageOnly(clip, "已截取图片，请点「开始识别」");
    };

    const id = window.setInterval(() => void takeIfNew(), 400);
    const onFocus = () => void takeIfNew();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        stopSnipWait();
      }
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    window.addEventListener("keydown", onKey);
    return () => {
      stop = true;
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
      window.removeEventListener("keydown", onKey);
    };
  }, [snipWaiting]);

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      if (loadingRef.current) return;
      const items = e.clipboardData?.items;
      if (!items?.length) return;
      const imageItem = Array.from(items).find((i) => i.type.startsWith("image/"));
      if (!imageItem) return;
      const blob = imageItem.getAsFile();
      if (!blob) return;
      e.preventDefault();
      if (snipWaitingRef.current) stopSnipWait();
      const name = blob.name && blob.name !== "image.png" ? blob.name : `paste-${Date.now()}.png`;
      const f = new File([blob], name, { type: blob.type || "image/png" });
      applyImageOnly(f, "已粘贴图片，请点「开始识别」");
    };
    window.addEventListener("paste", onPaste);
    return () => {
      window.removeEventListener("paste", onPaste);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    };
  }, []);

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
            上传中国身份证、香港身份证、护照或台湾身份证图片。点「截图」后直接框选屏幕上的证件；也可 Ctrl+V
            粘贴。图片出现在左侧后，再点「开始识别」。
          </small>

          <div className="id-extract-form">
            <div className="reg-field">
              <span>
                上传图片
                <em>*</em>
              </span>
              <input
                key={file ? file.name : "empty"}
                type="file"
                accept="image/jpeg,image/png,image/webp,image/jpg"
                disabled={loading}
                onChange={(e) => onPickFile(e.target.files?.[0])}
              />
              {file ? <small className="muted">{file.name}</small> : null}
              {previewUrl ? (
                <div className="id-preview-wrap">
                  <img
                    src={previewUrl}
                    alt="证件预览"
                    className="id-preview id-preview-zoom"
                    title="点击放大"
                    onClick={() => setModalOpen(true)}
                  />
                  <button
                    type="button"
                    className="id-preview-del"
                    title="删除图片"
                    disabled={loading}
                    onClick={() => onPickFile(undefined)}
                  >
                    ✕
                  </button>
                </div>
              ) : null}
            </div>

            <div className="reg-actions" style={{ marginTop: 0 }}>
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
                disabled={loading || snipWaiting}
                onClick={onScreenshot}
              >
                截图
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
            <div className="empty-box">上传 / 粘贴 / 截图后，点「开始识别」</div>
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
      <ImageModal
        src={previewUrl}
        alt="证件图片"
        open={modalOpen && !!previewUrl}
        onClose={() => setModalOpen(false)}
      />
    </div>
  );
}
