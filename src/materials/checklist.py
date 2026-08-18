"""材料清单字段定义（对齐 material_checklist.md / company_registration.json）"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

@dataclass(frozen=True)
class MaterialField:
    key: str
    label: str
    required: bool = True
    field_type: str = "text"  # text | file


# 对客收集字段（单董事兼股东）；秘书/联络默认由配置注入，不对客必填
# 身份证明正面/反面必填性由 _is_field_required 按 id_type 动态判定
MATERIAL_FIELDS: list[MaterialField] = [
    MaterialField("company_name_cn", "公司中文名"),
    MaterialField("company_name_en", "公司英文名"),
    MaterialField("registered_capital", "注册资本", required=False),
    MaterialField("business_desc", "经营范围"),
    MaterialField("registered_office_cn", "注册地址（中文）"),
    MaterialField("registered_office_en", "注册地址（英文）"),
    MaterialField("registered_office", "公司注册地址（香港）", required=False),  # 旧键兼容
    MaterialField("director_name", "董事兼股东姓名"),
    MaterialField("director_name_cn", "董事中文名", required=False),
    MaterialField("director_name_en", "董事英文名", required=False),
    MaterialField("directors", "董事资料", required=False),  # 旧键兼容
    MaterialField("founder_members", "股东/创办成员资料", required=False),  # 旧键兼容
    MaterialField("id_number", "身份证号码"),
    MaterialField("director_address_cn", "住址中文"),
    MaterialField("director_address_en", "住址英文"),
    MaterialField("contact_email", "邮箱", required=False),
    MaterialField("contact_phone", "香港联络电话", required=False),
    MaterialField("company_secretary", "公司秘书资料", required=False),
    MaterialField("br_certificate_years", "商业登记证有效期（1或3年）", required=False),
    MaterialField("applicant_name", "ICRIS 申请人姓名", required=False),
    MaterialField("applicant_email", "ICRIS 申请人电邮", required=False),
    MaterialField("applicant_phone", "ICRIS 申请人电话", required=False),
    MaterialField("id_type", "身份证明类型（HKID/PRC_ID/PASSPORT）", required=False),
    MaterialField("issuing_country", "证件签发地（CHN/HKG/TWN/OTHER）", required=False),
    MaterialField("id_card_front", "身份证正面", field_type="file", required=False),
    MaterialField("id_card_back", "身份证反面", field_type="file", required=False),
    MaterialField("id_card_handheld", "手持身份证明照片", field_type="file", required=False),
    MaterialField("address_proof", "地址证明", field_type="file", required=False),
    MaterialField("passport", "护照复印件", field_type="file", required=False),
    MaterialField("taiwan_id", "台湾身份证（住址用）", field_type="file", required=False),
]

FILE_FIELD_KEYS = {f.key for f in MATERIAL_FIELDS if f.field_type == "file"}
REQUIRED_FIELD_KEYS = {f.key for f in MATERIAL_FIELDS if f.required}

_SENSITIVE_KEYS = frozenset(
    {
        "id_number",
        "contact_email",
        "applicant_email",
        "contact_phone",
        "applicant_phone",
    }
)

KNOWLEDGE_CHECKLIST_PATH = PROJECT_ROOT / "templates" / "material_checklist.md"


def mask_value_preview(key: str, value: str, *, channel: str = "kf") -> str:
    """生成对客展示值；群聊不回显敏感字段明文。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    # 外部群：敏感项只显示已填写
    if channel == "group" and key in _SENSITIVE_KEYS:
        return "已填写"
    if key == "id_number":
        return _mask_id_number(raw)
    if key in ("contact_email", "applicant_email"):
        return _mask_email(raw) if channel == "group" else _truncate(raw, 40)
    if key in ("contact_phone", "applicant_phone"):
        return _mask_phone(raw) if channel == "group" else _truncate(raw, 40)
    return _truncate(raw, 40)


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _mask_id_number(s: str) -> str:
    t = re.sub(r"\s+", "", s)
    if len(t) <= 4:
        return "*" * len(t)
    if len(t) <= 8:
        return t[:1] + "*" * (len(t) - 2) + t[-1:]
    return t[:2] + "*" * (len(t) - 4) + t[-2:]


def _mask_email(s: str) -> str:
    if "@" not in s:
        return "已填写"
    name, _, domain = s.partition("@")
    if not name:
        return "***@" + domain
    return name[0] + "***@" + domain


