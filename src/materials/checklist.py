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
    MaterialField("id_type", "身份证明类型（HKID/PRC_ID/PASSPORT）", required=False),
    MaterialField("id_number", "身份证明号码", required=False),
    MaterialField("id_card_front", "身份证明（正面）", field_type="file"),
    MaterialField("id_card_back", "身份证明（反面）", field_type="file", required=False),
    MaterialField("address_proof", "地址证明", field_type="file", required=False),
    MaterialField("passport", "护照复印件", field_type="file", required=False),
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


def progress_summary(materials: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根据 group_materials 计算进度。needs_review 不计入齐全。"""
    received = 0
    received_labels: list[str] = []
    received_details: list[dict[str, str]] = []
    missing: list[str] = []
    needs_review: list[str] = []
    needs_review_details: list[dict[str, str]] = []
    for f in MATERIAL_FIELDS:
        row = materials.get(f.key) or {}
        status = row.get("status", "missing")
        has_value = bool(row.get("field_value") or row.get("file_path"))
        if status in ("received", "confirmed") and has_value:
            received += 1
            received_labels.append(f.label)
            if f.field_type == "file":
                preview = "已上传"
            else:
                preview = str(row.get("field_value") or "").strip()
            received_details.append(
                {"key": f.key, "label": f.label, "value_preview": preview}
            )
        elif status == "needs_review" and has_value:
            needs_review.append(f.label)
            if f.field_type == "file":
                preview = "已上传（待复核）"
            else:
                preview = str(row.get("field_value") or "").strip()
            needs_review_details.append(
                {"key": f.key, "label": f.label, "value_preview": preview}
            )
            if f.required:
                missing.append(f"{f.label}（待复核）")
        elif f.required:
            missing.append(f.label)
    total_required = len(REQUIRED_FIELD_KEYS)
    return {
        "received": received,
        "total": len(MATERIAL_FIELDS),
        "total_required": total_required,
        "received_labels": received_labels,
        "received_details": received_details,
        "missing_labels": missing,
        "needs_review_labels": needs_review,
        "needs_review_details": needs_review_details,
        "complete": len(missing) == 0,
    }


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

    hint = _linked_channel_hint(linked_hint)
    if hint:
        lines.append(hint)
    return "\n".join(lines)
