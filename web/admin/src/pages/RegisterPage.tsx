import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type RunnerFile, type RunnerStatus } from "../api";
import { formatDateTime } from "../format";
import { asLogText, logLineClass, normalizeLogLines } from "../jobLog";
import { statusBadge } from "../components/ui";

type Props = {
  onToast: (msg: string) => void;
};

type TextField = {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
};

const TEXT_FIELDS: TextField[] = [
  { key: "company_name_cn", label: "公司中文名" },
  { key: "company_name_en", label: "公司英文名", required: true },
  { key: "registered_capital", label: "注册资本", placeholder: "1万港币" },
  { key: "business_desc", label: "经营范围" },
  { key: "registered_office_cn", label: "注册地址（中文）" },
  { key: "registered_office_en", label: "注册地址（英文）" },
  { key: "director_name", label: "董事兼股东姓名", required: true },
  { key: "id_number", label: "身份证号码", required: true },
  {
    key: "contact_email",
    label: "联络邮箱",
    required: true,
    placeholder: "默认 MATERIALS_DEFAULT_CONTACT_EMAIL",
  },
  { key: "director_address_cn", label: "住址（中文）" },
  { key: "director_address_en", label: "住址（英文）" },
];

const ID_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "PRC_ID", label: "内地身份证" },
  { value: "HKID", label: "香港身份证" },
  { value: "PASSPORT", label: "护照" },
];

function primaryIdFileKey(idType: string): string {
  return idType === "PASSPORT" ? "passport" : "id_card_front";
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

/** 判断一行是否像英文地址（含较多 ASCII 字母且不以中文开头） */
function looksLikeEnglishAddress(s: string): boolean {
  const asciiLetters = (s.match(/[A-Za-z]/g) || []).length;
  return asciiLetters >= 5 && !/^[\u4e00-\u9fff]/.test(s);
}

/**
 * 检测是否香港地址：含 香港/Hong Kong/Kowloon/九龍/新界 → True。
 * 与后端 src.browser.icris_registration.detect_hk_address 同款逻辑。
 */
function detectHkAddress(cn: string, en: string): boolean {
  const addr = `${en || ""} ${cn || ""}`.toLowerCase();
  const keywords = [
    "hong kong",
    "kowloon",
    "new territories",
    "香港",
    "九龍",
    "九龙",
    "新界",
  ];
  return keywords.some((k) => addr.includes(k));
}

/**
 * 把整段注册信息解析为字段映射。
 * 支持「中文名：」「英文名：」「注册资本：」「经营范围：」「注册地址：」
 * 「董事：」「身份证号码：」「住址中文：」「住址英文：」等关键字。
 * 「注册地址：」后紧跟的、不含关键字的英文行视为英文注册地址。
 */
function parseRegistrationText(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  if (!raw) return result;

  const text = raw.replace(/\r\n?/g, "\n");
  const lines = text.split("\n");

  // 行首（去掉 "1、" "2." "(3)" 等编号后）匹配关键字
  const rules: { field: string; pattern: RegExp }[] = [
    { field: "director_address_cn", pattern: /^(住址中文|住址（中文）|中文住址)/ },
    { field: "director_address_en", pattern: /^(住址英文|住址（英文）|英文住址)/ },
    { field: "company_name_cn", pattern: /^(公司中文名|公司中文名称|中文名)/ },
    { field: "company_name_en", pattern: /^(公司英文名|公司英文名称|英文名)/ },
    { field: "registered_capital", pattern: /^注册资本/ },
    { field: "business_desc", pattern: /^(经营范围|业务范围)/ },
    { field: "director_name", pattern: /^(董事|股东)/ },
    { field: "id_number", pattern: /^(身份证号?码?|证件号)/ },
    { field: "contact_email", pattern: /^(联络邮箱|邮箱|电邮|电子邮件)/ },
  ];

  // 已知关键字集合：用于判断「注册地址」后下一行是否为新字段
  const knownKeyRe =
    /^(住址中文|住址英文|住址（中文|住址（英文|中文住址|英文住址|公司中文名|公司中文名称|中文名|公司英文名|公司英文名称|英文名|注册资本|经营范围|业务范围|董事|股东|身份证号?码?|证件号|注册地址|公司名称|联络邮箱|邮箱|电邮)/;

  function stripLeadingNumber(s: string): string {
    return s.replace(/^\s*\d+\s*[、.）)]\s*/, "").trim();
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const stripped = stripLeadingNumber(line);

    // 注册地址：本行可能是中文地址，下一行可能是英文地址
    if (/^注册地址/.test(stripped)) {
      const after = stripped.replace(/^注册地址\s*[:：]\s*/, "").trim();
      if (after) result.registered_office_cn = after;
      if (i + 1 < lines.length) {
        const next = lines[i + 1].trim();
        if (
          next &&
          !/^\s*\d+\s*[、.）)]/.test(next) &&
          !knownKeyRe.test(stripLeadingNumber(next)) &&
          looksLikeEnglishAddress(next)
        ) {
          result.registered_office_en = next;
          i++;
        }
      }
      continue;
    }

    for (const r of rules) {
      if (r.pattern.test(stripped)) {
        const idx = stripped.search(/[:：]/);
        if (idx >= 0) {
          const val = stripped.slice(idx + 1).trim();
          if (val) result[r.field] = val;
        }
        break;
      }
    }
  }

  return result;
}

