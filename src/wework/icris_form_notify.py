"""NNC1 初步检查结果：发到审核群（Webhook markdown + 截图）。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FONT_TAG_RE = re.compile(r"</?font(?:\s[^>]*)?>", re.I)


def _md_status(text: str, *, passed: bool) -> str:
    color = "info" if passed else "warning"
    return f'<font color="{color}">{text}</font>'


def strip_prelim_markdown(text: str) -> str:
    """去掉企微 markdown 的 font 标签，供纯文本回退。"""
    return _FONT_TAG_RE.sub("", text or "")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _person_name(person: dict[str, Any]) -> str:
    cn = str(person.get("name_cn") or "").strip()
    en = str(person.get("name_en") or "").strip()
    if cn or en:
        return cn or en
    parts = [
        str(person.get("surname_en") or "").strip(),
        str(person.get("given_en") or "").strip(),
    ]
    return " ".join(p for p in parts if p).strip()


def _first_person(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("directors", "founder_members"):
        items = payload.get(key)
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return first
    applicant = payload.get("applicant")
    if isinstance(applicant, dict):
        return applicant
    return {}


def fields_from_payload(payload: dict[str, Any] | None) -> dict[str, str]:
    """从 job payload / company_data 抽出群通知字段。证件号不打码。"""
    data = payload if isinstance(payload, dict) else {}
    person = _first_person(data)
    identity = _as_dict(data.get("identity_proof"))
    applicant = _as_dict(data.get("applicant"))
    acct = _as_dict(data.get("icris_account"))
    id_type = str(
        person.get("id_type")
        or identity.get("id_type")
        or applicant.get("id_type")
        or ""
    ).strip()
    id_number = str(
        person.get("id_number")
        or identity.get("id_number")
        or applicant.get("id_number")
        or ""
    ).strip()
    return {
        "company_name_cn": str(data.get("company_name_cn") or "").strip(),
        "company_name_en": str(data.get("company_name_en") or "").strip(),
        "shareholder_name": _person_name(person) or _person_name(applicant),
        "id_type": id_type,
        "id_number": id_number,
        "icris_username": str(acct.get("username") or "").strip(),
    }


def format_prelim_message(
    job_id: int,
    fields: dict[str, str] | None = None,
    *,
    passed: bool,
    reasons: str = "",
    nnc1_duration: str = "",
) -> str:
    """初步检查群通知正文。通过不含拒絕原因；拒絕追加页面抓到的全文。"""
    f = fields if isinstance(fields, dict) else {}
    title = _md_status(
        "【NNC1 初步检查通过】" if passed else "【NNC1 初步检查拒絕】",
        passed=passed,
    )
    result = _md_status("通过" if passed else "拒絕", passed=passed)
    duration = str(nnc1_duration or "").strip()
    lines = [
        title,
        f"任务 #{int(job_id or 0)}",
    ]
    if duration:
        lines.append(f"填表耗时：{duration}")
    lines.extend(
        [
            f"公司中文名: {f.get('company_name_cn') or ''}",
            f"公司英文名: {f.get('company_name_en') or ''}",
            f"股东姓名: {f.get('shareholder_name') or ''}",
            f"证件类型: {f.get('id_type') or ''}",
            f"证件号码: {f.get('id_number') or ''}",
            f"ICRIS账号: {f.get('icris_username') or ''}",
            f"初步检查结果: {result}",
        ]
    )
    if not passed:
        reasons_text = (reasons or "").strip()
        if reasons_text:
            lines.append(f"拒絕原因:\n{reasons_text}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    return text


def send_prelim_notify(
    job_id: int,
    payload: dict[str, Any] | None = None,
    *,
    passed: bool,
    reasons: str = "",
    screenshot_path: str = "",
    nnc1_duration: str = "",
    client: Any = None,
) -> bool:
    """发审核群：先 markdown 文字，再 webhook 截图。无通道则直接返回。"""
    from config.settings import settings
    from src.wework.client import WeWorkClient

    webhook_url = (settings.icris_review_webhook_url or "").strip()
    chat_id = (settings.icris_review_notify_chat_id or "").strip()
    if not webhook_url and not chat_id:
        logger.info("初步检查通知未配置 webhook/chat_id，跳过")
        return False

    msg = format_prelim_message(
        job_id,
        fields_from_payload(payload),
        passed=passed,
        reasons=reasons,
        nnc1_duration=nnc1_duration,
    )
    ww = client or WeWorkClient()
    sent = False
    if webhook_url:
        try:
            ww.send_webhook_markdown(webhook_url, msg)
            sent = True
        except Exception as e:
            logger.warning("初步检查通知 Webhook 发送失败，回退应用消息: %s", e)
        else:
            shot = (screenshot_path or "").strip()
            if shot and Path(shot).is_file():
                try:
                    ww.send_webhook_image(webhook_url, shot)
                except Exception as e:
                    logger.warning("初步检查截图 Webhook 发送失败: %s", e)
            return True
    if not sent and chat_id:
        try:
            ww.send_group_text(chat_id, strip_prelim_markdown(msg))
            return True
        except Exception as e:
            logger.warning("初步检查通知应用消息发送失败: %s", e)
            return False
    return False
