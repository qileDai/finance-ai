"""group_materials → company_registration.json 结构"""

from __future__ import annotations

import json
import re
from typing import Any

from src.materials.checklist import MATERIAL_FIELDS, progress_summary


def _get_val(materials: dict[str, dict[str, Any]], key: str, default: str = "") -> str:
    row = materials.get(key) or {}
    return str(row.get("field_value") or default).strip()


def _get_files(materials: dict[str, dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for f in MATERIAL_FIELDS:
        if f.field_type != "file":
            continue
        row = materials.get(f.key) or {}
        p = row.get("file_path") or row.get("field_value") or ""
        if p:
            paths.append(str(p))
    return paths


def aggregate_company_data(materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """将群材料库聚合为 ICRIS/打包用的 company_data"""
    br_years = _get_val(materials, "br_certificate_years", "1")
    try:
        br_int = int(re.sub(r"\D", "", br_years) or "1")
    except ValueError:
        br_int = 1

    applicant_name = _get_val(materials, "applicant_name")
    name_parts = applicant_name.split(maxsplit=1) if applicant_name else ["", ""]

    doc_files = _get_files(materials)
    data: dict[str, Any] = {
        "company_name_en": _get_val(materials, "company_name_en"),
        "company_name_cn": _get_val(materials, "company_name_cn"),
        "company_type": "private_limited_by_shares",
        "registered_office": {
            "flat_floor": "",
            "building": "",
            "street": _get_val(materials, "registered_office"),
            "district": "",
            "region": "Hong Kong",
        },
        "contact": {
            "email": _get_val(materials, "contact_email"),
            "phone": _get_val(materials, "contact_phone"),
        },
        "share_capital": {
            "currency": "HKD",
            "total_shares": 10000,
            "par_value": 1,
            "paid_up": 10000,
        },
        "founder_members": [
            {"name_en": _get_val(materials, "founder_members"), "raw": True}
        ]
        if _get_val(materials, "founder_members")
        else [],
        "directors": [
            {"name_en": _get_val(materials, "directors"), "email": _get_val(materials, "contact_email"), "raw": True}
        ]
        if _get_val(materials, "directors")
        else [],
        "company_secretary": {"name_en": _get_val(materials, "company_secretary"), "raw": True}
        if _get_val(materials, "company_secretary")
        else {},
        "business_nature_desc": _get_val(materials, "business_desc"),
        "br_certificate_years": br_int,
        "applicant": {
            "name_en": applicant_name,
            "name_cn": "",
            "email": _get_val(materials, "applicant_email") or _get_val(materials, "contact_email"),
            "phone": _get_val(materials, "applicant_phone") or _get_val(materials, "contact_phone"),
        },
        "identity_proof": {
            "document_files": doc_files,
            "document_dir": str(doc_files[0].rsplit("/", 1)[0]) if doc_files else "",
        },
        "icris_account": {
            "username": _get_val(materials, "applicant_email").split("@")[0][:20]
            if _get_val(materials, "applicant_email")
            else "",
            "password": "",
        },
    }
    return data


def collect_attachment_paths(materials: dict[str, dict[str, Any]]) -> list[str]:
    return _get_files(materials)


def is_ready_for_confirm(materials: dict[str, dict[str, Any]]) -> bool:
    return progress_summary(materials)["complete"]