export function RegisterPage({ onToast }: Props) {
  const [fields, setFields] = useState<Record<string, string>>({
    registered_capital: "1万港币",
  });
  const [idType, setIdType] = useState("PRC_ID");
  const [idFile, setIdFile] = useState<File | undefined>();
  const [dryRun, setDryRun] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [runnerStatus, setRunnerStatus] = useState<RunnerStatus | null>(null);
  const [polling, setPolling] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  // 预填默认邮箱 + 恢复运行中任务状态
  useEffect(() => {
    api.registerRunner
      .defaults()
      .then((d) => {
        const email = (d.contact_email || "").trim();
        if (email) {
          setFields((p) =>
            p.contact_email ? p : { ...p, contact_email: email }
          );
        }
      })
      .catch(() => {});
    api.registerRunner
      .status()
      .then((d) => {
        setRunnerStatus(d);
        if (d.status === "running" || d.status === "pending") setPolling(true);
      })
      .catch(() => {});
  }, []);

  // 轮询
  useEffect(() => {
    if (!polling) return;
    let alive = true;
    const tick = async () => {
      try {
        const d = await api.registerRunner.status();
        if (!alive) return;
        setRunnerStatus(d);
        if (d.status !== "running" && d.status !== "pending") setPolling(false);
      } catch {
        /* ignore */
      }
    };
    const id = window.setInterval(tick, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [polling]);

  // messages 滚到底
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [runnerStatus?.messages]);

  const isRunning =
    runnerStatus?.status === "running" || runnerStatus?.status === "pending";
  const statusLogs = normalizeLogLines(runnerStatus?.messages);

  function setField(key: string, val: string) {
    setFields((p) => ({ ...p, [key]: val }));
  }

  function onParse() {
    const parsed = parseRegistrationText(pasteText);
    const keys = Object.keys(parsed);
    if (!keys.length) {
      onToast("未识别到任何字段，请检查格式");
      return;
    }
    setFields((p) => ({ ...p, ...parsed }));
    onToast(`已解析 ${keys.length} 个字段`);
  }

  function validate(): string | null {
    for (const f of TEXT_FIELDS) {
      if (f.required && !(fields[f.key] || "").trim())
        return `${f.label}必填`;
    }
    const email = (fields.contact_email || "").trim();
    if (email && !email.includes("@")) return "联络邮箱格式无效";
    const hasAddr = ["director_address_cn", "director_address_en", "registered_office_cn", "registered_office_en"].some(
      (k) => (fields[k] || "").trim()
    );
    if (!hasAddr) return "至少填写一个地址（住址或注册地址）";
    if (!idFile) return "请上传证件文件（PDF 或图片）";
    return null;
  }

  async function onSubmit() {
    const err = validate();
    if (err) {
      onToast(err);
      return;
    }
    const files: Record<string, RunnerFile> = {};
    if (idFile) {
      const dataUrl = await readFileAsDataUrl(idFile);
      const key = primaryIdFileKey(idType);
      files[key] = { name: idFile.name, data_url: dataUrl };
    }
    setSubmitting(true);
    try {
      const payload = { ...fields, id_type: idType };
      const res = await api.registerRunner.submit(payload, files, dryRun);
      onToast(
        res.job_id
          ? `已入队任务 #${res.job_id}：${res.company_name}`
          : `已提交注册：${res.company_name}`
      );
      setRunnerStatus({
        status: "pending",
        company_name: res.company_name,
        case_id: res.case_id,
        job_id: res.job_id ?? null,
        messages: res.job_id
          ? [
              {
                level: "INFO",
                message: `已入队任务 #${res.job_id}，等待 Worker 执行`,
              },
            ]
          : [],
        dry_run: res.dry_run ?? dryRun,
      });
      setPolling(true);
    } catch (e) {
      onToast((e as Error).message || "提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="register-page">
      <section className="reg-card reg-paste-card">
        <h2>快速填充</h2>
        <small className="muted">
          粘贴整段注册信息（含「中文名：」「英文名：」「注册资本：」「经营范围：」「注册地址：」「董事：」「身份证号码：」「住址中文：」「住址英文：」等关键字），点「解析填充」自动写入下方表单。
        </small>
        <textarea
          className="reg-paste-area"
          rows={10}
          placeholder={
            "1、公司名称\n 中文名：撼世全球有限公司\n 英文名：Humsienk Global Limited\n2、注册资本：1万港币\n3、经营范围：新能源產品、電子元器件銷售，電子商務，國際貿易\n4、注册地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O\n 909O 9/F., High Fashion Centre,1-11 Kwai Hei Street, Kwai Chung,New Territories, Hong Kong\n\n董事+股东：姚曉佳\n身份证号码：44051420000318492X\n住址中文：广东省深圳市南山区西丽南路8号110室\n住址英文：Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province"
          }
          value={pasteText}
          onChange={(e) => setPasteText(e.target.value)}
          disabled={isRunning || submitting}
        />
        <div className="reg-paste-actions">
          <button
            type="button"
            className="btn btn-primary"
            disabled={isRunning || submitting || !pasteText.trim()}
            onClick={onParse}
          >
            解析填充
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={isRunning || submitting}
            onClick={() => setPasteText("")}
          >
            清空
          </button>
        </div>
      </section>

      <div className="reg-grid">
        <section className="reg-card">
          <h2>公司资料</h2>
          <div className="reg-form">
            {TEXT_FIELDS.map((f) => (
              <label key={f.key} className="reg-field">
                <span>
                  {f.label}
                  {f.required ? <em>*</em> : null}
                </span>
                <input
                  type="text"
                  value={fields[f.key] || ""}
                  placeholder={f.placeholder}
                  onChange={(e) => setField(f.key, e.target.value)}
                  disabled={isRunning || submitting}
                />
                {f.key === "director_address_en" &&
                (fields.director_address_cn || fields.director_address_en) ? (
                  <small
                    className={
                      detectHkAddress(
                        fields.director_address_cn || "",
                        fields.director_address_en || ""
                      )
                        ? "badge ok"
                        : "badge warn"
                    }
                  >
                    {detectHkAddress(
                      fields.director_address_cn || "",
                      fields.director_address_en || ""
                    )
                      ? "香港地址（本地地址）"
                      : "非香港地址 · 国家/地区=中国"}
                  </small>
                ) : null}
              </label>
            ))}
            <label className="reg-field">
              <span>身份证明类型</span>
              <select
                value={idType}
                onChange={(e) => {
                  setIdType(e.target.value);
                  setIdFile(undefined);
                }}
                disabled={isRunning || submitting}
              >
                {ID_TYPE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <h2>证件文件</h2>
          <div className="reg-form">
            <label className="reg-field">
              <span>
                证件文件（PDF/图片）
                <em>*</em>
              </span>
              <input
                type="file"
                accept="image/*,application/pdf"
                disabled={isRunning || submitting}
                onChange={(e) => setIdFile(e.target.files?.[0])}
              />
              {idFile ? (
                <small className="muted">{idFile.name}</small>
              ) : (
                <small className="muted">
                  快速注册只需 1 个文件；企微客服仍按完整正反面/手持收集
                </small>
              )}
            </label>
          </div>

          <div className="reg-actions">
            <label className="reg-field" style={{ marginBottom: 8 }}>
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={dryRun}
                  disabled={isRunning || submitting}
                  onChange={(e) => setDryRun(e.target.checked)}
                />
                dry_run（仅填表，不最终提交）
              </span>
              <small className="muted">
                {dryRun
                  ? "默认开启：只自动填表，不点 ICRIS 最终提交"
                  : "已关闭：将允许自动提交（请确认材料无误）"}
              </small>
            </label>
            <button
              type="button"
              className="btn btn-primary"
              disabled={isRunning || submitting}
              onClick={onSubmit}
            >
              {submitting ? "提交中…" : isRunning ? "注册进行中" : "跑注册"}
            </button>
          </div>
        </section>

        <section className="reg-card">
          <h2>运行状态</h2>
          {!runnerStatus || runnerStatus.status === "idle" ? (
            <div className="empty-box">尚未提交注册任务</div>
          ) : (
            <div className="reg-status">
              <div className="reg-status-head">
                <span className={statusBadge(runnerStatus.status)}>
                  {runnerStatus.status}
                </span>
                <strong>{runnerStatus.company_name || "-"}</strong>
                {runnerStatus.job_id ? (
                  <Link
                    className="mono"
                    to={`/jobs/${runnerStatus.job_id}`}
                  >
                    任务 #{runnerStatus.job_id}
                  </Link>
                ) : null}
                {runnerStatus.case_id ? (
                  <small className="muted mono">{runnerStatus.case_id}</small>
                ) : null}
              </div>
              <div className="reg-meta">
                <span>开始: {formatDateTime(runnerStatus.started_at)}</span>
                <span>完成: {formatDateTime(runnerStatus.finished_at)}</span>
                <span>dry_run: {runnerStatus.dry_run ? "Y" : "N"}</span>
              </div>
              {runnerStatus.error ? (
                <div className="error-box">{runnerStatus.error}</div>
              ) : null}
              <div className="reg-log job-log" ref={logRef} role="log">
                {statusLogs.map((line, i) => {
                  const msg = asLogText(line.message);
                  const level = asLogText(line.level) || "INFO";
                  const time = asLogText(line.time);
                  return (
                    <div
                      key={`${i}-${time}-${msg.slice(0, 24)}`}
                      className={logLineClass(level)}
                    >
                      {time ? (
                        <span className="job-log-time">{time}</span>
                      ) : null}
                      <span className="job-log-level">[{level}]</span>{" "}
                      <span className="job-log-msg">{msg}</span>
                    </div>
                  );
                })}
                {!statusLogs.length ? (
                  <div className="muted">暂无日志…</div>
                ) : null}
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
