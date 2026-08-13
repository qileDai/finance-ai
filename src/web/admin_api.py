"""管理后台 JSON API（/admin/api/*）。"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from config.settings import settings
from src.storage.db import ExternalGroupStore

logger = logging.getLogger(__name__)


def _ok(**payload: Any) -> tuple[dict[str, Any], int]:
    return {"ok": True, **payload}, 200


def _err(message: str, code: int = 400) -> tuple[dict[str, Any], int]:
    return {"ok": False, "error": message}, code


def _session_label(row: dict[str, Any]) -> str:
    name = row.get("name") or ""
    kfid = row.get("open_kfid") or ""
    if kfid:
        acc = settings.get_kf_account(str(kfid))
        if acc and acc.label:
            return f"{name} ({acc.label})" if name else acc.label
    return str(name)


def handle_admin_api(
    *,
    method: str,
    path: str,
    store: ExternalGroupStore,
    icris_worker: Any = None,
    body: dict | None = None,
) -> tuple[dict[str, Any], int] | None:
    """处理 /admin/api/*。非 API 路径返回 None。"""
    parsed = urlparse(path or "")
    raw = (parsed.path or "").rstrip("/") or "/"
    if not raw.startswith("/admin/api"):
        return None

    rel = raw[len("/admin/api") :].lstrip("/")  # e.g. overview, sessions/xxx
    method = (method or "GET").upper()
    query = parse_qs(parsed.query)

    try:
        if method == "GET" and rel == "overview":
            return _handle_overview(store, icris_worker)
        if method == "GET" and rel == "sessions":
            channel = (query.get("channel", ["all"])[0] or "all").strip().lower()
            return _handle_sessions_list(store, channel)
        if method == "GET" and rel.startswith("sessions/"):
            roomid = unquote(rel[len("sessions/") :])
            return _handle_session_detail(store, roomid)
        if method == "GET" and rel == "jobs":
            status = (query.get("status", [""])[0] or "").strip().lower()
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            return _handle_jobs_list(store, status=status, limit=limit)
        if method == "GET" and rel.startswith("jobs/"):
            mid = rel[len("jobs/") :]
            if "/" not in mid and mid.isdigit():
                return _handle_job_detail(store, int(mid))
            return _err("invalid job id", 400)
        if method == "POST" and rel.startswith("jobs/") and rel.endswith("/cancel"):
            job_id = _parse_job_id(rel, suffix="/cancel")
            if job_id is None:
                return _err("invalid job id", 400)
            return _handle_job_cancel(store, job_id)
        if method == "POST" and rel.startswith("jobs/") and rel.endswith("/requeue"):
            job_id = _parse_job_id(rel, suffix="/requeue")
            if job_id is None:
                return _err("invalid job id", 400)
            return _handle_job_requeue(store, job_id)
        if method == "GET" and rel == "quality":
            try:
                hours = float(query.get("hours", ["24"])[0])
            except ValueError:
                hours = 24.0
            return _handle_quality(store, hours=hours)
        if method == "GET" and rel == "register-runner/defaults":
            from src.web.admin_runner import defaults as runner_defaults

            return _ok(**runner_defaults())
        if method == "GET" and rel == "register-runner/status":
            from src.web.admin_runner import status as runner_status

            return _ok(**runner_status())
        if method == "POST" and rel == "register-runner/submit":
            return _handle_runner_submit(body)
        if method == "GET" and rel == "wework/send-modes":
            return _handle_wework_send_modes()
        if method == "POST" and rel == "wework/send":
            return _handle_wework_send(body)
    except Exception as e:
        logger.exception("admin api error: %s", e)
        return _err(str(e) or "internal error", 500)

    return _err("not found", 404)


def _handle_wework_send_modes() -> tuple[dict[str, Any], int]:
    """返回当前外部群发送模式（决策结果）。"""
    return _ok(
        configured=settings.wework_configured,
        kf_configured=settings.wework_kf_configured,
        send_mode=settings.wework_external_send_mode_resolved,
        channel=settings.wework_channel_resolved,
        webhook_url_set=bool(
            (settings.wework_external_group_webhook_url or "").strip()
        ),
        default_owner_set=bool(
            (settings.wework_default_group_owner_userid or "").strip()
        ),
    )


def _handle_wework_send(body: dict | None) -> tuple[dict[str, Any], int]:
    """手动往外部群发消息（测试用）。

    body: {chat_id, content, to_external_userid?}
    """
    if not isinstance(body, dict):
        return _err("request body required", 400)
    chat_id = str(body.get("chat_id") or "").strip()
    content = str(body.get("content") or "").strip()
    to_external_userid = str(body.get("to_external_userid") or "").strip() or None
    if not chat_id:
        return _err("chat_id required", 400)
    if not content:
        return _err("content required", 400)
    if len(content.encode("utf-8")) > 2000:
        return _err("content too long (>2000 bytes)", 400)

    from src.wework.external_client import WeWorkExternalClient

    client = WeWorkExternalClient()
    if client._mock_mode:
        return _err(
            "当前为 mock 模式（未配置 WEWORK_CORP_ID/SECRET），无法真实发送；"
            "请配置 .env 后重启 admin",
            400,
        )
    try:
        # 用 describe_send_plan 先告知将走哪条通道
        plan = client.describe_send_plan(chat_id, to_external_userid=to_external_userid)
        result = client.send_group_text(
            chat_id,
            content,
            to_external_userid=to_external_userid,
        )
        errcode = int(result.get("errcode", 0) or 0)
        if errcode != 0:
            return _err(
                f"发送失败 errcode={errcode}: {result.get('errmsg', '')}",
                500,
            )
        return _ok(plan=plan, result=result)
    except Exception as e:
        logger.exception("wework send failed chat_id=%s", chat_id)
        return _err(str(e) or "send failed", 500)


def _handle_runner_submit(body: dict | None) -> tuple[dict[str, Any], int]:
    """快速注册：表单字段 + 证件文件(data_url) → 入队 registration_jobs。"""
    from src.web.admin_runner import submit as runner_submit

    if not isinstance(body, dict):
        return _err("request body required", 400)
    fields = body.get("fields") or {}
    files = body.get("files") or {}
    if not isinstance(fields, dict) or not isinstance(files, dict):
        return _err("fields/files must be objects", 400)
    # 缺省 dry_run=True；显式 false/0/"false" 才关闭
    raw_dry = body.get("dry_run", True)
    if isinstance(raw_dry, str):
        dry_run = raw_dry.strip().lower() not in ("0", "false", "no", "off")
    else:
        dry_run = bool(raw_dry)
    return runner_submit(fields, files, dry_run=dry_run)


def _parse_job_id(rel: str, *, suffix: str) -> int | None:
    mid = rel[len("jobs/") : -len(suffix)]
    try:
        return int(mid)
    except ValueError:
        return None


def _handle_overview(
    store: ExternalGroupStore, icris_worker: Any
) -> tuple[dict[str, Any], int]:
    try:
        conversation = store.conversation_quality_stats(hours=24.0)
    except Exception:
        conversation = {
            "hours": 24.0,
            "agent_runs_total": 0,
            "actions": {},
            "reply_rate": 0.0,
            "silent_rate": 0.0,
            "abstain_rate": 0.0,
            "human_transfer_rate": 0.0,
            "avg_confidence": 0.0,
            "low_confidence_count": 0,
            "inbox_unprocessed": 0,
            "send_failures": 0,
            "kf_sends": 0,
            "qa_latency_ms": {},
            "intent_routes": {},
        }
    try:
        registration = store.registration_job_stats(hours=24.0)
    except Exception:
        registration = {
            "counts": {},
            "pending_count": 0,
            "running_count": 0,
            "success_rate": 0.0,
            "window_counts": {},
            "recent_failures": [],
        }
    if icris_worker is not None and hasattr(icris_worker, "status_payload"):
        worker = icris_worker.status_payload()
    else:
        worker = {
            "enabled": bool(settings.icris_worker_enabled),
            "alive": False,
            "pending_count": registration.get("pending_count", 0),
            "running_job_id": registration.get("running_job_id"),
            "note": "ICRIS Worker 运行在 wework-external-bot 进程；此处仅反映队列 DB 状态",
        }
    return _ok(
        conversation=conversation,
        registration=registration,
        icris_worker=worker,
        hours=24.0,
    )


def _handle_sessions_list(
    store: ExternalGroupStore, channel: str
) -> tuple[dict[str, Any], int]:
    rows = store.list_all_materials_summary(channel=channel)
    items = []
    for r in rows:
        item = dict(r)
        item["label"] = _session_label(r)
        items.append(item)
    return _ok(items=items, channel=channel)


def _handle_session_detail(
    store: ExternalGroupStore, roomid: str
) -> tuple[dict[str, Any], int]:
    roomid = (roomid or "").strip()
    if not roomid:
        return _err("roomid required", 400)
    group = store.get_group(roomid)
    if not group:
        return _err("session not found", 404)
    materials = store.get_materials(roomid)
    material_items = []
    for row in materials.values():
        item = dict(row)
        fv = str(item.get("field_value") or "")
        item["value_text"] = fv
        material_items.append(item)
    material_items.sort(key=lambda x: str(x.get("field_key") or ""))
    out = dict(group)
    out["label"] = _session_label(group)
    out["channel"] = (
        "kf" if str(group.get("roomid") or "").startswith("kf:") else "group"
    )
    return _ok(session=out, materials=material_items)


def _handle_jobs_list(
    store: ExternalGroupStore, *, status: str, limit: int
) -> tuple[dict[str, Any], int]:
    items = store.list_registration_jobs(limit=limit, status=status)
    # 列表不返回完整 payload，减小响应
    slim: list[dict[str, Any]] = []
    for it in items:
        row = dict(it)
        row.pop("payload_json", None)
        row.pop("result_messages", None)
        slim.append(row)
    return _ok(items=slim, status=status or "all", limit=limit)


_FIELD_LABELS: dict[str, str] = {
    "company_name_cn": "公司中文名",
    "company_name_en": "公司英文名",
    "registered_capital": "注册资本",
    "business_desc": "经营范围",
    "registered_office_cn": "注册地址（中文）",
    "registered_office_en": "注册地址（英文）",
    "contact.email": "联络邮箱",
    "contact.phone": "联络电话",
    "director.name": "董事兼股东姓名",
    "director.id_type": "证件类型",
    "director.id_number": "证件号码",
    "director.address_cn": "住址（中文）",
    "director.address_en": "住址（英文）",
    "applicant.name": "申请人姓名",
    "applicant.id_type": "申请人证件类型",
    "applicant.id_number": "申请人证件号码",
    "icris_account.username": "ICRIS 用户名",
    "icris_account.password": "ICRIS 密码",
    "identity_proof.id_type": "身份证明类型",
    "identity_proof.id_number": "身份证明号码",
    "identity_proof.document_files": "证件文件",
    "id_card_front": "身份证正面",
    "id_card_back": "身份证反面",
    "id_card_handheld": "手持身份证",
    "passport": "护照",
}


def _flatten_payload_fields(payload: dict[str, Any]) -> list[dict[str, str]]:
    """将 company_data 展平为详情字段（含中文 label）。"""
    fields: list[dict[str, str]] = []

    def add(key: str, value: Any, label: str | None = None) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            text = "；".join(str(x).strip() for x in value if str(x).strip())
        else:
            text = str(value).strip()
        if not text:
            return
        fields.append(
            {
                "key": key,
                "label": label or _FIELD_LABELS.get(key, key),
                "value": text,
            }
        )

    add("company_name_cn", payload.get("company_name_cn"))
    add("company_name_en", payload.get("company_name_en"))

    sc = payload.get("share_capital") or {}
    if isinstance(sc, dict) and sc.get("total_shares") is not None:
        cur = str(sc.get("currency") or "HKD")
        add("registered_capital", f"{sc.get('total_shares')} {cur}")
    else:
        add("registered_capital", payload.get("registered_capital"))

    add(
        "business_desc",
        payload.get("business_nature_desc") or payload.get("business_desc"),
    )

    office = payload.get("registered_office") or {}
    if isinstance(office, dict):
        add(
            "registered_office_cn",
            office.get("street_cn") or payload.get("registered_office_cn"),
        )
        add(
            "registered_office_en",
            office.get("street_en")
            or office.get("street")
            or payload.get("registered_office_en"),
        )
    else:
        add("registered_office_cn", payload.get("registered_office_cn"))
        add("registered_office_en", payload.get("registered_office_en"))

    contact = payload.get("contact") or {}
    if isinstance(contact, dict):
        add("contact.email", contact.get("email"))
        add("contact.phone", contact.get("phone"))

    directors = payload.get("directors") or []
    director = directors[0] if isinstance(directors, list) and directors else {}
    if not isinstance(director, dict):
        director = {}
    applicant = payload.get("applicant") or {}
    if not isinstance(applicant, dict):
        applicant = {}

    add(
        "director.name",
        director.get("name_en")
        or director.get("name")
        or director.get("name_cn")
        or applicant.get("name_cn")
        or applicant.get("name_en"),
    )
    add(
        "director.id_type",
        applicant.get("id_type") or director.get("id_type"),
    )
    add(
        "director.id_number",
        applicant.get("id_number") or director.get("id_number"),
    )
    add(
        "director.address_cn",
        director.get("address_cn") or applicant.get("address_cn"),
    )
    add(
        "director.address_en",
        director.get("address_en") or applicant.get("address_en"),
    )

    account = payload.get("icris_account") or {}
    if isinstance(account, dict):
        add("icris_account.username", account.get("username"))
        add("icris_account.password", account.get("password"))

    proof = payload.get("identity_proof") or {}
    if isinstance(proof, dict):
        add("identity_proof.id_type", proof.get("id_type"))
        add("identity_proof.id_number", proof.get("id_number"))
        docs = proof.get("document_files") or []
        if docs:
            add("identity_proof.document_files", docs)

    return fields


def _handle_job_detail(
    store: ExternalGroupStore, job_id: int
) -> tuple[dict[str, Any], int]:
    import json

    job = store.get_registration_job(job_id)
    if not job:
        return _err("job not found", 404)
    out = dict(job)
    payload: dict[str, Any] = {}
    raw = out.get("payload_json") or ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
    messages: list[str] = []
    raw_msgs = out.get("result_messages") or ""
    if raw_msgs:
        try:
            parsed_msgs = json.loads(raw_msgs)
            if isinstance(parsed_msgs, list):
                messages = [str(x) for x in parsed_msgs]
        except (TypeError, ValueError, json.JSONDecodeError):
            messages = []
    fields = _flatten_payload_fields(payload) if payload else []
    # 无 payload 时回退 materials（中文 label）
    if not fields:
        materials = store.get_materials(str(out.get("roomid") or ""))
        for key in sorted(materials.keys()):
            row = materials[key]
            val = str(row.get("field_value") or "").strip()
            fpath = str(row.get("file_path") or "").strip()
            label = _FIELD_LABELS.get(key, key)
            if fpath:
                fields.append({"key": key, "label": label, "value": fpath})
            elif val:
                fields.append({"key": key, "label": label, "value": val})
    return _ok(
        job=out,
        payload=payload,
        fields=fields,
        messages=messages,
    )


def _handle_job_cancel(
    store: ExternalGroupStore, job_id: int
) -> tuple[dict[str, Any], int]:
    job = store.cancel_registration_job(job_id)
    if not job:
        return _err("job not found", 404)
    if job.get("status") != "cancelled":
        return _err(
            f"cannot cancel (status={job.get('status')}; pending only)",
            409,
        )
    rid = str(job.get("roomid") or "")
    if rid:
        store.set_group_status(rid, "FAILED")
    return _ok(job=job, message=f"cancelled #{job_id}")


def _handle_job_requeue(
    store: ExternalGroupStore, job_id: int
) -> tuple[dict[str, Any], int]:
    job = store.requeue_registration_job(job_id)
    if not job:
        return _err("job not found", 404)
    if job.get("status") != "pending":
        return _err(
            f"cannot requeue (status={job.get('status')}; "
            "need failed/cancelled and no active job)",
            409,
        )
    rid = str(job.get("roomid") or "")
    if rid:
        store.set_group_status(rid, "QUEUED")
    return _ok(job=job, message=f"requeued #{job_id}")


def _handle_quality(
    store: ExternalGroupStore, *, hours: float
) -> tuple[dict[str, Any], int]:
    hours = max(1.0, float(hours or 24.0))
    try:
        stats = store.conversation_quality_stats(hours=hours)
    except Exception:
        stats = {
            "hours": hours,
            "agent_runs_total": 0,
            "actions": {},
            "reply_rate": 0.0,
            "avg_confidence": 0.0,
            "low_confidence_count": 0,
            "qa_latency_ms": {},
            "intent_routes": {},
        }
    try:
        low = store.list_low_confidence_runs(limit=50, threshold=0.5)
    except Exception:
        low = []
    return _ok(stats=stats, low_confidence_runs=low, hours=hours)
