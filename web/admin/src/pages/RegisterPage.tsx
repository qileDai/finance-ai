import { Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Modal, Select, Spin } from "antd";
import {
  api,
  ApiError,
  type EmailAccount,
  type RunnerFile,
  type RunnerStatus,
} from "../api";
import { PASSPORT_COUNTRIES, countryLabel } from "../countries";
import { formatDateTime } from "../format";
import { asLogText, logLineClass, normalizeLogLines } from "../jobLog";
import { statusBadge } from "../components/ui";
import { useMessageApi } from "../useMessageApi";

type TextField = {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
};

const TEXT_FIELDS: TextField[] = [
  { key: "company_name_cn", label: "公司中文名" },
  { key: "company_name_en", label: "公司英文名" },
  { key: "registered_capital", label: "注册资本", placeholder: "1万港币" },
  { key: "business_desc", label: "经营范围" },
  { key: "registered_office_cn", label: "注册地址（中文）" },
  { key: "registered_office_en", label: "注册地址（英文）" },
  { key: "director_name", label: "董事兼股东姓名", required: true },
  {
    key: "contact_email",
    label: "联络邮箱",
    required: true,
    placeholder: "从邮箱账号配置选择",
  },
  { key: "director_address_cn", label: "住址（中文）" },
  { key: "director_address_en", label: "住址（英文）" },
];

const OFFICE_FIELDS: TextField[] = [
  { key: "office_flat_floor", label: "室/楼/座" },
  { key: "office_building", label: "大厦" },
  { key: "office_street", label: "街道" },
  { key: "office_district", label: "区" },
];

const ID_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "PRC_ID", label: "内地身份证" },
  { value: "HKID", label: "香港身份证" },
  { value: "PASSPORT", label: "护照" },
];

function accountEnabled(a: EmailAccount): boolean {
  return a.enabled === true || a.enabled === 1;
}

type EmailOption = { value: string; label: string; remark?: string };

function emailSelectOptions(
  accounts: EmailAccount[],
  defaultEmail: string,
): EmailOption[] {
  const seen = new Set<string>();
  const options: EmailOption[] = [];
  const add = (addr: string, tag?: string) => {
    const value = (addr || "").trim();
    if (!value) return;
    const key = value.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    const remark = (tag || "").trim();
    options.push({
      value,
      remark: remark || undefined,
      label: remark ? `${value}  ${remark}` : value,
    });
  };
  for (const a of accounts) {
    if (!accountEnabled(a)) continue;
    add(a.email_address || "", a.label);
  }
  add(defaultEmail);
  return options;
}

/** 联络邮箱入库只取地址；「邮箱  备注」里的备注仅供下拉展示。 */
function contactEmailOnly(raw: string): string {
  const s = (raw || "").trim();
  if (!s) return "";
  const token = s.split(/\s+/)[0] || "";
  return token.includes("@") ? token : s;
}

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
 * 检测是否香港地址：仅看董事个人住址关键字（与后端 english_address_is_hk 一致）。
 */
const HK_EN_RE =
  /\bhong\s*kong\b|\bkowloon\b|new\s*territories|\bhksar\b|hk\s*island|\bN\.?\s*T\.?\b|tin\s*shui\s*wai|yuen\s*long|tuen\s*mun|sha\s*tin|kwun\s*tong|tsuen\s*wan|kwai\s*chung|tai\s*po|fanling|sheung\s*shui|tseung\s*kwan\s*o|sai\s*kung|tung\s*chung|mong\s*kok|tsim\s*sha\s*tsui|sham\s*shui\s*po|wong\s*tai\s*sin|causeway\s*bay|wan\s*chai|\baberdeen\b/i;
const HK_CN_RE = /香港|九[龍龙]|新界/;

function detectHkAddressEn(en: string): boolean {
  return HK_EN_RE.test(en || "");
}

function detectHkAddressCn(cn: string): boolean {
  return HK_CN_RE.test(cn || "");
}

