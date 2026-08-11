"""解析 H5/群消息中的公司注册表单"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from src.feishu.icris_form_parser import extract_kv_fields

logger = logging.getLogger(__name__)

# 标准中文标签 → field_key
FIELD_MAP = {
    "公司英文名": "company_name_en",
    "公司中文名": "company_name_cn",
    "注册地址": "registered_office",
    "注册地址中文": "registered_office_cn",
    "注册地址英文": "registered_office_en",
    "住址": "director_address_cn",  # 可能为中英混贴整段，后续拆分
    "联络邮箱": "contact_email",
    "联络电话": "contact_phone",
    "香港联络电话": "contact_phone",
    "股东资料": "founder_members",
    "董事资料": "directors",
    "董事兼股东姓名": "director_name",
    "董事兼股东": "director_name",
    "秘书资料": "company_secretary",
    "业务性质": "business_desc",
    "经营范围": "business_desc",
    "商业登记证年限": "br_certificate_years",
    "申请人姓名": "applicant_name",
    "申请人电邮": "applicant_email",
    "申请人电话": "applicant_phone",
    "证件类型": "id_type",
    "证件号码": "id_number",
    "注册资本": "registered_capital",
    "董事住址中文": "director_address_cn",
    "董事住址英文": "director_address_en",
    "住址中文": "director_address_cn",
    "住址英文": "director_address_en",
}

# 别名 / 口语标签 → 标准中文标签（再映射到 field_key）
FIELD_ALIASES_CN: dict[str, str] = {
    "公司名称": "公司英文名",
    "英文名": "公司英文名",
    "英文公司名": "公司英文名",
    "拟用公司英文名": "公司英文名",
    "公司英文名称": "公司英文名",
    "中文名": "公司中文名",
    "拟用公司中文名": "公司中文名",
    "公司中文名称": "公司中文名",
    "公司注册地址": "注册地址",
    "香港地址": "注册地址",
    "注册办公地址": "注册地址",
    "地址中文": "注册地址中文",
    "地址英文": "注册地址英文",
    "注册地中文": "注册地址中文",
    "注册地英文": "注册地址英文",
    "邮箱": "联络邮箱",
    "电邮": "联络邮箱",
    "公司邮箱": "联络邮箱",
    "电话": "联络电话",
    "手机": "联络电话",
    "公司电话": "联络电话",
    "香港电话": "香港联络电话",
    "股东": "股东资料",
    "创办成员": "股东资料",
    "股东/创办成员": "股东资料",
    "董事": "董事兼股东姓名",
    "董事姓名": "董事兼股东姓名",
    "董事+股东": "董事兼股东姓名",
    "董事＋股东": "董事兼股东姓名",
    "董事及股东": "董事兼股东姓名",
    "股东兼董事": "董事兼股东姓名",
    "姓名": "董事兼股东姓名",
    "秘书": "秘书资料",
    "公司秘书": "秘书资料",
    "业务": "经营范围",
    "业务描述": "经营范围",
    "业务性质描述": "经营范围",
    "商业登记证": "商业登记证年限",
    "商业登记年限": "商业登记证年限",
    "申请人": "申请人姓名",
    "申请人名字": "申请人姓名",
    "申请人邮箱": "申请人电邮",
    "申请人电邮地址": "申请人电邮",
    "申请人手机": "申请人电话",
    "身份证明类型": "证件类型",
    "身份证类型": "证件类型",
    "id_type": "证件类型",
    "身份证号码": "证件号码",
    "身分證號碼": "证件号码",
    "护照号码": "证件号码",
    "护照號碼": "证件号码",
    "id_number": "证件号码",
    "号码": "证件号码",
    "注册资金": "注册资本",
    "股本": "注册资本",
    "董事地址中文": "住址中文",
    "董事地址英文": "住址英文",
}

# 资料相关关键词（意图识别用）
MATERIAL_KEYWORDS = (
    "公司名", "公司英文", "公司中文", "注册地址", "股东", "董事", "秘书",
    "身份证", "地址证明", "护照", "申请人", "联络邮箱", "联络电话",
    "创办成员", "业务性质", "经营范围", "商业登记", "英文名", "中文名",
    "证件类型", "证件号码", "身分證", "注册资本", "住址", "董事兼股东",
)

_LABEL_TO_KEY: dict[str, str] = {}
for _cn, _key in FIELD_MAP.items():
    _LABEL_TO_KEY[_cn] = _key
for _alias, _cn in FIELD_ALIASES_CN.items():
    _LABEL_TO_KEY[_alias] = FIELD_MAP[_cn]


@dataclass
class FormParseResult:
    ok: bool
    fields: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_label(label: str) -> str:
    lab = re.sub(r"[\s　#]+", "", (label or "").strip())
    # 去掉「1、」「2.」「3）」等小节前缀（客户常按清单编号粘贴）
    lab = re.sub(r"^\d+[、.．\)）]\s*", "", lab)
    if lab in FIELD_MAP:
        return lab
    return FIELD_ALIASES_CN.get(lab, lab)


def _label_to_field_key(label: str) -> str | None:
    lab = _normalize_label(label)
    if lab in FIELD_MAP:
        return FIELD_MAP[lab]
    return _LABEL_TO_KEY.get(lab)


def parse_registration_form(text: str) -> FormParseResult:
    fields = extract_material_fields(text)

    required = [
        "company_name_en",
        "company_name_cn",
        "registered_office_cn",
        "registered_office_en",
        "registered_capital",
        "business_desc",
        "director_name",
        "id_number",
        "director_address_cn",
        "director_address_en",
    ]
    # 旧键兼容：单行注册地址 / 董事资料
    if not fields.get("registered_office_cn") and fields.get("registered_office"):
        fields["registered_office_cn"] = fields["registered_office"]
    if not fields.get("director_name"):
        fields["director_name"] = (
            fields.get("directors") or fields.get("founder_members") or ""
        )
    missing = [k for k in required if not fields.get(k)]
    # 注册资本可缺（系统默认）
    missing = [k for k in missing if k != "registered_capital"]
    errors: list[str] = []

    email = fields.get("contact_email", "")
    if email and "@" not in email:
        errors.append("联络邮箱格式不正确")

    return FormParseResult(
        ok=len(missing) == 0 and len(errors) == 0,
        fields=fields,
        missing=missing,
        errors=errors,
    )


def extract_material_fields(text: str) -> dict[str, str]:
    """从键=值或自然语言中抽取材料字段（任意命中即可，不要求一次齐全）"""
    fields: dict[str, str] = {}
    if not (text or "").strip():
        return fields

    # 1) 标准键=值 / 键：值（含别名）
    raw = extract_kv_fields(text)
    for cn_or_alias, val in raw.items():
        key = _label_to_field_key(cn_or_alias)
        if key and val.strip():
            fields[key] = val.strip()

    # 行内「标签=值」未按行切开时的补充（支持同行多组键值）
    for m in re.finditer(
        r"([^\s=:：,，。；;]{2,12})\s*[=:：]\s*"
        r"(.+?)(?=\s+[^\s=:：,，。；;]{2,12}\s*[=:：]|$)",
        text,
    ):
        key = _label_to_field_key(m.group(1))
        val = m.group(2).strip().rstrip("。.!！,，；;")
        if key and val and key not in fields:
            fields[key] = val

    # 2) 「标签是/为 XXX」
    for m in re.finditer(
        r"([^\s,:：,，。；;]{2,12})\s*(?:是|为|：|:)\s*([^\n,，；;]+)",
        text,
    ):
        key = _label_to_field_key(m.group(1))
        val = m.group(2).strip().rstrip("。.!！")
        if key and val and len(val) <= 200 and key not in fields:
            # 避免把问句「还缺什么是…」误抽
            if val.startswith("什么") or val.startswith("哪"):
                continue
            fields[key] = val

    # 2b) 「邮箱改成 xx」「公司名更正为 yy」
    for m in re.finditer(
        r"([^\s,:：,，。；;]{2,12})\s*(?:改成|改为|换成|修改为|修改成|更正为|更正成|更正)\s*"
        r"([^\n,，；;]+)",
        text,
    ):
        key = _label_to_field_key(m.group(1))
        val = m.group(2).strip().rstrip("。.!！")
        if key and val and len(val) <= 200:
            fields[key] = val

    # 3) 「董事张三」「股东是李四」「董事兼股东姚曉佳」
    _role_stop = {
        "资料", "姓名", "信息", "相关", "文件", "证明", "身份证", "护照",
        "复印件", "扫描件", "发给", "给你", "如下", "照片", "兼股东",
    }
    for label, key in (
        ("董事兼股东", "director_name"),
        ("董事", "director_name"),
        ("股东", "founder_members"),
        ("秘书", "company_secretary"),
    ):
        if key in fields:
            continue
        m = re.search(
            rf"{label}(?:资料|姓名)?\s*(?:是|为|:|：)\s*"
            rf"([A-Za-z\u4e00-\u9fff·]{{2,40}})",
            text,
        )
        if m:
            val = m.group(1).strip().rstrip("。.!！")
            if val and not val.startswith("什么") and not any(val.startswith(s) for s in _role_stop):
                fields[key] = val
                continue
        m2 = re.search(
            rf"{label}([A-Za-z]{{2,30}}|[\u4e00-\u9fff·]{{2,4}})(?=[,，。；;\s]|$)",
            text,
        )
        if m2:
            val = m2.group(1).strip()
            if val not in _role_stop:
                fields[key] = val

    # 旧键同步到 director_name
    if not fields.get("director_name"):
        fields["director_name"] = (
            fields.get("directors") or fields.get("founder_members") or ""
        )
        if not fields["director_name"]:
            fields.pop("director_name", None)
    elif not fields.get("directors"):
        fields["directors"] = fields["director_name"]
    if fields.get("director_name") and not fields.get("founder_members"):
        fields["founder_members"] = fields["director_name"]

    # 捕获「注册地址：中文\n英文」等多行地址块（正则单行易丢英文行）
    _capture_multiline_address_blocks(text, fields)

    # 4) 松散：邮箱 / 电话（仅当尚未填对应字段）
    if "contact_email" not in fields:
        em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
        if em:
            fields["contact_email"] = em.group(0)
    if "contact_phone" not in fields:
        ph = re.search(
            r"(?:电话|手机|联络电话|联系电话)[是为:：\s]*([+\d][\d\s-]{6,})",
            text,
        )
        if ph:
            fields["contact_phone"] = re.sub(r"\s+", "", ph.group(1))
        else:
            ph2 = re.search(r"(?<!\d)(1[3-9]\d{9}|852\d{8}|\+852\d{8})(?!\d)", text)
            if ph2 and ("电话" in text or "手机" in text or "联络" in text):
                fields["contact_phone"] = ph2.group(1)

    # 5) LLM 辅助字段提取兜底（优化 10）
    regex_field_count = sum(1 for v in fields.values() if v)
    min_fields = int(getattr(settings, "materials_llm_extraction_min_fields", 2) or 2)
    if (
        getattr(settings, "materials_llm_extraction_enabled", True)
        and regex_field_count < min_fields
        and len(text.strip()) >= 10
    ):
        llm_fields = _llm_extract_fields(text)
        if llm_fields:
            valid_keys = set(FIELD_MAP.values()) | {
                "director_name",
                "registered_office_cn",
                "registered_office_en",
                "registered_capital",
                "director_address_cn",
                "director_address_en",
            }
            for k, v in llm_fields.items():
                if k in valid_keys and v and not fields.get(k):
                    fields[k] = v

    # 6) 中英混贴地址 → LLM/启发式拆分（与字段数兜底解耦，检测到即拆）
    _resolve_bilingual_addresses(fields)

    return _normalize_fields(fields)


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _looks_bilingual_address(text: str) -> bool:
    """同时含足够中文与英文时视为混贴。"""
    t = (text or "").strip()
    if not t:
        return False
    cjk = len(_CJK_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    return cjk >= 2 and latin >= 8


def _heuristic_split_address(raw: str) -> tuple[str, str]:
    """按行拆分：偏中文行→cn，偏英文行→en。"""
    cn_lines: list[str] = []
    en_lines: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip().rstrip("，,；;")
        if not line:
            continue
        cjk = len(_CJK_RE.findall(line))
        latin = len(_LATIN_RE.findall(line))
        if cjk >= 2 and cjk >= max(1, latin // 3):
            cn_lines.append(line)
        elif latin >= 5:
            en_lines.append(line)
        elif cjk:
            cn_lines.append(line)
        else:
            en_lines.append(line)
    return "\n".join(cn_lines).strip(), "\n".join(en_lines).strip()


def _split_address_blob(raw: str) -> tuple[str, str]:
    """优先 LLM 拆分，失败回退启发式。"""
    text = (raw or "").strip()
    if not text:
        return "", ""
    if not _looks_bilingual_address(text):
        # 单语：有中文当 cn，否则当 en
        if _CJK_RE.search(text):
            return text, ""
        return "", text
    try:
        from src.llm.openai_client import LLMClient

        if getattr(settings, "materials_llm_extraction_enabled", True) and (
            settings.openai_api_key or ""
        ).strip():
            parts = LLMClient().split_bilingual_address(text)
            cn = (parts.get("cn") or "").strip()
            en = (parts.get("en") or "").strip()
            if cn or en:
                # LLM 只出一侧时用启发式补另一侧
                if not cn or not en:
                    h_cn, h_en = _heuristic_split_address(text)
                    cn = cn or h_cn
                    en = en or h_en
                return cn, en
    except Exception as e:
        logger.debug("双语地址 LLM 拆分不可用: %s", e)
    return _heuristic_split_address(text)


def _capture_multiline_address_blocks(text: str, fields: dict[str, str]) -> None:
    """把「注册地址：…」后的续行（常为英文地址）并入 blob。"""
    # 注册地址块：标签后直到下一「数字、」小节或下一「短标签=」
    office_pat = re.compile(
        r"(?:^|\n)\s*(?:4[、.\s]*)?(?:注册地址|公司注册地址|香港注册地址)"
        r"[^\n:：=]{0,8}[:：=]\s*(.+?)"
        r"(?=\n\s*\d+[、.]|\n\s*(?:董事兼股东|董事|股东|姓名|身份证|住址|经营范围|注册资本|"
        r"联络|邮箱|电话|中文名|英文名)\b|\Z)",
        re.S | re.I,
    )
    m = office_pat.search(text or "")
    if m:
        blob = re.sub(r"[ \t]+\n", "\n", m.group(1)).strip()
        blob = re.sub(r"\n{3,}", "\n\n", blob)
        if blob and (
            not fields.get("registered_office")
            or len(blob) > len(fields.get("registered_office") or "")
        ):
            fields["registered_office"] = blob

    # 住址整段（非「住址中文/英文」分项）
    res_pat = re.compile(
        r"(?:^|\n)\s*(?:住址|常住地址|董事住址)(?!中文|英文)"
        r"[^\n:：=]{0,6}[:：=]\s*(.+?)"
        r"(?=\n\s*\d+[、.]|\n\s*(?:联络|邮箱|电话|证件|身份证|经营范围|注册|"
        r"董事兼股东|姓名)\b|\Z)",
        re.S | re.I,
    )
    m2 = res_pat.search(text or "")
    if m2:
        blob = re.sub(r"[ \t]+\n", "\n", m2.group(1)).strip()
        if blob and _looks_bilingual_address(blob):
            # 暂存到 cn，后续统一拆分；避免覆盖已明确的分项
            if not fields.get("director_address_cn") and not fields.get(
                "director_address_en"
            ):
                fields["director_address_cn"] = blob


def _resolve_bilingual_addresses(fields: dict[str, str]) -> None:
    """注册地址 / 住址：混贴则拆成 cn/en。"""
    # --- 注册地址 ---
    office_blob = (
        fields.get("registered_office")
        or fields.get("registered_office_cn")
        or ""
    ).strip()
    cn = (fields.get("registered_office_cn") or "").strip()
    en = (fields.get("registered_office_en") or "").strip()
    need_office_split = False
    if office_blob and _looks_bilingual_address(office_blob):
        need_office_split = not (cn and en and cn != office_blob) or (
            cn == office_blob and not en
        ) or _looks_bilingual_address(cn)
    elif office_blob and not cn and not en:
        need_office_split = True
    elif cn and _looks_bilingual_address(cn) and not en:
        office_blob = cn
        need_office_split = True
    elif en and _looks_bilingual_address(en) and not cn:
        office_blob = en
        need_office_split = True

    if need_office_split and office_blob:
        split_cn, split_en = _split_address_blob(office_blob)
        if split_cn:
            fields["registered_office_cn"] = split_cn
        if split_en:
            fields["registered_office_en"] = split_en
        # 保留原文便于审计
        if not fields.get("registered_office"):
            fields["registered_office"] = office_blob
    elif fields.get("registered_office") and not fields.get("registered_office_cn"):
        # 单语中文兜底
        if _CJK_RE.search(fields["registered_office"]):
            fields["registered_office_cn"] = fields["registered_office"]
        elif not fields.get("registered_office_en"):
            fields["registered_office_en"] = fields["registered_office"]

    # --- 住址 ---
    res_cn = (fields.get("director_address_cn") or "").strip()
    res_en = (fields.get("director_address_en") or "").strip()
    res_blob = res_cn if _looks_bilingual_address(res_cn) else ""
    if res_blob or (res_cn and not res_en and _looks_bilingual_address(res_cn)):
        split_cn, split_en = _split_address_blob(res_cn)
        if split_cn:
            fields["director_address_cn"] = split_cn
        if split_en:
            fields["director_address_en"] = split_en
    elif res_en and not res_cn and _looks_bilingual_address(res_en):
        split_cn, split_en = _split_address_blob(res_en)
        if split_cn:
            fields["director_address_cn"] = split_cn
        if split_en:
            fields["director_address_en"] = split_en


def _llm_extract_fields(text: str) -> dict[str, str]:
    """LLM 辅助提取字段（优化 10）。不可用时静默返回空。"""
    try:
        from src.llm.openai_client import LLMClient

        client = LLMClient()
        return client.extract_material_fields_llm(text)
    except Exception as e:
        logger.debug("LLM 字段提取兜底不可用: %s", e)
        return {}


def _normalize_phone(raw: str) -> str:
    """统一电话格式：去空格/横线，补国际前缀。

    - 已有 + 前缀：保留
    - 852 开头（8 位）：补 +852
    - 1[3-9] 开头的 11 位大陆号码：补 +86
    - 其他：保持原样（去空格横线后）
    """
    s = re.sub(r"[\s\-()+]+", "", (raw or "")).strip()
    if not s:
        return ""
    if s.startswith("+"):
        return s
    # 香港号码：852 + 8 位
    if s.startswith("852") and len(s) == 11:
        return f"+{s}"
    # 大陆号码：1[3-9]xxxxxxxxx
    if re.match(r"^1[3-9]\d{9}$", s):
        return f"+86{s}"
    # 已是 8 位香港号码（无 852 前缀）难以判断，保守不加前缀
    return s


def _normalize_email(raw: str) -> str:
    """统一邮箱格式：去空格、转小写、去尾部多余标点。"""
    s = (raw or "").strip().lower()
    s = s.rstrip(".,;:。；：，")
    s = re.sub(r"\s+", "", s)
    return s


def _normalize_company_name(raw: str) -> str:
    """统一公司名格式：全角→半角括号，压缩空格，去首尾标点。"""
    s = (raw or "").strip()
    if not s:
        return ""
    # 全角括号 → 半角
    s = s.translate(str.maketrans({"（": "(", "）": ")"}))
    # 压缩连续空格
    s = re.sub(r"\s+", " ", s).strip()
    # 去首尾标点
    s = s.strip(".,;:。；：，()（）")
    return s


def _normalize_fields(fields: dict[str, str]) -> dict[str, str]:
    """统一字段标准化入口：id_type/id_number + phone/email/company_name。

    供正则提取后与 LLM 提取兜底后复用（见优化 10）。
    """
    if not fields:
        return fields
    if "id_type" in fields:
        fields["id_type"] = _normalize_id_type_value(fields["id_type"])
    if "id_number" in fields:
        fields["id_number"] = fields["id_number"].strip().replace(" ", "")
    for key in ("contact_phone", "applicant_phone"):
        if fields.get(key):
            fields[key] = _normalize_phone(fields[key])
    for key in ("contact_email", "applicant_email"):
        if fields.get(key):
            fields[key] = _normalize_email(fields[key])
    for key in ("company_name_en", "company_name_cn"):
        if fields.get(key):
            fields[key] = _normalize_company_name(fields[key])
    return fields


def _normalize_id_type_value(raw: str) -> str:
    t = (raw or "").strip().upper()
    if t in ("HKID", "HK", "HK_ID"):
        return "HKID"
    if t in ("PRC_ID", "PRC", "CN_ID", "CHINA_ID"):
        return "PRC_ID"
    if t in ("PASSPORT", "PP"):
        return "PASSPORT"
    if re.search(r"香港", raw or ""):
        return "HKID"
    if re.search(r"护照|護照", raw or ""):
        return "PASSPORT"
    if re.search(r"中国|大陸|大陆|居民身份证|身分證|身份证", raw or ""):
        return "PRC_ID"
    return t or raw.strip()


def text_looks_like_material_submit(text: str) -> bool:
    """启发式：是否像在提交注册资料"""
    t = (text or "").strip()
    if not t:
        return False
    if extract_material_fields(t):
        return True
    # 多行键值
    kv_lines = sum(
        1 for line in t.splitlines()
        if re.match(r"^[^:=：]{1,20}[=:：]", line.strip())
    )
    if kv_lines >= 2:
        return True
    # 标签出现次数
    hit = sum(1 for lab in _LABEL_TO_KEY if lab in t)
    if hit >= 2:
        return True
    if hit >= 1 and (
        "=" in t
        or "：" in t
        or ":" in t
        or re.search(r"(?<![董监])是", t)  # 「董事是张三」；勿把「董事」里的「是」算进去
    ):
        return True
    email_n = len(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", t))
    if email_n >= 1 and any(k in t for k in ("公司", "董事", "股东", "注册", "申请人")):
        return True
    return False


def text_has_material_keyword(text: str) -> bool:
    t = text or ""
    return any(k in t for k in MATERIAL_KEYWORDS)


def fields_to_material_rows(fields: dict[str, str], source: str = "form") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, val in fields.items():
        if val:
            rows.append(
                {
                    "field_key": key,
                    "field_value": val,
                    "file_path": "",
                    "source": source,
                    "status": "received",
                }
            )
    return rows