def _mask_phone(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return "已填写"
    return digits[:3] + "****" + digits[-2:]


def _field_value(materials: dict[str, dict[str, Any]], key: str) -> str:
    """从 materials 行 dict 中取字段值（优化 11 辅助）。"""
    row = materials.get(key) or {}
    return str(row.get("field_value") or "").strip()


def _effective_text(materials: dict[str, dict[str, Any]], key: str) -> str:
    """解析字段有效值（含旧键兼容）。"""
    if key == "director_name":
        return (
            _field_value(materials, "director_name")
            or _field_value(materials, "directors")
            or _field_value(materials, "founder_members")
        )
    if key == "registered_office_cn":
        return _field_value(materials, "registered_office_cn") or _field_value(
            materials, "registered_office"
        )
    if key == "registered_office_en":
        return _field_value(materials, "registered_office_en")
    return _field_value(materials, key)


def _row_has_value(materials: dict[str, dict[str, Any]], field: MaterialField) -> bool:
    row = materials.get(field.key) or {}
    if field.field_type == "file":
        return bool(row.get("file_path") or row.get("field_value"))
    if field.key in ("director_name", "registered_office_cn", "registered_office_en"):
        return bool(_effective_text(materials, field.key))
    return bool(row.get("field_value") or row.get("file_path"))


def _is_field_required(field: MaterialField, materials: dict[str, dict[str, Any]]) -> bool:
    """动态判定字段是否必填。

    身份证明（本期）：
      - 内地/香港身份证（含未提供类型，默认按身份证）：正面 + 反面必收；
      - 手持照可选；
      - 护照：仅护照页。
    """
    id_type = _field_value(materials, "id_type").upper()
    issuing = _field_value(materials, "issuing_country").upper()
    if field.key in ("id_card_front", "id_card_back"):
        return id_type in ("", "PRC_ID", "HKID")
    if field.key == "id_card_handheld":
        return False
    if field.key == "passport":
        return id_type == "PASSPORT"
    if field.key == "taiwan_id":
        # 台湾护照需另传台湾身份证以取得住址
        return id_type == "PASSPORT" and issuing in ("TWN", "TW", "TAIWAN", "ROC")
    if field.key == "director_address_cn" and id_type == "PASSPORT" and issuing in (
        "TWN",
        "TW",
        "TAIWAN",
        "ROC",
    ):
        return True
    return field.required


def progress_summary(materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根据 group_materials 计算进度。

    needs_review 有值时不进 missing_labels（对客「还需要」仅未收项），
    也不计入 complete；确认仍由 is_ready_for_confirm 拦截待复核。
    """
    received = 0
    received_labels: list[str] = []
    received_details: list[dict[str, str]] = []
    missing: list[str] = []
    needs_review: list[str] = []
    needs_review_details: list[dict[str, str]] = []
    for f in MATERIAL_FIELDS:
        # 旧键/内部字段不出现在对客进度明细（仍可入库）
        if f.key in (
            "registered_office",
            "directors",
            "founder_members",
            "company_secretary",
            "applicant_name",
            "applicant_email",
            "applicant_phone",
            "id_card_handheld",
        ):
            if not _is_field_required(f, materials):
                continue
        row = materials.get(f.key) or {}
        status = row.get("status", "missing")
        # 兼容：director_name 等可能落在旧键上
        has_value = _row_has_value(materials, f)
        if f.key == "director_name" and has_value and not (row.get("field_value") or "").strip():
            # 值在 directors/founder_members：按已收展示
            status = "received"
            for alt in ("directors", "founder_members"):
                alt_row = materials.get(alt) or {}
                if alt_row.get("status") in ("received", "confirmed", "needs_review"):
                    status = alt_row.get("status") or status
                    break
        if f.key == "registered_office_cn" and has_value and not (
            (materials.get("registered_office_cn") or {}).get("field_value") or ""
        ).strip():
            status = str(
                (materials.get("registered_office") or {}).get("status") or "received"
            )
        if status in ("received", "confirmed") and has_value:
            received += 1
            received_labels.append(f.label)
            if f.field_type == "file":
                preview = "已上传"
            else:
                preview = _effective_text(materials, f.key)
            received_details.append(
                {"key": f.key, "label": f.label, "value_preview": preview}
            )
        elif status == "needs_review" and has_value:
            needs_review.append(f.label)
            if f.field_type == "file":
                preview = "已上传（待复核）"
            else:
                preview = _effective_text(materials, f.key)
            needs_review_details.append(
                {"key": f.key, "label": f.label, "value_preview": preview}
            )
            # 待复核单独列出；不进 missing，避免对客「还需要：…（待复核）」
        elif _is_field_required(f, materials):
            missing.append(f.label)
    total_required = sum(1 for f in MATERIAL_FIELDS if _is_field_required(f, materials))
    return {
        "received": received,
        "total": len(MATERIAL_FIELDS),
        "total_required": total_required,
        "received_labels": received_labels,
        "received_details": received_details,
        "missing_labels": missing,
        "needs_review_labels": needs_review,
        "needs_review_details": needs_review_details,
        "complete": len(missing) == 0 and len(needs_review) == 0,
        "cross_field_issues": validate_cross_fields(materials),
    }


def validate_cross_fields(
    materials: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    """跨字段一致性校验（优化 11）。

    返回问题列表，每项 {"level": "error"|"warning", "field": key, "message": "..."}。
    error 级问题应阻止进入 REVIEW；warning 级仅提示。
    """
    issues: list[dict[str, str]] = []
    id_type = _field_value(materials, "id_type").upper()
    id_number = _field_value(materials, "id_number")

    # 规则 1（error）：证件类型与号码格式一致性
    if id_type and id_number:
        if id_type == "HKID" and not re.match(r"^[A-Z]{1,2}\d{6}\(?[\dA]\)?$", id_number):
            issues.append({
                "level": "error", "field": "id_number",
                "message": "香港身份证号码格式不符（应为 1-2 位字母+6 位数字+校验位）",
            })
        elif id_type == "PRC_ID" and not re.match(r"^\d{17}[\dXx]$", id_number):
            issues.append({
                "level": "error", "field": "id_number",
                "message": "内地身份证号码应为 18 位",
            })
        elif id_type == "PASSPORT" and not re.match(r"^[A-Z0-9]{5,15}$", id_number):
            issues.append({
                "level": "error", "field": "id_number",
                "message": "护照号码格式不符（应为 5-15 位字母或数字）",
            })

    # 规则 2（warning）：联系邮箱与申请人邮箱域名差异（仅个人邮箱域）
    contact_email = _field_value(materials, "contact_email")
    applicant_email = _field_value(materials, "applicant_email")
    personal_domains = {
        "gmail.com", "qq.com", "163.com", "outlook.com",
        "hotmail.com", "foxmail.com", "sina.com",
    }
    if (
        contact_email
        and applicant_email
        and "@" in contact_email
        and "@" in applicant_email
    ):
        c_domain = contact_email.split("@", 1)[1].lower()
        a_domain = applicant_email.split("@", 1)[1].lower()
        if (
            c_domain != a_domain
            and c_domain in personal_domains
            and a_domain in personal_domains
        ):
            issues.append({
                "level": "warning", "field": "applicant_email",
                "message": "联系邮箱与申请人邮箱域名不一致，请确认是否同一主体",
            })

    # 规则 3（warning）：电话区号与证件类型一致性
    contact_phone = _field_value(materials, "contact_phone")
    applicant_phone = _field_value(materials, "applicant_phone")
    phone = contact_phone or applicant_phone
    if id_type and phone:
        is_hk_phone = phone.startswith("+852") or phone.startswith("852")
        is_cn_phone = phone.startswith("+86") or bool(
            re.match(r"^1[3-9]\d{9}$", phone.lstrip("+"))
        )
        if id_type == "HKID" and is_cn_phone and not is_hk_phone:
            issues.append({
                "level": "warning", "field": "contact_phone",
                "message": "证件为香港身份证但电话为内地号码，请确认",
            })
        elif id_type == "PRC_ID" and is_hk_phone and not is_cn_phone:
            issues.append({
                "level": "warning", "field": "contact_phone",
                "message": "证件为内地身份证但电话为香港号码，请确认",
            })

    # 规则 4（error）：商业登记证年限应为 1 或 3
    br_years = _field_value(materials, "br_certificate_years")
    if br_years and br_years not in ("1", "3"):
        issues.append({
            "level": "error", "field": "br_certificate_years",
            "message": "商业登记证年限应为 1 年或 3 年",
        })

    # 规则 5（error）：证件识别与填写的姓名/号码不一致
    verify = _field_value(materials, "id_verify_status").lower()
    if verify == "mismatch":
        detail = _field_value(materials, "id_verify_detail") or "证件识别信息与所填姓名/号码不一致"
        issues.append({
            "level": "error",
            "field": "id_number",
            "message": detail,
        })

    return issues


# === 优化 12：智能缺失材料提醒 ===
# 必填字段优先级（数字越小越优先提醒）；按收窄后的必填集排序。
FIELD_PRIORITY: dict[str, int] = {
    "company_name_cn": 1,
    "company_name_en": 2,
    "registered_office_cn": 3,
    "registered_office_en": 4,
    "director_name": 5,
    "id_number": 6,
    "director_address_cn": 7,
    "director_address_en": 8,
    "business_desc": 9,
    "registered_capital": 10,
    "id_card_front": 11,
    "id_card_back": 12,
    "passport": 13,
    "taiwan_id": 14,
}
CRITICAL_FIELD_KEYS = {
    "company_name_en",
    "company_name_cn",
    "registered_office_cn",
    "director_name",
}


def prioritized_missing(
    materials: dict[str, dict[str, Any]], *, limit: int = 3
) -> list[dict[str, str]]:
    """按优先级返回缺失的必填字段（优化 12）。

    返回列表，每项 {"key": ..., "label": ..., "critical": bool}，按优先级升序。
    """
    missing: list[tuple[int, str, str, bool]] = []
    for f in MATERIAL_FIELDS:
        if not _is_field_required(f, materials):
            continue
        row = materials.get(f.key) or {}
        status = row.get("status", "missing")
        has_value = _row_has_value(materials, f)
        if status in ("received", "confirmed") and has_value:
            continue
        if status == "needs_review" and has_value:
            # 已收待复核：不进「尚未收到」提醒（与 progress_summary 一致）
            continue
        if has_value and f.key in ("director_name", "registered_office_cn"):
            # 旧键已填则不算缺失
            continue
        priority = FIELD_PRIORITY.get(f.key, 99)
        critical = f.key in CRITICAL_FIELD_KEYS
        missing.append((priority, f.key, f.label, critical))
    missing.sort(key=lambda x: (x[0], x[2]))
    return [
        {"key": k, "label": lbl, "critical": crit}
        for _, k, lbl, crit in missing[:limit]
    ]


def format_materials_snapshot(materials: dict[str, dict[str, Any]], *, max_chars: int = 1200) -> str:
    """供 QA SessionContext 注入的材料快照（已收+待收）。"""
    p = progress_summary(materials)
    parts: list[str] = [
        f"进度 {p['received']}/{p['total']}（必填剩余 {len(p['missing_labels'])}）",
    ]
    details = p.get("received_details") or []
    if details:
        bits = []
        for d in details[:12]:
            prev = mask_value_preview(d["key"], d.get("value_preview") or "", channel="kf")
            if prev and prev not in ("已上传",):
                bits.append(f"{d['label']}={prev}")
            else:
                bits.append(d["label"] + ("=已上传" if prev == "已上传" else ""))
        parts.append("已收集: " + "、".join(bits))
        if len(details) > 12:
            parts.append(f"等共{len(details)}项已收")
    else:
        parts.append("已收集: （尚未收到）")
    missing = p.get("missing_labels") or []
    if missing:
        parts.append("还需要: " + "、".join(missing[:16]))
        if len(missing) > 16:
            parts.append(f"等共{len(missing)}项待补")
    review = p.get("needs_review_labels") or []
    if review:
        parts.append("待复核: " + "、".join(review[:8]))
    id_type = str((materials.get("id_type") or {}).get("field_value") or "").strip()
    id_number = str((materials.get("id_number") or {}).get("field_value") or "").strip()
    if id_type:
        parts.append(f"证件类型={id_type}")
    if id_number:
        parts.append(f"证件号码={mask_value_preview('id_number', id_number, channel='kf')}")
    text = "; ".join(parts)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _channel_header(channel: str) -> str:
    if channel == "kf":
        return "当前会话：微信客服私聊"
    return "当前会话：客户群"


def _linked_channel_hint(linked_hint: str = "") -> str:
    return (linked_hint or "").strip()


def _format_received_lines(
    details: list[dict[str, str]],
    *,
    channel: str,
    limit: int = 20,
) -> list[str]:
    lines: list[str] = []
    for d in details[:limit]:
        key = d.get("key") or ""
        label = d.get("label") or key
        raw_prev = d.get("value_preview") or ""
        if raw_prev == "已上传" or raw_prev.startswith("已上传"):
            lines.append(f"  - {label}：已上传")
            continue
        preview = mask_value_preview(key, raw_prev, channel=channel)
        if preview:
            lines.append(f"  - {label}：{preview}")
        else:
            lines.append(f"  - {label}")
    if len(details) > limit:
        lines.append(f"  … 等共 {len(details)} 项")
    return lines


def format_received_text(
    materials: dict[str, dict[str, Any]],
    *,
    channel: str = "kf",
    session_header: bool = True,
    linked_hint: str = "",
    status: str = "",
) -> str:
    p = progress_summary(materials)
    lines: list[str] = []
    if session_header:
        lines.append(_channel_header(channel))
    lines.append("根据您当前会话，已收集到：")
    details = p.get("received_details") or []
    if details:
        lines.extend(_format_received_lines(details, channel=channel))
    else:
        lines.append("  （尚未收到资料）")
    review = p.get("needs_review_details") or []
    if review:
        lines.append("待人工复核（不算已齐）：")
        lines.extend(_format_received_lines(review, channel=channel, limit=8))
    if not details and not review:
        lines.append("可发送「/资料」查看完整清单，或按「键=值」提交。")
    hint = _linked_channel_hint(linked_hint)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def format_missing_text(
    materials: dict[str, dict[str, Any]],
    *,
    channel: str = "kf",
    session_header: bool = True,
    linked_hint: str = "",
    status: str = "",
) -> str:
    p = progress_summary(materials)
    lines: list[str] = []
    if session_header:
        lines.append(_channel_header(channel))
    lines.append("根据您当前会话，还需要：")
    if p["missing_labels"]:
        lines.extend(f"  - {lbl}" for lbl in p["missing_labels"][:12])
        if len(p["missing_labels"]) > 12:
            lines.append(f"  … 等共 {len(p['missing_labels'])} 项")
        lines.append("请按上表继续补充；补齐后我会提醒您开始注册。")
    else:
        lines.append("  （必填项已齐）")
        if p.get("needs_review_labels"):
            lines.append("但仍有待复核项，暂不可确认注册。")
        else:
            lines.append("请回复「确认」或「开始注册」进入注册流程。")
    if status == "REVIEW" and p.get("complete"):
        lines.append("资料已齐，可回复「确认」或「开始注册」。")
    hint = _linked_channel_hint(linked_hint)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def format_knowledge_checklist_text() -> str:
    """与 /资料 同源：读取 templates/material_checklist.md。"""
    path: Path = KNOWLEDGE_CHECKLIST_PATH
    try:
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return content
    except OSError:
        pass
    # 回退：由字段定义生成简版
    lines = ["# 香港公司注册材料清单", "", "以下为注册所需资料："]
    for f in MATERIAL_FIELDS:
        mark = "（必填）" if f.required else "（可选）"
        kind = "文件" if f.field_type == "file" else "文本"
        lines.append(f"- {f.label}{mark} [{kind}]")
    lines.append("")
    lines.append("发送「/进度」可查看本会话已收/待收。")
    return "\n".join(lines)


def format_case_status_text(
    *,
    group_status: str = "",
    job: dict[str, Any] | None = None,
    materials: dict[str, dict[str, Any]] | None = None,
    channel: str = "kf",
    linked_hint: str = "",
) -> str:
    """办理状态独立话术；可附一行材料摘要。"""
    lines = [_channel_header(channel)]
    gst = (group_status or "").strip()
    p = progress_summary(materials or {})
    summary_line = (
        f"材料进度：{p['received']}/{p['total']}，必填剩余 {len(p['missing_labels'])} 项"
    )
    if p.get("needs_review_labels"):
        summary_line += f"；待复核 {len(p['needs_review_labels'])} 项"

    if job:
        jid = job.get("id")
        jst = str(job.get("status") or "")
        if jst in ("pending", "running") or gst in ("QUEUED", "HANDOFF", "CONFIRMED"):
            lines.append(f"您的注册任务 #{jid} 正在办理中（{jst or gst}），请稍候。")
            lines.append("预计需排队处理，办结前可继续咨询业务问题；需要人工请回复「转人工」。")
        elif jst == "failed" or gst == "FAILED":
            lines.append(f"注册任务 #{jid} 未成功完成。")
            lines.append("可回复「重新办理」再次排队，或继续咨询业务问题 /「转人工」。")
        else:
            lines.append(f"注册任务 #{jid} 状态：{jst or '未知'}。")
        lines.append(summary_line)
    elif gst in ("QUEUED", "HANDOFF", "CONFIRMED"):
        lines.append(f"您的注册正在办理中（会话 {gst}），请稍候。")
        lines.append("可继续咨询业务问题；需要人工请回复「转人工」。")
        lines.append(summary_line)
    elif gst == "FAILED":
        lines.append("当前自动办理未完成。")
        lines.append("可回复「重新办理」再次排队，或继续咨询 /「转人工」。")
        lines.append(summary_line)
    elif gst == "HUMAN":
        lines.append("您的会话已转接人工专员，老师会尽快回复。")
        lines.append(summary_line)
        lines.append("如需查看材料明细，可再说「进度」或「还缺哪些」。")
    elif gst == "REVIEW" and p.get("complete"):
        lines.append("材料已收齐，等待您确认。")
        lines.append("请回复「确认」或「开始注册」进入注册流程。")
        lines.append(summary_line)
    else:
        lines.append("尚未提交注册，当前在收集资料。")
        lines.append(summary_line)
        if p["missing_labels"]:
            lines.append("还需要：" + "、".join(p["missing_labels"][:6]))
            if len(p["missing_labels"]) > 6:
                lines.append(f"等共 {len(p['missing_labels'])} 项")

    hint = _linked_channel_hint(linked_hint)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def format_progress_text(
    materials: dict[str, dict[str, Any]],
    *,
    mode: str = "full_progress",
    channel: str = "kf",
    linked_hint: str = "",
    status: str = "",
) -> str:
    """客户可见进度；mode=received|missing|full_progress。"""
    if mode == "received":
        return format_received_text(
            materials,
            channel=channel,
            linked_hint=linked_hint,
            status=status,
        )
    if mode == "missing":
        return format_missing_text(
            materials,
            channel=channel,
            linked_hint=linked_hint,
            status=status,
        )
    if mode == "knowledge_checklist":
        return format_knowledge_checklist_text()

    p = progress_summary(materials)
    lines = [_channel_header(channel), "根据您当前会话的材料："]
    lines.append(f"材料收集进度：{p['received']}/{p['total']} 项")
    lines.append(f"必填项剩余：{len(p['missing_labels'])} 项")

    details = p.get("received_details") or []
    if details:
        lines.append("已收集：")
        lines.extend(_format_received_lines(details, channel=channel))
    else:
        lines.append("已收集：（尚未收到资料）")
        lines.append("可发送「/资料」查看完整清单，或按「键=值」提交、上传证件图片。")

    if p.get("needs_review_labels"):
        lines.append("待人工复核（不可直接确认注册）：")
        review_details = p.get("needs_review_details") or [
            {"key": "", "label": lbl, "value_preview": ""}
            for lbl in p["needs_review_labels"]
        ]
        lines.extend(_format_received_lines(review_details, channel=channel, limit=8))

    if p["missing_labels"]:
        lines.append("还需要：")
        lines.extend(f"  - {lbl}" for lbl in p["missing_labels"][:12])
        if len(p["missing_labels"]) > 12:
            lines.append(f"  … 等共 {len(p['missing_labels'])} 项")
        if p.get("needs_review_labels") and "收齐" in (mode or ""):
            pass
        lines.append("请按上表继续补充，补齐后我会提醒您开始注册。")
        if p.get("needs_review_labels"):
            lines.append("说明：存在待复核项时视为未齐，暂不可确认注册。")
    else:
        if p.get("needs_review_labels"):
            lines.append("必填项表面已填，但仍有待复核项，暂不可确认注册。")
        else:
            lines.append(
                "必填资料已齐全。请回复「确认」或「开始注册」进入注册流程。"
            )

    if status == "REVIEW" and p.get("complete"):
        lines.append("当前为确认阶段，可直接回复「确认」或「开始注册」。")

    # 跨字段校验问题（优化 11）
    cross_issues = p.get("cross_field_issues") or []
    if cross_issues:
        errors = [i for i in cross_issues if i.get("level") == "error"]
        warnings = [i for i in cross_issues if i.get("level") == "warning"]
        if errors:
            lines.append("⚠️ 校验错误（需修正后才能注册）：")
            for i in errors:
                lines.append(f"  ✗ {i.get('message')}")
        if warnings:
            lines.append("提醒（请确认）：")
            for i in warnings:
                lines.append(f"  · {i.get('message')}")

    hint = _linked_channel_hint(linked_hint)
    if hint:
        lines.append(hint)
    return "\n".join(lines)
