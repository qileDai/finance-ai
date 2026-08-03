"""解析 H5/群消息中的公司注册表单"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.feishu.icris_form_parser import extract_kv_fields

# 标准中文标签 → field_key
FIELD_MAP = {
    "公司英文名": "company_name_en",
    "公司中文名": "company_name_cn",
    "注册地址": "registered_office",
    "联络邮箱": "contact_email",
    "联络电话": "contact_phone",
    "股东资料": "founder_members",
    "董事资料": "directors",
    "秘书资料": "company_secretary",
    "业务性质": "business_desc",
    "商业登记证年限": "br_certificate_years",
    "申请人姓名": "applicant_name",
    "申请人电邮": "applicant_email",
    "申请人电话": "applicant_phone",
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
    "邮箱": "联络邮箱",
    "电邮": "联络邮箱",
    "公司邮箱": "联络邮箱",
    "电话": "联络电话",
    "手机": "联络电话",
    "公司电话": "联络电话",
    "股东": "股东资料",
    "创办成员": "股东资料",
    "股东/创办成员": "股东资料",
    "董事": "董事资料",
    "董事姓名": "董事资料",
    "秘书": "秘书资料",
    "公司秘书": "秘书资料",
    "业务": "业务性质",
    "业务描述": "业务性质",
    "商业登记证": "商业登记证年限",
    "商业登记年限": "商业登记证年限",
    "申请人": "申请人姓名",
    "申请人名字": "申请人姓名",
    "申请人邮箱": "申请人电邮",
    "申请人电邮地址": "申请人电邮",
    "申请人手机": "申请人电话",
}

# 资料相关关键词（意图识别用）
MATERIAL_KEYWORDS = (
    "公司名", "公司英文", "公司中文", "注册地址", "股东", "董事", "秘书",
    "身份证", "地址证明", "护照", "申请人", "联络邮箱", "联络电话",
    "创办成员", "业务性质", "商业登记", "英文名", "中文名",
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

    required = ["company_name_en", "registered_office", "contact_email", "directors"]
    missing = [k for k in required if not fields.get(k)]
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

    # 行内「标签=值」未按行切开时的补充
    for m in re.finditer(
        r"([^\s=:：,，。；;]{2,12})\s*[=:：]\s*([^\n,，；;]+)",
        text,
    ):
        key = _label_to_field_key(m.group(1))
        val = m.group(2).strip()
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

    # 3) 「董事张三」「股东是李四」
    _role_stop = {
        "资料", "姓名", "信息", "相关", "文件", "证明", "身份证", "护照",
        "复印件", "扫描件", "发给", "给你", "如下", "照片",
    }
    for label, key in (("董事", "directors"), ("股东", "founder_members"), ("秘书", "company_secretary")):
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

    return fields


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
    if hit >= 1 and ("=" in t or "：" in t or ":" in t or "是" in t):
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
