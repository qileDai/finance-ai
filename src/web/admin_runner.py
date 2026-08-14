"""管理后台「快速注册」：表单 → materials 落库 → registration_jobs 入队。

权威状态在 SQLite registration_jobs；内存 RunnerState 仅作最近一次兼容轮询。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from src.storage.db import ExternalGroupStore
from src.storage.file_store import materials_root, safe_dirname

logger = logging.getLogger(__name__)

TEXT_FIELDS = (
    "company_name_cn",
    "company_name_en",
    "registered_capital",
    "business_desc",
    "registered_office_cn",
    "registered_office_en",
    "director_name",
    "id_number",
    "director_address_cn",
    "director_address_en",
    "contact_email",
)

# 可落盘的证件字段（快速注册只需其一）
FILE_FIELDS = ("id_card_front", "id_card_back", "id_card_handheld", "passport")

_DATA_URL_RE = re.compile(r"^data:([\w/+.-]+);base64,(.*)$", re.DOTALL)

_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "application/pdf": "pdf",
}


@dataclass
class RunnerState:
    """最近一次快速注册（内存兼容；权威状态看 job_id → DB）。"""

    status: str = "idle"  # idle | running | succeeded | failed | pending
    started_at: str = ""
    finished_at: str = ""
    messages: list[Any] = field(default_factory=list)
    error: str = ""
    company_name: str = ""
    case_id: str = ""
    job_id: int | None = None
    dry_run: bool = True

    def is_running(self) -> bool:
        return self.status in ("running", "pending")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "messages": list(self.messages),
            "error": self.error,
            "company_name": self.company_name,
            "case_id": self.case_id,
            "job_id": self.job_id,
            "dry_run": self.dry_run,
        }


_lock = threading.Lock()
_state = RunnerState()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def defaults() -> dict[str, Any]:
    """快速注册表单默认值（含 MATERIALS_DEFAULT_CONTACT_EMAIL）。"""
    return {
        "contact_email": (
            getattr(settings, "materials_default_contact_email", "") or ""
        ).strip(),
        "contact_phone": (
            getattr(settings, "materials_default_contact_phone", "") or ""
        ).strip(),
    }


def _ext_from_mime(mime: str) -> str:
    return _MIME_EXT.get((mime or "").lower(), "bin")


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        raise ValueError("invalid data_url (expect data:<mime>;base64,<...>)")
    mime, b64 = m.group(1), m.group(2)
    raw = base64.b64decode(b64)
    return raw, _ext_from_mime(mime)


def _primary_id_file_key(id_type: str) -> str:
    return "passport" if id_type == "PASSPORT" else "id_card_front"


def _normalize_files_for_id_type(
    files: dict[str, dict[str, Any]], id_type: str
) -> dict[str, dict[str, Any]]:
    """支持前端单文件 key=id_document，映射到 id_card_front / passport。"""
    out = dict(files or {})
    primary = _primary_id_file_key(id_type)
    doc = out.pop("id_document", None)
    if isinstance(doc, dict) and str(doc.get("data_url") or "").strip():
        if not str((out.get(primary) or {}).get("data_url") or "").strip():
            out[primary] = doc
    return out


def _save_uploaded_files(
    files: dict[str, dict[str, Any]], case_dir: Path
) -> dict[str, str]:
    case_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for fld, meta in (files or {}).items():
        if fld not in FILE_FIELDS:
            continue
        data_url = str((meta or {}).get("data_url") or "").strip()
        if not data_url:
            continue
        raw, ext = _decode_data_url(data_url)
        base_name = safe_dirname(str((meta or {}).get("name") or fld))
        stem = Path(base_name).stem or fld
        suffix = Path(base_name).suffix or f".{ext}"
        fname = f"{stem}{suffix}"
        p = case_dir / fname
        i = 1
        while p.exists():
            p = case_dir / f"{stem}_{i}{suffix}"
            i += 1
        p.write_bytes(raw)
        out[fld] = str(p)
    return out


def _build_materials(
    fields: dict[str, str], file_paths: dict[str, str]
) -> dict[str, dict[str, Any]]:
    materials: dict[str, dict[str, Any]] = {}
    id_type = (fields.get("id_type") or "PRC_ID").strip().upper() or "PRC_ID"
    materials["id_type"] = {"field_key": "id_type", "field_value": id_type}
    for k in TEXT_FIELDS:
        v = (fields.get(k) or "").strip()
        if v:
            materials[k] = {"field_key": k, "field_value": v}
    for k, p in file_paths.items():
        materials[k] = {
            "field_key": k,
            "field_value": Path(p).name,
            "file_path": p,
            "status": "ok",
        }
    return materials


def _persist_materials(
    store: ExternalGroupStore,
    roomid: str,
    materials: dict[str, dict[str, Any]],
) -> None:
    for key, row in materials.items():
        store.upsert_material(
            roomid,
            key,
            field_value=str(row.get("field_value") or ""),
            file_path=str(row.get("file_path") or ""),
            source="admin",
            status=str(row.get("status") or "received"),
        )


def _validate(
    fields: dict[str, str], files: dict[str, dict[str, Any]]
) -> list[str]:
    errs: list[str] = []
    if not (fields.get("company_name_en") or "").strip():
        errs.append("公司英文名必填")
    if not (fields.get("director_name") or "").strip():
        errs.append("董事兼股东姓名必填")
    if not (fields.get("id_number") or "").strip():
        errs.append("身份证号码必填")
    email = (fields.get("contact_email") or "").strip()
    if not email:
        errs.append("联络邮箱必填（可用 MATERIALS_DEFAULT_CONTACT_EMAIL）")
    elif "@" not in email:
        errs.append("联络邮箱格式无效")
    has_addr = any(
        (fields.get(k) or "").strip()
        for k in (
            "director_address_cn",
            "director_address_en",
            "registered_office_cn",
            "registered_office_en",
        )
    )
    if not has_addr:
        errs.append("至少填写一个地址（住址或注册地址）")
    # 至少 1 个证件文件（PDF/图片）
    has_file = any(
        str((files.get(f) or {}).get("data_url") or "").strip() for f in FILE_FIELDS
    ) or str((files.get("id_document") or {}).get("data_url") or "").strip()
    if not has_file:
        errs.append("请上传证件文件（PDF 或图片，至少 1 个）")
    return errs


def _parse_result_messages(
    raw_msgs: Any, *, last_error: str = ""
) -> list[dict[str, str]]:
    """Normalize DB result_messages for runner status UI."""
    messages: list[dict[str, str]] = []
    if isinstance(raw_msgs, str) and raw_msgs.strip():
        try:
            parsed = json.loads(raw_msgs)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    level = str(item.get("level") or "INFO").upper()
                    text = str(item.get("message") or "").strip()
                    if not text:
                        continue
                    entry: dict[str, str] = {"level": level, "message": text}
                    t = str(item.get("time") or "").strip()
                    if t:
                        entry["time"] = t
                    messages.append(entry)
                else:
                    text = str(item or "").strip()
                    if text:
                        messages.append({"level": "INFO", "message": text})
    err = str(last_error or "").strip()
    if err:
        already = any(
            m.get("level") in ("ERROR", "CRITICAL") and err in str(m.get("message") or "")
            for m in messages[-5:]
        )
        if not already:
            messages.append({"level": "ERROR", "message": err})
    return messages


def _sync_state_from_job(job: dict[str, Any] | None) -> None:
    global _state
    if not job:
        return
    st = str(job.get("status") or "")
    mapped = {
        "pending": "pending",
        "running": "running",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "failed",
    }.get(st, st or "idle")
    msgs = _parse_result_messages(
        job.get("result_messages") or "",
        last_error=str(job.get("last_error") or ""),
    )
    with _lock:
        _state.status = mapped
        _state.job_id = int(job["id"]) if job.get("id") is not None else _state.job_id
        _state.case_id = str(job.get("roomid") or _state.case_id)
        _state.company_name = str(job.get("company_name") or _state.company_name)
        _state.error = str(job.get("last_error") or "")
        _state.messages = msgs
        _state.dry_run = bool(int(job.get("dry_run", 1) or 0))
        if job.get("started_at"):
            _state.started_at = str(job["started_at"])
        if job.get("finished_at"):
            _state.finished_at = str(job["finished_at"])
        elif mapped in ("pending", "running"):
            _state.finished_at = ""


def submit(
    fields: dict[str, str],
    files: dict[str, dict[str, Any]],
    *,
    dry_run: bool = True,
) -> tuple[dict[str, Any], int]:
    """校验 → 落盘/materials → enqueue registration_jobs。"""
    global _state
    fields = dict(fields or {})
    files = dict(files or {})
    id_type = (fields.get("id_type") or "PRC_ID").strip().upper() or "PRC_ID"
    fields["id_type"] = id_type

    # 空邮箱回退环境变量
    if not (fields.get("contact_email") or "").strip():
        fields["contact_email"] = (
            getattr(settings, "materials_default_contact_email", "") or ""
        ).strip()

    files = _normalize_files_for_id_type(files, id_type)

    errs = _validate(fields, files)
    if errs:
        return {"ok": False, "error": "；".join(errs)}, 400

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    case_id = safe_dirname(f"admin-quick-{ts}")
    case_dir = materials_root() / case_id
    try:
        file_paths = _save_uploaded_files(files, case_dir)
    except Exception as e:
        logger.exception("保存上传文件失败")
        return {"ok": False, "error": f"文件保存失败: {e}"}, 400

    from src.materials.aggregator import aggregate_company_data

    materials = _build_materials(fields, file_paths)
    try:
        company_data = aggregate_company_data(materials)
    except Exception as e:
        logger.exception("aggregate_company_data 失败")
        return {"ok": False, "error": f"资料聚合失败: {e}"}, 500

    company_name = (
        str(company_data.get("company_name_en") or "")
        or str(company_data.get("company_name_cn") or "")
    )

    # 表单 dry_run 为准：勾选=仅填表；取消勾选=允许提交
    dry_run = bool(dry_run)
    allow_submit = not dry_run

    store = ExternalGroupStore()
    store.upsert_group(
        case_id,
        name=f"admin-quick {company_name}".strip(),
        company_name=company_name,
        status="QUEUED",
    )
    _persist_materials(store, case_id, materials)

    job, created = store.enqueue_registration_job(
        case_id,
        customer_id="admin",
        dry_run=dry_run,
        allow_submit=allow_submit,
        max_attempts=settings.icris_job_max_attempts,
        payload=company_data,
        source="admin",
        company_name=company_name,
    )
    if not created:
        return {
            "ok": False,
            "error": f"已有活跃任务 #{job.get('id')}（{job.get('status')}）",
            "job_id": job.get("id"),
            "case_id": case_id,
        }, 409

    job_id = int(job["id"])
    with _lock:
        _state = RunnerState(
            status="pending",
            started_at=_utc_now(),
            messages=[{"level": "INFO", "message": f"已入队任务 #{job_id}，等待 Worker 执行"}],
            error="",
            company_name=company_name,
            case_id=case_id,
            job_id=job_id,
            dry_run=dry_run,
        )

    logger.info(
        "[快速注册] 已入队 job_id=%s case_id=%s company=%s files=%d "
        "email=%s dry_run=%s allow_submit=%s",
        job_id,
        case_id,
        company_name,
        len(file_paths),
        fields.get("contact_email", "")[:40],
        dry_run,
        allow_submit,
    )
    return {
        "ok": True,
        "case_id": case_id,
        "company_name": company_name,
        "job_id": job_id,
        "dry_run": dry_run,
    }, 200


def status() -> dict[str, Any]:
    with _lock:
        job_id = _state.job_id
        snap = _state.to_dict()
    if job_id:
        try:
            store = ExternalGroupStore()
            job = store.get_registration_job(int(job_id))
            if job:
                _sync_state_from_job(job)
                with _lock:
                    return _state.to_dict()
        except Exception:
            logger.exception("刷新 job 状态失败 id=%s", job_id)
    return snap