function isLocalHkAddress(fields: Record<string, string>): boolean {
  if (fields.address_is_hk === "1") return true;
  if (fields.address_is_hk === "0") return false;
  return (
    detectHkAddressEn(fields.director_address_en || "") ||
    detectHkAddressCn(fields.director_address_cn || "")
  );
}

/**
 * 把整段注册信息解析为字段映射（API 失败时的弱兜底）。
 * 支持简繁标签；住址按正文判中/英文。
 */
function parseRegistrationText(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  if (!raw) return result;

  const text = raw.replace(/\r\n?/g, "\n");
  const lines = text.split("\n");

  // 行首（去掉 "1、" "2." "(3)" 等编号后）匹配关键字
  const rules: { field: string; pattern: RegExp }[] = [
    { field: "director_address_cn", pattern: /^(住址中文|住址（中文）|中文住址|地址中文|中文地址|地址（中文）)/ },
    { field: "director_address_en", pattern: /^(住址英文|住址（英文）|英文住址|地址英文|英文地址|地址（英文）)/ },
    { field: "company_name_cn", pattern: /^(公司中文名|公司中文名称|中文名)/ },
    { field: "company_name_en", pattern: /^(公司英文名|公司英文名称|英文名)/ },
    { field: "registered_capital", pattern: /^(注册资本|註冊資本)/ },
    { field: "business_desc", pattern: /^(经营范围|經營範圍|业务范围|業務範圍)/ },
    { field: "director_name", pattern: /^董事\s*[+＋、,，&＆]?\s*股东|^股东\s*[+＋、,，&＆]?\s*董事|^董事兼股东|^董事|^股东/ },
    { field: "id_number", pattern: /^(香港身份证号?码?|香港身分證號?碼?|香港身分证号?码?|身份证号?码?|身分證號?碼?|证件号|證件號|护照号|護照號)/ },
    { field: "contact_email", pattern: /^(联络邮箱|聯絡郵箱|邮箱|電郵|电邮|电子邮件|電子郵件)/ },
    { field: "registered_office_cn", pattern: /^(注册办事处|註冊辦事處|建议地址|建議地址|办事处地址|辦事處地址|注册地址|註冊地址)/ },
    { field: "office_flat_floor", pattern: /^室[／/]楼[／/]座[^:：]*[:：]|^室\/楼\/座[^:：]*[:：]|^楼层[^:：]*[:：]/ },
    { field: "office_building", pattern: /^(大厦|大廈|大楼|大樓)[^:：]*[:：]/ },
    { field: "office_street", pattern: /^街道[／/]屋苑[／/]地段[／/]村[^:：]*[:：]|^街道[^:：]*[:：]/ },
    { field: "office_district", pattern: /^区[^:：]*[:：]|^區[^:：]*[:：]/ },
  ];

  // 已知关键字集合：用于判断「注册地址」后下一行是否为新字段
  const knownKeyRe =
    /^(住址中文|住址英文|住址（中文|住址（英文|中文住址|英文住址|地址中文|地址英文|地址（中文|地址（英文|中文地址|英文地址|住址|居住地址|地址|公司中文名|公司中文名称|中文名|公司英文名|公司英文名称|英文名|注册资本|註冊資本|经营范围|經營範圍|业务范围|業務範圍|董事|股东|股東|香港身份证|香港身分證|香港身分证|身份证号?码?|身分證|证件号|證件號|护照号|護照號|注册地址|註冊地址|公司名称|公司名稱|联络邮箱|聯絡郵箱|邮箱|电邮|電郵|注册办事处|註冊辦事處|建议地址|建議地址|办事处地址|辦事處地址|室[／/]楼|大厦|大廈|大楼|大樓|街道|区|區)/;
  const officeLabelRe =
    /^(注册地址|註冊地址|注册办事处|註冊辦事處|建议地址|建議地址|办事处地址|辦事處地址)/;
  const directorAddrLineRe =
    /^(?:住址中文|住址英文|住址（中文）|住址（英文）|中文住址|英文住址|地址中文|地址英文|地址（中文）|地址（英文）|中文地址|英文地址|居住地址|住址|地址)(?:\s*[（(][^）)]+[）)])?\s*[:：]?\s*(.*)$/;

  function stripLeadingNumber(s: string): string {
    return s.replace(/^\s*\d+\s*[、.）)]\s*/, "").trim();
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const stripped = stripLeadingNumber(line);

    // 公司名称：冒号后有值才解析（纯标题行跳过），含中文→中文名，纯英文→英文名
    const companyNameMatch = stripped.match(/^(公司名称|公司名稱)\s*[:：]\s*(\S.*)$/);
    if (companyNameMatch) {
      const afterColon = companyNameMatch[2].trim();
      const nextKeyMatch = afterColon.match(
        /\s+(中文名|英文名|公司中文名|公司英文名|注册资本|註冊資本|经营范围|經營範圍|董事|股东|股東|身份证|身分證|注册地址|註冊地址|联络邮箱|聯絡郵箱|邮箱|住址)/
      );
      const val = nextKeyMatch
        ? afterColon.slice(0, nextKeyMatch.index).trim()
        : afterColon;
      if (val) {
        if (/[\u4e00-\u9fff]/.test(val)) {
          result.company_name_cn = val;
        } else {
          result.company_name_en = val;
        }
      }
      continue;
    }

    if (officeLabelRe.test(stripped)) {
      const after = stripped
        .replace(officeLabelRe, "")
        .replace(/^[:：]\s*/, "")
        .trim();
      if (after) {
        if (looksLikeEnglishAddress(after)) result.registered_office_en = after;
        else result.registered_office_cn = after;
      }
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

    const addrLine = stripped.match(directorAddrLineRe);
    if (addrLine && !officeLabelRe.test(stripped)) {
      const val = (addrLine[1] || "").trim();
      if (val) {
        if (looksLikeEnglishAddress(val)) result.director_address_en = val;
        else result.director_address_cn = val;
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

export function RegisterPage() {
  const messageApi = useMessageApi();
  const [fields, setFields] = useState<Record<string, string>>({
    registered_capital: "1万港币",
  });
  const [idType, setIdType] = useState("PRC_ID");
  const [idTypeUserEdited, setIdTypeUserEdited] = useState(false);
  const [idTypeFromTextLlm, setIdTypeFromTextLlm] = useState(false);
  const [idFile, setIdFile] = useState<File | undefined>();
  const [taiwanIdFile, setTaiwanIdFile] = useState<File | undefined>();
  const [taiwanPassport, setTaiwanPassport] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [runnerStatus, setRunnerStatus] = useState<RunnerStatus | null>(null);
  const [polling, setPolling] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [defaultEmail, setDefaultEmail] = useState("");
  const [emailAccounts, setEmailAccounts] = useState<EmailAccount[]>([]);
  const [parsing, setParsing] = useState(false);
  const [s03Countries, setS03Countries] = useState<string[]>([]);
  const [s03Districts, setS03Districts] = useState<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);

  // 预填默认邮箱 + 默认办事处地址 + 恢复运行中任务状态
  useEffect(() => {
    api.s03Countries()
      .then((d) => {
        const items = (d.items || [])
          .map((x) => (x.value || x.label || "").trim())
          .filter(Boolean);
        if (items.length) setS03Countries(items);
      })
      .catch(() => {});
    api.s03Districts()
      .then((d) => {
        const items = (d.items || [])
          .map((x) => (x.value || x.label || "").trim())
          .filter(Boolean);
        if (items.length) setS03Districts(items);
      })
      .catch(() => {});
    api.registerRunner
      .defaults()
      .then((d) => {
        const email = (d.contact_email || "").trim();
        if (email) {
          setDefaultEmail(email);
          setFields((p) =>
            p.contact_email ? p : { ...p, contact_email: email }
          );
        }
      })
      .catch(() => {});
    api.emailAccounts
      .list()
      .then((d) => setEmailAccounts(d.items || []))
      .catch(() => {});
    api.defaultOffice
      .get()
      .then((d) => {
        setFields((p) => {
          const updates: Record<string, string> = {};
          if (!p.office_flat_floor && d.flat_floor)
            updates.office_flat_floor = d.flat_floor;
          if (!p.office_building && d.building)
            updates.office_building = d.building;
          if (!p.office_street && d.street)
            updates.office_street = d.street;
          if (!p.office_district && d.district)
            updates.office_district = d.district;
          return Object.keys(updates).length > 0 ? { ...p, ...updates } : p;
        });
      })
      .catch(() => {});
    api.registerRunner
      .status()
      .then((d) => {
        if (d && d.status && d.status !== "idle") {
          setRunnerStatus(d);
          if (d.status === "pending" || d.status === "running") {
            setPolling(true);
          }
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const d = await api.registerRunner.status();
        if (cancelled) return;
        setRunnerStatus(d);
        if (d.status !== "pending" && d.status !== "running") {
          setPolling(false);
        }
      } catch {
        /* ignore */
      }
    };
    tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [polling]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [runnerStatus?.messages]);

  const statusLogs = normalizeLogLines(runnerStatus?.messages || []);
  const emailOptions = emailSelectOptions(emailAccounts, defaultEmail);

  function setField(key: string, value: string) {
    setFields((p) => ({ ...p, [key]: value }));
  }

  function resetFormForNext() {
    setPasteText("");
    setFields({
      registered_capital: "1万港币",
      ...(defaultEmail ? { contact_email: defaultEmail } : {}),
    });
    setIdType("PRC_ID");
    setIdTypeUserEdited(false);
    setIdTypeFromTextLlm(false);
    setTaiwanPassport(false);
    setIdFile(undefined);
    setTaiwanIdFile(undefined);
  }

  async function onParse() {
    setParsing(true);
    try {
      let parsed: Record<string, string> = {};
      let taiwan = false;
      try {
        const r = await api.registerRunner.parsePaste({ text: pasteText });
        parsed = { ...(r.fields || {}) };
        taiwan = Boolean(r.taiwan_passport);
        if ((parsed.issuing_country || "").toUpperCase() === "TWN") {
          taiwan = true;
        }
      } catch {
        parsed = parseRegistrationText(pasteText);
      }
      const fillKeys = Object.keys(parsed).filter(
        (k) => k !== "id_type" && String(parsed[k] || "").trim()
      );
      if (!fillKeys.length && !parsed.id_type) {
        messageApi.warning("未识别到可填充字段，请检查关键字格式");
        return;
      }
      const nextIdType = parsed.id_type || "";
      const rest = { ...parsed };
      delete rest.id_type;
      delete rest.contact_email;
      const keepEmail =
        (fields.contact_email || "").trim() || defaultEmail;
      const officeKeep: Record<string, string> = {};
      for (const k of [
        "office_flat_floor",
        "office_building",
        "office_street",
        "office_district",
      ]) {
        if ((fields[k] || "").trim()) officeKeep[k] = fields[k];
      }
      setIdFile(undefined);
      setTaiwanIdFile(undefined);
      setIdTypeUserEdited(false);
      setIdTypeFromTextLlm(false);
      setTaiwanPassport(Boolean(taiwan));
      setFields({
        registered_capital: rest.registered_capital || "1万港币",
        ...(keepEmail ? { contact_email: keepEmail } : {}),
        ...officeKeep,
        ...rest,
      });
      if (
        nextIdType &&
        ID_TYPE_OPTIONS.some((o) => o.value === nextIdType)
      ) {
        setIdType(nextIdType);
        setIdTypeFromTextLlm(true);
      } else {
        setIdType("PRC_ID");
      }
      messageApi.success(`已填充 ${fillKeys.length || 1} 项`);
    } finally {
      setParsing(false);
    }
  }

  async function onIdFileChange(file: File | undefined) {
    setIdFile(file);
    if (!file || !file.type.startsWith("image/")) return;
    try {
      const dataUrl = await readFileAsDataUrl(file);
      const current: Record<string, string> = { ...fields };
      if (!idTypeUserEdited && !idTypeFromTextLlm) {
        delete current.id_type;
      } else {
        current.id_type = idType;
      }
      const res = await api.registerRunner.extractId({
        data_url: dataUrl,
        filename: file.name,
        current_fields: current,
        fill_empty_only: true,
      });
      const filled = res.fields || {};
      const skipType = idTypeUserEdited || idTypeFromTextLlm;
      setFields((p) => {
        const next = { ...p };
        for (const [k, v] of Object.entries(filled)) {
          if (k === "id_type") continue;
          if (!(next[k] || "").trim() && v) next[k] = v;
        }
        return next;
      });
      if (
        !skipType &&
        filled.id_type &&
        ID_TYPE_OPTIONS.some((o) => o.value === filled.id_type)
      ) {
        setIdType(filled.id_type);
      }
    } catch {
      /* 视觉识别失败不阻断手工上传 */
    }
  }

  function validate(): string | null {
    if (
      !(fields.company_name_cn || "").trim() &&
      !(fields.company_name_en || "").trim()
    ) {
      return "公司中文名或英文名至少填一个";
    }
    if (!(fields.director_name || "").trim()) return "董事兼股东姓名必填";
    if (!(fields.id_number || "").trim()) return "证件号码必填";
    const email = contactEmailOnly(fields.contact_email || "");
    if (!email) return "联络邮箱必填";
    if (!email.includes("@")) return "联络邮箱格式无效";
    const hasAddr = [
      "director_address_cn",
      "director_address_en",
      "registered_office_cn",
      "registered_office_en",
    ].some((k) => (fields[k] || "").trim());
    if (!hasAddr) return "至少填写一个地址（住址或注册地址）";
    if (!idFile) return "请上传证件文件（PDF 或图片）";
    if (
      idType === "PASSPORT" &&
      taiwanPassport &&
      !(fields.director_address_cn || "").trim()
    ) {
      return "台湾护照请填写住址中文";
    }
    return null;
  }

  async function onSubmit() {
    const err = validate();
    if (err) {
      messageApi.warning(err);
      return;
    }
    setSubmitting(true);
    try {
      const files: Record<string, RunnerFile> = {};
      if (idFile) {
        const dataUrl = await readFileAsDataUrl(idFile);
        files[primaryIdFileKey(idType)] = { name: idFile.name, data_url: dataUrl };
      }
      if (taiwanIdFile) {
        const dataUrl = await readFileAsDataUrl(taiwanIdFile);
        files.taiwan_id = { name: taiwanIdFile.name, data_url: dataUrl };
      }
      const payload = {
        ...fields,
        contact_email: contactEmailOnly(fields.contact_email || ""),
        id_type: idType,
        issuing_country: fields.issuing_country || "",
        paste_text: pasteText,
        id_type_user_edited: idTypeUserEdited ? "1" : "0",
        taiwan_passport: taiwanPassport ? "1" : "0",
      };
      const res = await api.registerRunner.submit(payload, files, false);
      messageApi.success(
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
        dry_run: false,
      });
      setPolling(true);
      resetFormForNext();
    } catch (e) {
      const msg = (e as Error).message || "提交失败";
      if (
        msg.includes("证件号码已被注册") ||
        (e instanceof ApiError && e.status === 409 && msg.includes("已被注册"))
      ) {
        Modal.warning({
          title: "已被注册",
          content: "该证件号码已被注册，无法再跑任务。",
        });
      } else {
        messageApi.error(msg);
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <Spin spinning={parsing}>
        <div className="register-page">
      <section className="reg-card reg-paste-card">
        <h2>快速填充</h2>
        <small className="muted">
          粘贴整段注册信息（含「中文名：」「英文名：」「注册资本：」「经营范围：」「注册地址：」「董事：」「身份证号码：」「香港身份证号码：」「护照号码：」「住址中文：」「住址英文：」等关键字），点「解析填充」自动写入下方表单，并由大模型判定证件类型。
        </small>
        <textarea
          className="reg-paste-area"
          rows={10}
          placeholder={
            "1、公司名称\n 中文名：撼世全球有限公司\n 英文名：Humsienk Global Limited\n2、注册资本：1万港币\n3、经营范围：新能源產品、電子元器件銷售，電子商務，國際貿易\n4、注册地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O\n 909O 9/F., High Fashion Centre,1-11 Kwai Hei Street, Kwai Chung,New Territories, Hong Kong\n\n董事+股东：姚曉佳\n身份证号码：44051420000318492X\n住址中文：广东省深圳市南山区西丽南路8号110室\n住址英文：Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province"
          }
          value={pasteText}
          onChange={(e) => setPasteText(e.target.value)}
          disabled={submitting || parsing}
        />
        <div className="reg-paste-actions">
          <Button
            type="primary"
            loading={parsing}
            disabled={submitting || !pasteText.trim()}
            onClick={onParse}
          >
            解析填充
          </Button>
          <Button
            disabled={submitting || parsing}
            onClick={() => setPasteText("")}
          >
            清空
          </Button>
        </div>
      </section>

      <div className="reg-grid">
        <section className="reg-card">
          <h2>公司资料</h2>
          <p className="muted" style={{ margin: "0 0 8px" }}>
            公司中文名、英文名至少填一个，两个都填也可以
          </p>
          <div className="reg-form">
            {TEXT_FIELDS.map((f) => (
              <Fragment key={f.key}>
              <label className="reg-field">
                <span>
                  {f.label}
                  {f.required ? <em>*</em> : null}
                </span>
                {f.key === "contact_email" ? (
                  <>
                    <Select
                      showSearch
                      optionFilterProp="label"
                      placeholder={f.placeholder}
                      value={fields.contact_email || undefined}
                      onChange={(v) =>
                        setField("contact_email", contactEmailOnly(String(v || "")))
                      }
                      disabled={submitting}
                      options={emailOptions}
                      style={{ width: "100%" }}
                      optionRender={(option) => {
                        const remark = (option.data as EmailOption).remark;
                        return (
                          <>
                            <span>{String(option.value)}</span>
                            {remark ? (
                              <span className="reg-email-remark">{remark}</span>
                            ) : null}
                          </>
                        );
                      }}
                      labelRender={(item) => {
                        const remark =
                          emailOptions.find((o) => o.value === item.value)
                            ?.remark || "";
                        return (
                          <>
                            <span>{item.value}</span>
                            {remark ? (
                              <span className="reg-email-remark">{remark}</span>
                            ) : null}
                          </>
                        );
                      }}
                    />
                    <small className="muted">
                      选项来自 <Link to="/email-config">邮箱账号配置</Link>
                    </small>
                  </>
                ) : (
                  <input
                    type="text"
                    value={fields[f.key] || ""}
                    placeholder={f.placeholder}
                    onChange={(e) => setField(f.key, e.target.value)}
                    disabled={submitting}
                  />
                )}
                {f.key === "director_address_en" &&
                (fields.director_address_cn || fields.director_address_en) ? (
                  <small
                    className={isLocalHkAddress(fields) ? "badge ok" : "badge warn"}
                  >
                    {isLocalHkAddress(fields)
                      ? "香港地址（本地地址）"
                      : `非香港地址 · 国家=${
                          fields.address_country || "中國"
                        }`}
                  </small>
                ) : null}
              </label>
              {f.key === "director_name" ? (
                <div className="reg-id-row with-country">
                  <label className="reg-field">
                    <span>中文姓名</span>
                    <input
                      type="text"
                      value={fields.director_name_cn || ""}
                      onChange={(e) => setField("director_name_cn", e.target.value)}
                      disabled={submitting}
                    />
                  </label>
                  <label className="reg-field">
                    <span>英文姓氏</span>
                    <input
                      type="text"
                      value={fields.director_surname_en || ""}
                      onChange={(e) =>
                        setField("director_surname_en", e.target.value)
                      }
                      disabled={submitting}
                    />
                  </label>
                  <label className="reg-field">
                    <span>英文名字</span>
                    <input
                      type="text"
                      value={fields.director_given_en || ""}
                      onChange={(e) =>
                        setField("director_given_en", e.target.value)
                      }
                      disabled={submitting}
                    />
                  </label>
                </div>
              ) : null}
              {f.key === "director_address_en" ? (
                isLocalHkAddress(fields) ? (
                  <>
                    <div className="reg-id-row">
                      <label className="reg-field">
                        <span>室／楼／座</span>
                        <input
                          type="text"
                          value={fields.director_address_flat || ""}
                          onChange={(e) =>
                            setField("director_address_flat", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                      <label className="reg-field">
                        <span>大厦</span>
                        <input
                          type="text"
                          value={fields.director_address_building || ""}
                          onChange={(e) =>
                            setField("director_address_building", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                    </div>
                    <div className="reg-id-row">
                      <label className="reg-field">
                        <span>街道／屋苑／地段／村</span>
                        <input
                          type="text"
                          value={fields.director_address_street || ""}
                          onChange={(e) =>
                            setField("director_address_street", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                      <label className="reg-field">
                        <span>郵遞區號</span>
                        <Select
                          showSearch
                          allowClear
                          optionFilterProp="label"
                          placeholder="选择区"
                          value={fields.director_address_region || undefined}
                          onChange={(v) =>
                            setField("director_address_region", v || "")
                          }
                          disabled={submitting}
                          options={s03Districts.map((label) => ({
                            value: label,
                            label,
                          }))}
                          style={{ width: "100%" }}
                        />
                      </label>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="reg-id-row">
                      <label className="reg-field">
                        <span>室／楼／座</span>
                        <input
                          type="text"
                          value={fields.director_address_flat || ""}
                          onChange={(e) =>
                            setField("director_address_flat", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                      <label className="reg-field">
                        <span>大厦</span>
                        <input
                          type="text"
                          value={fields.director_address_building || ""}
                          onChange={(e) =>
                            setField("director_address_building", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                    </div>
                    <div className="reg-address-stack">
                      <label className="reg-field">
                        <span>街道／屋苑／地段／村</span>
                        <input
                          type="text"
                          value={fields.director_address_street || ""}
                          onChange={(e) =>
                            setField("director_address_street", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                      <label className="reg-field">
                        <span>区／市／省／州／邮递区号</span>
                        <input
                          type="text"
                          value={fields.director_address_region || ""}
                          onChange={(e) =>
                            setField("director_address_region", e.target.value)
                          }
                          disabled={submitting}
                        />
                      </label>
                      <label className="reg-field">
                        <span>国家／地区</span>
                        <Select
                          showSearch
                          allowClear
                          optionFilterProp="label"
                          placeholder="搜索国家"
                          value={fields.address_country || undefined}
                          onChange={(v) => setField("address_country", v || "")}
                          disabled={submitting}
                          options={s03Countries.map((label) => ({
                            value: label,
                            label,
                          }))}
                          style={{ width: "100%" }}
                        />
                      </label>
                    </div>
                  </>
                )
              ) : null}
              {f.key === "director_name" ? (
                <div
                  className={
                    "reg-id-row" + (idType === "PASSPORT" ? " with-country" : "")
                  }
                >
                  <label className="reg-field">
                    <span>身份证明类型</span>
                    <select
                      value={idType}
                      onChange={(e) => {
                        setIdType(e.target.value);
                        setIdTypeUserEdited(true);
                        setIdFile(undefined);
                        if (e.target.value !== "PASSPORT") {
                          setTaiwanIdFile(undefined);
                          setTaiwanPassport(false);
                        }
                      }}
                      disabled={submitting}
                    >
                      {ID_TYPE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="reg-field">
                    <span>
                      证件号码
                      <em>*</em>
                    </span>
                    <input
                      type="text"
                      value={fields.id_number || ""}
                      onChange={(e) => setField("id_number", e.target.value)}
                      disabled={submitting}
                    />
                  </label>
                  {idType === "PASSPORT" ? (
                    <label className="reg-field">
                      <span>护照签发地</span>
                      <Select
                        showSearch
                        allowClear
                        optionFilterProp="label"
                        placeholder="搜索国家"
                        value={fields.issuing_country || undefined}
                        onChange={(v) => setField("issuing_country", v || "")}
                        disabled={submitting}
                        options={PASSPORT_COUNTRIES.map((c) => ({
                          value: c.code,
                          label: countryLabel(c),
                        }))}
                        style={{ width: "100%" }}
                      />
                      {taiwanPassport ? (
                        <small className="muted">台湾护照签发地为台湾；可另传台证抽住址</small>
                      ) : null}
                    </label>
                  ) : null}
                </div>
              ) : null}
              </Fragment>
            ))}
          </div>

          <h2>证件文件</h2>
          <div className="reg-form">
            <label className="reg-field">
              <span>
                证件文件（PDF/图片）
                <em>*</em>
              </span>
              <input
                key={`id-${idType}-${idFile ? idFile.name : "empty"}`}
                type="file"
                accept="image/*,application/pdf"
                disabled={submitting}
                onChange={(e) => onIdFileChange(e.target.files?.[0])}
              />
              {idFile ? (
                <small className="muted">{idFile.name}</small>
              ) : (
                <small className="muted">
                  可识别姓名住址；证件类型以粘贴资料为准，手工改过下拉则以下拉为准
                </small>
              )}
            </label>
            {idType === "PASSPORT" && taiwanPassport ? (
              <label className="reg-field">
                <span>台湾身份证（可选）</span>
                <input
                  key={taiwanIdFile ? taiwanIdFile.name : "tw-empty"}
                  type="file"
                  accept="image/*,application/pdf"
                  disabled={submitting}
                  onChange={(e) => setTaiwanIdFile(e.target.files?.[0])}
                />
                {taiwanIdFile ? (
                  <small className="muted">{taiwanIdFile.name}</small>
                ) : (
                  <small className="muted">可另传台证原件，住址请在上方手填</small>
                )}
              </label>
            ) : null}
          </div>

          <div style={{ marginTop: 24, paddingTop: 16, borderTop: "1px solid #e8e8e8" }}>
            <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>公司在香港的注册办事处地址</h3>
            <small className="muted" style={{ display: "block", marginBottom: 12 }}>
              不填写则使用系统默认地址（管理后台 &gt; 默认办事处配置）
            </small>
            <div className="reg-form">
              {OFFICE_FIELDS.map((f) => (
                <label key={f.key} className="reg-field">
                  <span>{f.label}</span>
                  <input
                    type="text"
                    value={fields[f.key] || ""}
                    placeholder="默认用系统配置"
                    onChange={(e) => setField(f.key, e.target.value)}
                    disabled={submitting}
                  />
                </label>
              ))}
            </div>
          </div>

          <div className="reg-actions">
            <Button
              type="primary"
              disabled={submitting}
              loading={submitting}
              onClick={onSubmit}
            >
              {submitting ? "提交中…" : "跑注册"}
            </Button>
          </div>
        </section>

        <section className="reg-card">
          <h2>运行状态</h2>
          <small className="muted" style={{ display: "block", marginBottom: 8 }}>
            显示最近一单进度。可继续填写并提交下一单，任务将排队执行。
          </small>
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
      </Spin>
    </>
  );
}
