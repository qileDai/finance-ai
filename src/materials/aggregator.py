"""group_materials → company_registration.json 结构"""

from __future__ import annotations

import json
import re
import secrets
import string
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import settings
from src.materials.checklist import MATERIAL_FIELDS, FILE_FIELD_KEYS, progress_summary


def _get_val(materials: dict[str, dict[str, Any]], key: str, default: str = "") -> str:
    row = materials.get(key) or {}
    return str(row.get("field_value") or default).strip()


def _split_cjk_latin_name(name: str) -> tuple[str, str]:
    """姓名 → (中文部分, 英文部分)。

    纯中文→(name,"")；纯英文→("",name)；混合→(CJK片段, 拉丁片段)。
    与 icris_registration.split_cjk_latin_name 同款实现（本地副本，避免引入 playwright 依赖）。
    """
    name = (name or "").strip()
    if not name:
        return "", ""
    cjk_chars: list[str] = []
    latin_chars: list[str] = []
    for c in name:
        if c.isascii():
            latin_chars.append(c)
            continue
        cat = unicodedata.category(c)
        if cat in ("Lo", "Nl", "Mn") or c in "·•、・":
            cjk_chars.append(c)
        else:
            cjk_chars.append(c)
    return "".join(cjk_chars).strip(), "".join(latin_chars).strip()


def _director_name(materials: dict[str, dict[str, Any]]) -> str:
    return (
        _get_val(materials, "director_name")
        or _get_val(materials, "directors")
        or _get_val(materials, "founder_members")
    )


def _office_cn(materials: dict[str, dict[str, Any]]) -> str:
    return _get_val(materials, "registered_office_cn") or _get_val(
        materials, "registered_office"
    )


def _office_en(materials: dict[str, dict[str, Any]]) -> str:
    return _get_val(materials, "registered_office_en") or _get_val(
        materials, "registered_office"
    )


