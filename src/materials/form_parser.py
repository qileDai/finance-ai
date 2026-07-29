"""解析 H5/群消息中的公司注册表单"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.feishu.icris_form_parser import extract_kv_fields

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


@dataclass
class FormParseResult:
    ok: bool
    fields: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_registration_form(text: str) -> FormParseResult:
    raw = extract_kv_fields(text)
    fields: dict[str, str] = {}
    for cn, key in FIELD_MAP.items():
        val = raw.get(cn, "").strip()
        if val:
            fields[key] = val

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
