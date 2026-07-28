"""解析飞书群内 ICRIS 账号注册填写模板"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = (
    "英文姓氏",
    "英文名字",
    "电邮",
    "确认电邮",
    "香港电话",
    "用户名",
    "密码",
    "身份证号码",
)

FIELD_ALIASES = {
    "英文姓": "英文姓氏",
    "英文名": "英文名字",
    "邮箱": "电邮",
    "电邮地址": "电邮",
    "确认邮箱": "确认电邮",
    "电话": "香港电话",
    "联络电话": "香港电话",
    "用户名称": "用户名",
    "登录名": "用户名",
    "身分证号码": "身份证号码",
    "身份证件号码": "身份证号码",
    "室/楼/座": "室楼座",
    "区/市/省": "区市省",
    "区市": "区市省",
    "照片路径": "身份证照片路径",
    "证件照路径": "身份证照片路径",
}


@dataclass
class ParseResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_fields: dict[str, str] = field(default_factory=dict)


def _normalize_key(key: str) -> str:
    key = (key or "").strip().lstrip("#").strip()
    key = re.sub(r"[\s　]+", "", key)
    return FIELD_ALIASES.get(key, key)


def extract_kv_fields(text: str) -> dict[str, str]:
    """从消息文本中提取 键=值 / 键：值"""
    fields: dict[str, str] = {}
    if not text:
        return fields
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("---") or line.startswith("【"):
            continue
        if line.startswith("说明") or line.startswith("请按"):
            continue
        m = re.match(r"^([^:=：]+)[=:：]\s*(.*)$", line)
        if not m:
            continue
        key = _normalize_key(m.group(1))
        val = m.group(2).strip()
        if key:
            fields[key] = val
    return fields


def _map_id_type(raw: str) -> tuple[str, str]:
    t = (raw or "中国身份证").strip()
    if re.search(r"香港|HKID", t, re.I):
        return "HKID", "香港身分證號碼"
    if re.search(r"护照|護照|passport", t, re.I):
        return "PASSPORT", "護照號碼"
    return "PRC_ID", "中華人民共和國身分證號碼"


def parse_icris_form(text: str) -> ParseResult:
    """解析模板文本 → company_data（applicant / identity_proof / icris_account）"""
    # 去掉命令行本身
    cleaned = re.sub(r"^/(开始注册|start|注册)\s*", "", text.strip(), flags=re.I | re.M)
    cleaned = re.sub(r"<at[^>]*>.*?</at>", "", cleaned, flags=re.I | re.S).strip()
    fields = extract_kv_fields(cleaned)

    missing = [k for k in REQUIRED_FIELDS if not (fields.get(k) or "").strip()]
    errors: list[str] = []

    email = fields.get("电邮", "").strip()
    email2 = fields.get("确认电邮", "").strip()
    if email and email2 and email.lower() != email2.lower():
        errors.append("电邮与确认电邮不一致")

    password = fields.get("密码", "").strip()
    if password:
        if len(password) < 10:
            errors.append("密码须至少 10 位")
        elif not password[0].isupper():
            errors.append("密码须以大写字母开头")
        elif not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
            errors.append("密码须同时包含字母和数字")

    photo = fields.get("身份证照片路径", "").strip()
    if photo and not Path(photo).is_file():
        errors.append(f"身份证照片路径不存在: {photo}")

    if missing or errors:
        return ParseResult(ok=False, missing=missing, errors=errors, raw_fields=fields)

    surname = fields.get("英文姓氏", "").strip()
    given = fields.get("英文名字", "").strip()
    name_en = f"{surname} {given}".strip()
    id_type, id_type_label = _map_id_type(fields.get("身份证类型", ""))
    id_number = fields.get("身份证号码", "").strip()
    phone = fields.get("香港电话", "").strip()
    username = fields.get("用户名", "").strip()
    district = fields.get("区市省", "香港仔").strip() or "香港仔"
    country = fields.get("国家", "中国").strip() or "中国"
    lang = fields.get("通讯语言", "English").strip() or "English"
    room = fields.get("室楼座", "").strip() or "8楼A室"
    building = fields.get("大厦", "").strip() or "快乐大厦"
    street = fields.get("街道", "").strip() or "中关村大街1号"
    region = fields.get("地区", "").strip() or f"{country} {district}"

    proof_method = fields.get("证明文件方式", "经核证真实副本").strip()
    online_method = "certified_copy"
    if re.search(r"数码|數碼|数字证书|數字證書", proof_method):
        online_method = "digital_cert"

    data: dict[str, Any] = {
        "applicant": {
            "title": "Mr",
            "name_en": name_en,
            "name_cn": "",  # 中文姓名不填
            "id_type": id_type,
            "id_number": id_number,
            "email": email,
            "phone": phone,
            "address": f"{room}, {building}, {street}, {region}",
            "address_cn": {
                "room": room,
                "building": building,
                "street": street,
                "region": region,
            },
            "district": district,
            "country": country,
            "correspondence_language": lang,
        },
        "identity_proof": {
            "id_type": id_type,
            "id_type_label": id_type_label,
            "id_number": id_number,
            "submission_method": "online",
            "submission_label": "網上提交",
            "online_document_method": online_method,
            "online_document_label": "身分證明文件的經核證真實副本",
            "document_files": [photo] if photo else [],
            "document_dir": str(Path(photo).parent) if photo else "",
        },
        "icris_account": {
            "username": username,
            "password": password,
        },
        "password_hint": fields.get("密码提示", "ICRIS account"),
        "security_question": fields.get("安全问题", "What is your password hint?"),
        "security_answer": fields.get("安全答案", "ICRIS"),
    }
    return ParseResult(ok=True, data=data, raw_fields=fields)


def save_runtime_data(chat_id: str, data: dict[str, Any]) -> Path:
    out_dir = PROJECT_ROOT / "data" / "runtime"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", chat_id or "unknown")[:64]
    path = out_dir / f"{safe}_icris.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已保存运行时资料: %s", path)
    return path
