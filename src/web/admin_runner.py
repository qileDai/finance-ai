"""管理后台「快速注册」运行器：表单数据 → aggregate → step_icris_register（后台线程）。

不依赖企微群/roomid、不依赖 ICRIS Worker 进程；admin 进程内单任务槽 + 内存状态。
"""

from __future__ import annotations

import base64
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from src.storage.file_store import materials_root, safe_dirname

logger = logging.getLogger(__name__)

# 表单文本字段（前端 key 与 materials key 一致）
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
)
# 证件文件字段
FILE_FIELDS = ("id_card_front", "id_card_back", "id_card_handheld", "passport")

# id_type → 需要的证件字段
_ID_TYPE_FILE_FIELDS: dict[str, tuple[str, ...]] = {
    "PRC_ID": ("id_card_front", "id_card_back", "id_card_handheld"),
    "HKID": ("id_card_front", "id_card_back", "id_card_handheld"),
    "PASSPORT": ("passport",),
}

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
    """单任务槽状态（内存，进程重启清空）。"""

    status: str = "idle"  # idle | running | succeeded | failed
    started_at: str = ""
    finished_at: str = ""
    messages: list[str] = field(default_factory=list)
    error: str = ""
    company_name: str = ""
    case_id: str = ""
    dry_run: bool = True

    def is_running(self) -> bool:
        return self.status == "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "messages": list(self.messages),
            "error": self.error,
            "company_name": self.company_name,
            "case_id": self.case_id,
            "dry_run": self.dry_run,
        }


_lock = threading.Lock()
_state = RunnerState()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 住址香港/非香港判断关键词（与 src.browser.icris_registration.detect_hk_address 同款）
_HK_ADDR_KEYWORDS = (
    "hong kong",
    "kowloon",
    "new territories",
    "香港",
    "九龍",
    "九龙",
    "新界",
)


def _detect_hk_address(addr_en: str, addr_cn: str) -> bool:
    """检测是否香港地址：含 香港/Hong Kong/Kowloon/九龍/新界 → True。"""
    addr = f"{addr_en or ''} {addr_cn or ''}".lower()
    return any(k in addr for k in _HK_ADDR_KEYWORDS)


def _ext_from_mime(mime: str) -> str:
    return _MIME_EXT.get((mime or "").lower(), "bin")


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """data:image/png;base64,xxxx → (bytes, ext)"""
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        raise ValueError("invalid data_url (expect data:<mime>;base64,<...>)")
    mime, b64 = m.group(1), m.group(2)
    raw = base64.b64decode(b64)
    return raw, _ext_from_mime(mime)


def _save_uploaded_files(
    files: dict[str, dict[str, Any]], case_dir: Path
) -> dict[str, str]:
    """{field: {name, data_url}} → {field: abs_path}。空/缺省跳过。"""
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
    """构造 materials dict（aggregator 输入格式：key → {field_key, field_value, ...}）。"""
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
    id_type = (fields.get("id_type") or "PRC_ID").strip().upper() or "PRC_ID"
    needed = _ID_TYPE_FILE_FIELDS.get(id_type, ())
    missing = [
        f
        for f in needed
        if not str((files.get(f) or {}).get("data_url") or "").strip()
    ]
    if missing:
        errs.append(f"证件文件缺失: {', '.join(missing)}")
    return errs


def submit(
    fields: dict[str, str], files: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], int]:
    """表单提交入口。返回 (json, code)。

    - 校验必填
    - 生成 case_id = admin-quick-<yyyymmdd-HHMMSS>，存文件到 materials_root()/case_id/
    - aggregate_company_data → company_data
    - 单任务槽：运行中返回 409
    - 起后台线程跑 step_icris_register(dry_run=True)
    """
    global _state
    fields = dict(fields or {})
    files = dict(files or {})
    id_type = (fields.get("id_type") or "PRC_ID").strip().upper() or "PRC_ID"
    fields["id_type"] = id_type

    with _lock:
        if _state.is_running():
            return {"ok": False, "error": "已有注册任务在运行，请等待完成"}, 409

    errs = _validate(fields, files)
    if errs:
        return {"ok": False, "error": "；".join(errs)}, 400

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    case_id = safe_dirname(f"admin-quick-{ts}")
    case_dir = materials_root() / case_id
    file_paths = _save_uploaded_files(files, case_dir)

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

    with _lock:
        _state = RunnerState(
            status="running",
            started_at=_utc_now(),
            messages=[],
            error="",
            company_name=company_name,
            case_id=case_id,
            dry_run=True,
        )

    th = threading.Thread(
        target=_run,
        args=(company_data, case_id),
        name="admin-quick-register",
        daemon=True,
    )
    th.start()
    logger.info(
        "[快速注册] 已启动 case_id=%s company=%s files=%d",
        case_id,
        company_name,
        len(file_paths),
    )
    return {"ok": True, "case_id": case_id, "company_name": company_name}, 200


def _run(company_data: dict[str, Any], case_id: str) -> None:
    """后台线程：跑 step_icris_register(dry_run=True)，更新 _state。"""
    global _state
    try:
        from src.workflow.steps import RegistrationWorkflow, WorkflowContext

        wf = RegistrationWorkflow()
        ctx = WorkflowContext(chat_id=case_id, company_data=company_data)

        # 住址判断（提前 log，让前端状态面板可见）
        director = (company_data.get("directors") or [{}])[0] or {}
        addr_cn = str(director.get("address_cn") or "").strip()
        addr_en = str(director.get("address_en") or "").strip()
        if addr_cn or addr_en:
            is_hk = _detect_hk_address(addr_en, addr_cn)
            ctx.log(
                f"住址判断: {'香港地址（本地地址）' if is_hk else '非香港地址（国家/地区=中国）'}"
            )
            if addr_cn:
                ctx.log(f"  住址中文: {addr_cn[:80]}")
            if addr_en:
                ctx.log(f"  住址英文: {addr_en[:80]}")

        wf.step_icris_register(
            ctx,
            dry_run=True,
            allow_submit=False,
            # 走 CDP 路径（与 `python main.py --step register` 一致）：
            # 自动启动 Chrome CDP（绕过 TLS 指纹检测）+ 注入 stealth 脚本
            force_isolated_browser=False,
        )
        with _lock:
            _state.status = "succeeded"
            _state.finished_at = _utc_now()
            _state.messages = list(ctx.messages)
            _state.error = ""
    except Exception as e:
        logger.exception("[快速注册] 运行失败 case_id=%s", case_id)
        with _lock:
            _state.status = "failed"
            _state.finished_at = _utc_now()
            _state.error = str(e)[:1000]


def status() -> dict[str, Any]:
    """返回当前任务状态。"""
    with _lock:
        return _state.to_dict()
