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
from src.storage.db import ExternalGroupStore


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


def _get_default_office() -> dict[str, str]:
    """从 DB 读取默认注册办事处地址 + 公司秘书法人团体信息（NNC1 表格用）。"""
    import json
    defaults = {
        "flat_floor": "ROOM 18 2/F",
        "building": "Tuspark",
        "street": "118 Wai Yip Street",
        "district": "Kwun Tong",
        "secretary_br_no": "78090873",
        "secretary_license_no": "TC010510",
        "secretary_company_no": "0852-52667282",
    }
    try:
        store = ExternalGroupStore()
        raw = store.get_system_setting("icris_default_office") or ""
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    if str(v or "").strip():
                        defaults[k] = str(v).strip()
    except Exception:
        pass
    return defaults


def apply_default_office(data: dict) -> None:
    """将后台配置的默认办事处地址合并到 data 的 registered_office。

    空则补默认，已有值不覆盖（用户填了用用户的）。
    同时补齐 company_secretary 的商业登记证/牌照号/公司号码。
    """
    default = _get_default_office()
    office = data.setdefault("registered_office", {})
    for key in ("flat_floor", "building", "district"):
        if not str(office.get(key) or "").strip():
            office[key] = default.get(key, "")
    if not str(office.get("street_en") or "").strip():
        office["street_en"] = default.get("street", "")
    if not str(office.get("street") or "").strip():
        office["street"] = default.get("street", "")
    if not office.get("region"):
        office["region"] = "Hong Kong"

    sec = data.setdefault("company_secretary", {})
    if not isinstance(sec, dict):
        sec = {}
        data["company_secretary"] = sec
    if not str(sec.get("type") or "").strip():
        sec["type"] = "body_corporate"
    if sec.get("hk_registered") is None:
        sec["hk_registered"] = True
    if not str(sec.get("br_number") or "").strip():
        sec["br_number"] = default.get("secretary_br_no", "")
    if not str(sec.get("license_number") or "").strip():
        sec["license_number"] = default.get("secretary_license_no", "")
    if not str(sec.get("company_number") or "").strip():
        sec["company_number"] = default.get("secretary_company_no", "")


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


def _icris_username_random_length() -> int:
    """ICRIS 用户名末尾随机后缀长度（默认 4）。"""
    try:
        n = int(getattr(settings, "icris_username_random_length", 4) or 4)
    except (TypeError, ValueError):
        n = 4
    return max(1, min(n, 12))


def _icris_username_random_suffix(length: int | None = None) -> str:
    length = length or _icris_username_random_length()
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _yingtai_username_has_random_suffix(username: str, rand_len: int | None = None) -> bool:
    """新规则：base 以 yt 结尾，且末尾有 rand_len 位随机字符。"""
    rand_len = rand_len or _icris_username_random_length()
    u = (username or "").strip()
    if len(u) < rand_len + 3:
        return False
    suffix = u[-rand_len:]
    if not suffix.isalnum() or not suffix.isascii():
        return False
    if not all(c.islower() or c.isdigit() for c in suffix):
        return False
    return u[:-rand_len].endswith("yt")


