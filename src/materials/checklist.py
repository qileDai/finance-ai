"""材料清单字段定义（对齐 material_checklist.md / company_registration.json）"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MaterialField:
    key: str
    label: str
    required: bool = True
    field_type: str = "text"  # text | file


# Phase 2/3 结构化检查项
MATERIAL_FIELDS: list[MaterialField] = [
    MaterialField("company_name_en", "拟用公司英文名"),
    MaterialField("company_name_cn", "拟用公司中文名", required=False),
    MaterialField("registered_office", "公司注册地址（香港）"),
    MaterialField("contact_email", "公司联络邮箱"),
    MaterialField("contact_phone", "公司联络电话"),
    MaterialField("founder_members", "股东/创办成员资料"),
    MaterialField("directors", "董事资料"),
    MaterialField("company_secretary", "公司秘书资料"),
    MaterialField("business_desc", "业务性质描述"),
    MaterialField("br_certificate_years", "商业登记证有效期（1或3年）", required=False),
    MaterialField("applicant_name", "ICRIS 申请人姓名"),
    MaterialField("applicant_email", "ICRIS 申请人电邮"),
    MaterialField("applicant_phone", "ICRIS 申请人电话"),
    MaterialField("id_card_front", "身份证明（正面）", field_type="file"),
    MaterialField("id_card_back", "身份证明（反面）", field_type="file", required=False),
    MaterialField("address_proof", "地址证明", field_type="file", required=False),
    MaterialField("passport", "护照复印件", field_type="file", required=False),
]

FILE_FIELD_KEYS = {f.key for f in MATERIAL_FIELDS if f.field_type == "file"}
REQUIRED_FIELD_KEYS = {f.key for f in MATERIAL_FIELDS if f.required}


def progress_summary(materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根据 group_materials 计算进度"""
    received = 0
    missing: list[str] = []
    for f in MATERIAL_FIELDS:
        row = materials.get(f.key) or {}
        status = row.get("status", "missing")
        has_value = bool(row.get("field_value") or row.get("file_path"))
        if status in ("received", "confirmed", "needs_review") and has_value:
            received += 1
        elif f.required:
            missing.append(f.label)
    total_required = len(REQUIRED_FIELD_KEYS)
    return {
        "received": received,
        "total": len(MATERIAL_FIELDS),
        "total_required": total_required,
        "missing_labels": missing,
        "complete": len(missing) == 0,
    }


def format_progress_text(materials: dict[str, dict[str, Any]]) -> str:
    p = progress_summary(materials)
    lines = [
        f"材料收集进度：{p['received']}/{p['total']} 项",
        f"必填项剩余：{len(p['missing_labels'])} 项",
    ]
    if p["missing_labels"]:
        lines.append("还缺：")
        lines.extend(f"  - {lbl}" for lbl in p["missing_labels"][:10])
        if len(p["missing_labels"]) > 10:
            lines.append(f"  … 等共 {len(p['missing_labels'])} 项")
    else:
        lines.append("必填项已齐全，请回复「确认」进入审核。")
    return "\n".join(lines)