def _get_files(materials: dict[str, dict[str, Any]]) -> list[str]:
    """已知文件字段 + 未分类 file_*，避免上传丢失。"""
    paths: list[str] = []
    seen: set[str] = set()
    for key, row in materials.items():
        is_file = key in FILE_FIELD_KEYS or key.startswith("file_")
        if not is_file:
            # MATERIAL_FIELDS 里声明为 file 的也纳入
            fdef = next((f for f in MATERIAL_FIELDS if f.key == key), None)
            if not (fdef and fdef.field_type == "file"):
                continue
        p = str(row.get("file_path") or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        paths.append(p)
    return paths


def _generate_icris_credentials() -> tuple[str, str]:
    """生成 ICRIS 账号凭证：用户名=前缀+月日(MMDD)+N位随机字符，密码=用户名+后缀。"""
    prefix = getattr(settings, "icris_username_prefix", "Yingtai")
    rand_len = int(getattr(settings, "icris_username_random_length", 0) or 0)
    if rand_len <= 0:
        # 兼容旧配置名 icris_username_timestamp_digits
        rand_len = int(getattr(settings, "icris_username_timestamp_digits", 4) or 4)
    pw_suffix = getattr(settings, "icris_password_suffix", "@")
    md = datetime.now().strftime("%m%d")
    alphabet = string.ascii_lowercase + string.digits
    rand = "".join(secrets.choice(alphabet) for _ in range(rand_len))
    username = f"{prefix}{md}{rand}"
    password = f"{username}{pw_suffix}"
    return username, password


def _parse_share_capital(cap_str: str) -> int:
    """解析注册资本；支持「1万港币」；空则用配置默认。"""
    default = int(getattr(settings, "materials_default_share_capital", 10000) or 10000)
    s = (cap_str or "").strip()
    if not s:
        return default
    if "万" in s or "萬" in s:
        m = re.search(r"([\d.]+)", s)
        if m:
            try:
                n = int(float(m.group(1)) * 10000)
                return n if n > 0 else default
            except ValueError:
                return default
    digits = re.sub(r"\D", "", s)
    try:
        n = int(digits) if digits else default
    except ValueError:
        n = default
    return n if n > 0 else default


def _build_share_capital(materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cap_int = _parse_share_capital(_get_val(materials, "registered_capital"))
    return {
        "currency": "HKD",
        "total_shares": cap_int,
        "par_value": 1,
        "paid_up": cap_int,
    }


def aggregate_company_data(materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """将群材料库聚合为 ICRIS/打包用的 company_data"""
    br_years = _get_val(materials, "br_certificate_years", "1")
    try:
        br_int = int(re.sub(r"\D", "", br_years) or "1")
    except ValueError:
        br_int = 1

    person = _director_name(materials)
    person_cn = _get_val(materials, "director_name_cn")
    person_en = _get_val(materials, "director_name_en")
    if not person_cn and not person_en:
        person_cn, person_en = _split_cjk_latin_name(person)
    elif not person_cn or not person_en:
        split_cn, split_en = _split_cjk_latin_name(person)
        person_cn = person_cn or split_cn
        person_en = person_en or split_en
    applicant_name_raw = _get_val(materials, "applicant_name")
    if applicant_name_raw:
        am_cn, am_en = _split_cjk_latin_name(applicant_name_raw)
    else:
        am_cn, am_en = "", ""
    contact_email = _get_val(materials, "contact_email") or (
        getattr(settings, "materials_default_contact_email", "") or ""
    ).strip()
    contact_phone = _get_val(materials, "contact_phone") or (
        getattr(settings, "materials_default_contact_phone", "") or ""
    ).strip()
    secretary = _get_val(materials, "company_secretary") or (
        getattr(settings, "materials_default_company_secretary", "") or ""
    ).strip()

    office_cn = _office_cn(materials)
    office_en = _office_en(materials)
    doc_files = _get_files(materials)

    if getattr(settings, "icris_credential_mode", "yingtai") == "yingtai":
        icris_username, icris_password = _generate_icris_credentials()
    else:
        icris_username = ""
        icris_password = ""

    data: dict[str, Any] = {
        "company_name_en": _get_val(materials, "company_name_en"),
        "company_name_cn": _get_val(materials, "company_name_cn"),
        "company_type": "private_limited_by_shares",
        "registered_office": {
            "flat_floor": "",
            "building": "",
            "street": office_en or office_cn,
            "street_cn": office_cn,
            "street_en": office_en,
            "district": "",
            "region": "Hong Kong",
        },
        "contact": {
            "email": contact_email,
            "phone": contact_phone,
        },
        "share_capital": _build_share_capital(materials),
        "founder_members": (
            [
                {
                    "name_en": person_en or person,
                    "name_cn": person_cn,
                    "address_cn": _get_val(materials, "director_address_cn"),
                    "address_en": _get_val(materials, "director_address_en"),
                    "raw": True,
                }
            ]
            if person or person_cn or person_en
            else []
        ),
        "directors": (
            [
                {
                    "name_en": person_en or person,
                    "name_cn": person_cn,
                    "email": contact_email,
                    "address_cn": _get_val(materials, "director_address_cn"),
                    "address_en": _get_val(materials, "director_address_en"),
                    "raw": True,
                }
            ]
            if person or person_cn or person_en
            else []
        ),
        "company_secretary": {"name_en": secretary, "raw": True} if secretary else {},
        "business_nature_desc": _get_val(materials, "business_desc"),
        "br_certificate_years": br_int,
        "applicant": {
            "name_en": person_en or am_en,
            "name_cn": person_cn or am_cn,
            "email": _get_val(materials, "applicant_email") or contact_email,
            "phone": _get_val(materials, "applicant_phone") or contact_phone,
            "id_type": _get_val(materials, "id_type"),
            "id_number": _get_val(materials, "id_number"),
        },
        "identity_proof": {
            "id_type": _get_val(materials, "id_type") or "PRC_ID",
            "id_number": _get_val(materials, "id_number") or "",
            "document_files": doc_files,
            "document_dir": str(Path(doc_files[0]).parent) if doc_files else "",
        },
        "icris_account": {
            "username": icris_username
            or (
                (_get_val(materials, "applicant_email") or contact_email).split("@")[0][:20]
                if (_get_val(materials, "applicant_email") or contact_email)
                else ""
            ),
            "password": icris_password,
        },
    }
    return data


def collect_attachment_paths(materials: dict[str, dict[str, Any]]) -> list[str]:
    return _get_files(materials)


def is_ready_for_confirm(materials: dict[str, dict[str, Any]]) -> bool:
    """必填齐全且无待复核、无跨字段 error。"""
    p = progress_summary(materials)
    if not p.get("complete"):
        return False
    if p.get("needs_review_labels"):
        return False
    # 含未分类 file_* 等不在 MATERIAL_FIELDS 展示列表中的待复核行
    for row in materials.values():
        if str(row.get("status") or "") == "needs_review" and (
            row.get("field_value") or row.get("file_path")
        ):
            return False
    issues = p.get("cross_field_issues") or []
    if any(i.get("level") == "error" for i in issues):
        return False
    return True


def load_company_data_from_roomid(roomid: str) -> dict[str, Any]:
    """从外部群 SQLite 加载材料并聚合为 company_data（供 CLI --roomid 使用）"""
    from src.storage.db import ExternalGroupStore

    store = ExternalGroupStore()
    group = store.get_group(roomid)
    if not group:
        raise ValueError(f"群 {roomid} 不存在于 wework_external.db")
    materials = store.get_materials(roomid)
    if not materials:
        raise ValueError(f"群 {roomid} 尚无材料记录")
    return aggregate_company_data(materials)