def _generate_icris_credentials(
    person_en: str = "", id_number: str = "", *, retry: bool = False
) -> tuple[str, str]:
    """生成 ICRIS 账号凭证。

    用户名 = 姓名拼音首字母（小写） + 证件号码后5位 + yt + N位随机（默认4）
    密码 = 用户名 + @（icris_password_suffix 配置，默认 @）
    retry=True 时重新 roll 随机后缀（任务重跑、用户名已被占用）。
    """
    pw_suffix = getattr(settings, "icris_password_suffix", "@") or "@"
    rand_len = _icris_username_random_length()

    initials = ""
    name = (person_en or "").strip()
    if name:
        parts = re.split(r"[\s\-·•、]+", name)
        initials = "".join(p[0] for p in parts if p).lower()

    id_tail = re.sub(r"[^A-Za-z0-9]", "", id_number or "")[-5:]

    base = f"{initials}{id_tail}yt"
    username = f"{base}{_icris_username_random_suffix(rand_len)}"
    if retry:
        username = f"{base}{_icris_username_random_suffix(rand_len)}"
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
    # 纯中文名 → 拼音只用于生成用户名，不写入 name_en（S03 不应填英文姓/名）
    username_en = person_en
    if not username_en and person_cn:
        try:
            from pypinyin import lazy_pinyin
            pinyins = lazy_pinyin(person_cn)
            if pinyins:
                family = pinyins[0].capitalize()
                given = "".join(pinyins[1:]).capitalize() if len(pinyins) > 1 else ""
                username_en = f"{family} {given}".strip()
        except Exception:
            pass
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
        icris_username, icris_password = _generate_icris_credentials(
            person_en=username_en, id_number=_get_val(materials, "id_number")
        )
    else:
        icris_username = ""
        icris_password = ""

    default_office = _get_default_office()
    office_flat = (_get_val(materials, "office_flat_floor") or default_office.get("flat_floor", "")).strip()
    office_building = (_get_val(materials, "office_building") or default_office.get("building", "")).strip()
    office_street = (_get_val(materials, "office_street") or default_office.get("street", "")).strip()
    office_district = (_get_val(materials, "office_district") or default_office.get("district", "")).strip()

    data: dict[str, Any] = {
        "company_name_en": _get_val(materials, "company_name_en"),
        "company_name_cn": _get_val(materials, "company_name_cn"),
        "company_type": "private_limited_by_shares",
        "registered_office": {
            "flat_floor": office_flat,
            "building": office_building,
            "street": office_street,
            "street_cn": office_cn,
            "street_en": office_street,
            "district": office_district,
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
                    "name_en": person_en,
                    "name_cn": person_cn,
                    "address_cn": _get_val(materials, "director_address_cn"),
                    "address_en": _get_val(materials, "director_address_en"),
                    "id_type": _get_val(materials, "id_type"),
                    "id_number": _get_val(materials, "id_number"),
                    "issuing_country": _get_val(materials, "issuing_country"),
                    "raw": True,
                }
            ]
            if person or person_cn or person_en
            else []
        ),
        "directors": (
            [
                {
                    "name_en": person_en,
                    "name_cn": person_cn,
                    "email": contact_email,
                    "address_cn": _get_val(materials, "director_address_cn"),
                    "address_en": _get_val(materials, "director_address_en"),
                    "id_type": _get_val(materials, "id_type"),
                    "id_number": _get_val(materials, "id_number"),
                    "issuing_country": _get_val(materials, "issuing_country"),
                    "raw": True,
                }
            ]
            if person or person_cn or person_en
            else []
        ),
        "company_secretary": {
            "type": "body_corporate",
            "hk_registered": True,
            "br_number": default_office.get("secretary_br_no", ""),
            "license_number": default_office.get("secretary_license_no", ""),
            "company_number": default_office.get("secretary_company_no", ""),
            "name_en": secretary if secretary else "",
            "name_cn": "",
            "raw": True,
        },
        "business_nature_desc": _get_val(materials, "business_desc"),
        "br_certificate_years": br_int,
        "applicant": {
            "name_en": person_en or am_en,
            "name_cn": person_cn or am_cn,
            "email": _get_val(materials, "applicant_email") or contact_email,
            "phone": _get_val(materials, "applicant_phone") or contact_phone,
            "id_type": _get_val(materials, "id_type"),
            "id_number": _get_val(materials, "id_number"),
            "issuing_country": _get_val(materials, "issuing_country"),
        },
        "identity_proof": {
            "id_type": _get_val(materials, "id_type") or "PRC_ID",
            "id_number": _get_val(materials, "id_number") or "",
            "issuing_country": _get_val(materials, "issuing_country"),
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
