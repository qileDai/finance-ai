"""群文件接收、分类与入库"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from config.settings import settings
from src.llm.openai_client import LLMClient
from src.materials.checklist import FILE_FIELD_KEYS
from src.materials.id_document_ocr import enrich_number_from_ocr
from src.materials.id_document_vision import (
    ID_TYPE_LABELS,
    ID_TYPE_UNKNOWN,
    IdDocumentResult,
    id_numbers_match,
    is_image_bytes,
    names_match,
    recognize_id_document,
    validate_id_number,
)
from src.storage.db import ExternalGroupStore
from src.storage.file_store import save_bytes

logger = logging.getLogger(__name__)

REJECTED_NON_ID = "rejected_non_id"
REJECTED_UPLOAD = "rejected_upload"
REJECTED_BLUR = "rejected_blur"
DUPLICATE_FILE = "duplicate"

CLASSIFY_RULES: list[tuple[str, str]] = [
    (r"手持|hand.?held", "id_card_handheld"),
    (r"背面|back|反面", "id_card_back"),  # 须在「身份证」通用规则前
    (r"正面|front", "id_card_front"),
    (r"身份证|id.?card|hkid", "id_card_front"),
    (r"护照|passport", "passport"),
    (r"地址|address|水电|账单", "address_proof"),
]

_ID_FILE_KEYS = frozenset(
    {"id_card_front", "id_card_back", "id_card_handheld", "passport"}
)

_VISION_HARD_ERRORS = frozenset({
    "vision_disabled",
    "no_api_key",
    "not_image",
})


def _check_image_quality(data: bytes) -> tuple[bool, float]:
    """图片清晰度预检：Laplacian 方差法。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return True, -1.0
    try:
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return True, -1.0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        threshold = float(getattr(settings, "materials_blur_threshold", 100.0) or 100.0)
        return variance >= threshold, variance
    except Exception as e:
        logger.debug("图片质量检查失败，降级放行: %s", e)
        return True, -1.0


def _min_conf() -> float:
    return float(getattr(settings, "materials_id_min_confidence", 0.55) or 0.55)


@dataclass
class MaterialHandler:
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    llm: LLMClient = field(default_factory=LLMClient)

    def __post_init__(self) -> None:
        self._last_upload_reject_reason = ""
        self._last_id_verify_msg = ""

    def classify_by_filename(self, filename: str) -> str:
        name = filename.lower()
        for pattern, key in CLASSIFY_RULES:
            if re.search(pattern, name, re.I):
                return key
        return "unknown"

    def classify_by_llm(self, filename: str, roomid: str) -> str:
        """可选 LLM 分类（基于文件名 + 上下文）"""
        key = self.classify_by_filename(filename)
        if key != "unknown":
            return key
        try:
            prompt = (
                f"文件名: {filename}\n"
                f"可选类型: id_card_front, id_card_back, passport, address_proof, unknown\n"
                "只输出一个类型 key，不要解释。"
            )
            ans = self.llm.chat(
                "你是文档分类助手，将上传文件归类为香港公司注册材料类型。",
                prompt,
                temperature=0,
            ).strip()
            if ans in FILE_FIELD_KEYS or ans == "unknown":
                return ans
        except Exception as e:
            logger.debug("LLM 分类失败: %s", e)
        return "unknown"

    def _folder_label(self, roomid: str) -> str:
        materials = self.store.get_materials(roomid)
        cn = str((materials.get("company_name_cn") or {}).get("field_value") or "").strip()
        en = str((materials.get("company_name_en") or {}).get("field_value") or "").strip()
        return cn or en or ""

    def _persist_file(
        self,
        roomid: str,
        msgid: str,
        filename: str,
        data: bytes,
        field_key: str,
        status: str,
        *,
        folder_label: str,
        file_hash: str = "",
    ) -> str:
        try:
            dest = save_bytes(
                roomid,
                f"{msgid}_{filename}",
                data,
                folder_label=folder_label,
            )
        except ValueError as e:
            logger.warning("上传校验拒绝 room=%s file=%s: %s", roomid, filename, e)
            self._last_upload_reject_reason = str(e)
            return REJECTED_UPLOAD
        self.store.upsert_material(
            roomid,
            field_key,
            field_value=filename,
            file_path=str(dest),
            source="chat_file",
            status=status,
            file_hash=file_hash,
        )
        logger.info("群 %s 文件已入库 field=%s path=%s", roomid, field_key, dest)
        return field_key

    def _apply_ocr_fallback(self, vision: IdDocumentResult, data: bytes) -> IdDocumentResult:
        if vision.id_number and validate_id_number(vision.id_type, vision.id_number):
            return vision
        itype, inum = enrich_number_from_ocr(
            image_bytes=data,
            id_type=vision.id_type,
            id_number=vision.id_number,
        )
        if inum:
            vision.id_number = inum
            if itype and itype != ID_TYPE_UNKNOWN:
                vision.id_type = itype
            if vision.id_type != ID_TYPE_UNKNOWN and validate_id_number(
                vision.id_type, vision.id_number
            ):
                if vision.confidence >= _min_conf() or vision.side == "back":
                    vision.ok = True
                    if vision.error in ("number_invalid", "unreliable", "low_confidence"):
                        vision.error = ""
        return vision

    def _check_consistency(
        self,
        roomid: str,
        vision: IdDocumentResult,
        *,
        field_key: str,
    ) -> tuple[str, str]:
        """与已填姓名/号码比对。返回 (status, verify_msg)。"""
        self._last_id_verify_msg = ""
        if not getattr(settings, "materials_id_name_match", True):
            return "received", ""

        materials = self.store.get_materials(roomid)
        submitted_name = str(
            (materials.get("director_name") or {}).get("field_value")
            or (materials.get("directors") or {}).get("field_value")
            or ""
        ).strip()
        submitted_num = str(
            (materials.get("id_number") or {}).get("field_value") or ""
        ).strip()

        problems: list[str] = []
        if vision.full_name and submitted_name and field_key != "id_card_back":
            if not names_match(vision.full_name, submitted_name):
                problems.append(
                    f"证件姓名「{vision.full_name}」与所填「{submitted_name}」不一致"
                )
        if vision.id_number and submitted_num:
            itype = vision.id_type if vision.id_type != ID_TYPE_UNKNOWN else str(
                (materials.get("id_type") or {}).get("field_value") or ""
            )
            if not id_numbers_match(itype, vision.id_number, submitted_num):
                problems.append(
                    f"证件号码与所填号码不一致（识别 {vision.id_number}）"
                )

        if problems:
            detail = "；".join(problems)
            self.store.upsert_material(
                roomid,
                "id_verify_status",
                field_value="mismatch",
                file_path="",
                source="id_verify",
                status="needs_review",
            )
            self.store.upsert_material(
                roomid,
                "id_verify_detail",
                field_value=detail,
                file_path="",
                source="id_verify",
                status="needs_review",
            )
            self._last_id_verify_msg = detail
            return "needs_review", detail

        prev = str((materials.get("id_verify_status") or {}).get("field_value") or "")
        if prev == "mismatch" and (vision.id_number or vision.full_name):
            if (not submitted_num or vision.id_number) and (
                not submitted_name or vision.full_name or field_key == "id_card_back"
            ):
                self.store.upsert_material(
                    roomid,
                    "id_verify_status",
                    field_value="matched",
                    file_path="",
                    source="id_verify",
                    status="received",
                )
                self.store.upsert_material(
                    roomid,
                    "id_verify_detail",
                    field_value="",
                    file_path="",
                    source="id_verify",
                    status="received",
                )
        return "received", ""

    def _upsert_vision_fields(
        self,
        roomid: str,
        vision: IdDocumentResult,
        *,
        status: str,
        field_key: str,
    ) -> None:
        if vision.id_type != ID_TYPE_UNKNOWN:
            self.store.upsert_material(
                roomid,
                "id_type",
                field_value=vision.id_type,
                file_path="",
                source="id_vision",
                status=status if status == "received" else "needs_review",
            )
        if vision.id_number and vision.id_type != ID_TYPE_UNKNOWN and validate_id_number(
            vision.id_type, vision.id_number
        ):
            materials = self.store.get_materials(roomid)
            existing_num = str(
                (materials.get("id_number") or {}).get("field_value") or ""
            ).strip()
            if not existing_num or id_numbers_match(
                vision.id_type, existing_num, vision.id_number
            ):
                self.store.upsert_material(
                    roomid,
                    "id_number",
                    field_value=vision.id_number,
                    file_path="",
                    source="id_vision",
                    status="received",
                )
        if vision.full_name and field_key != "id_card_back":
            materials = self.store.get_materials(roomid)
            existing_name = str(
                (materials.get("director_name") or {}).get("field_value")
                or (materials.get("directors") or {}).get("field_value")
                or ""
            ).strip()
            if not existing_name:
                self.store.upsert_material(
                    roomid,
                    "director_name",
                    field_value=vision.full_name,
                    file_path="",
                    source="id_vision",
                    status="received",
                )

    def _id_slot_state(self, roomid: str) -> tuple[bool, bool]:
        """返回 (已有正面文件, 已有反面文件)。"""
        materials = self.store.get_materials(roomid)
        has_front = bool(
            (materials.get("id_card_front") or {}).get("file_path")
            or (materials.get("id_card_front") or {}).get("field_value")
        )
        has_back = bool(
            (materials.get("id_card_back") or {}).get("file_path")
            or (materials.get("id_card_back") or {}).get("field_value")
        )
        return has_front, has_back

    def _resolve_id_field_key(
        self,
        roomid: str,
        vision: IdDocumentResult,
        filename_key: str,
    ) -> str:
        """归类文件槽位：已有正面且缺反面时，第二张一律进反面（Vision 常误判 side=front）。"""
        field_key = vision.file_field_key
        has_front, has_back = self._id_slot_state(roomid)

        if field_key == "unknown":
            field_key = (
                filename_key if filename_key in _ID_FILE_KEYS else "id_card_front"
            )

        if vision.id_type == "PASSPORT" or field_key == "passport":
            return "passport"

        if vision.side == "back" or filename_key == "id_card_back":
            return "id_card_back"

        # 缺反面时第二张优先补反面，避免误判 front 盖写正面导致一直缺反面
        if has_front and not has_back:
            return "id_card_back"

        if vision.is_handheld or field_key == "id_card_handheld":
            return "id_card_handheld"
        return field_key if field_key in _ID_FILE_KEYS else "id_card_front"

    def _has_usable_id_number(
        self,
        roomid: str,
        vision: IdDocumentResult | None = None,
    ) -> bool:
        """Vision/OCR 或库中已有合法证件号码。"""
        materials = self.store.get_materials(roomid)
        stored_num = str(
            (materials.get("id_number") or {}).get("field_value") or ""
        ).strip()
        stored_type = str(
            (materials.get("id_type") or {}).get("field_value") or ""
        ).strip()
        stored_st = str((materials.get("id_number") or {}).get("status") or "")
        if (
            stored_num
            and stored_st in ("received", "confirmed")
            and (
                not stored_type
                or stored_type == ID_TYPE_UNKNOWN
                or validate_id_number(stored_type, stored_num)
            )
        ):
            return True
        if vision is None:
            return False
        itype = vision.id_type if vision.id_type != ID_TYPE_UNKNOWN else stored_type
        return bool(
            vision.id_number
            and itype
            and itype != ID_TYPE_UNKNOWN
            and validate_id_number(itype, vision.id_number)
        )

    def _id_file_status(
        self,
        roomid: str,
        vision: IdDocumentResult,
        *,
        field_key: str,
        c_status: str,
    ) -> str:
        """文件槽状态：反面有文件即 received；正面有合法号码且无 mismatch → received。"""
        if field_key == "id_card_back":
            return "received"
        if c_status == "needs_review":
            return "needs_review"
        if field_key == "id_card_front":
            materials = self.store.get_materials(roomid)
            if str(
                (materials.get("id_verify_status") or {}).get("field_value") or ""
            ) == "mismatch":
                return "needs_review"
            if self._has_usable_id_number(roomid, vision):
                return "received"
            min_c = _min_conf()
            if vision.confidence < min_c or not vision.ok:
                return "needs_review"
            return "received"
        min_c = _min_conf()
        if vision.confidence < min_c or not vision.ok:
            return "needs_review"
        return "received"

    def _promote_id_front_if_ready(self, roomid: str) -> None:
        """正面已有文件且号码已收、无 mismatch 时，去掉 needs_review 以免一直提示缺正面。"""
        materials = self.store.get_materials(roomid)
        if str((materials.get("id_verify_status") or {}).get("field_value") or "") == "mismatch":
            return
        front = materials.get("id_card_front") or {}
        if str(front.get("status") or "") != "needs_review":
            return
        if not (front.get("file_path") or front.get("field_value")):
            return
        if not self._has_usable_id_number(roomid):
            return
        self.store.upsert_material(
            roomid,
            "id_card_front",
            field_value=str(front.get("field_value") or ""),
            file_path=str(front.get("file_path") or ""),
            source=str(front.get("source") or "chat_file"),
            status="received",
            file_hash=str(front.get("file_hash") or ""),
        )
        id_type_row = materials.get("id_type") or {}
        if str(id_type_row.get("status") or "") == "needs_review" and id_type_row.get("field_value"):
            self.store.upsert_material(
                roomid,
                "id_type",
                field_value=str(id_type_row.get("field_value") or ""),
                file_path="",
                source=str(id_type_row.get("source") or "id_vision"),
                status="received",
            )

    def _persist_as_id_back(
        self,
        roomid: str,
        msgid: str,
        filename: str,
        data: bytes,
        *,
        folder_label: str,
        file_hash: str,
        vision: IdDocumentResult | None = None,
        status: str = "received",
    ) -> str:
        """强制落入身份证反面槽（已有正面时的兜底）。"""
        if vision is not None:
            if not vision.side:
                vision.side = "back"
            # 反面文件在即可；不因无号码拖成缺项
            self._upsert_vision_fields(
                roomid, vision, status=status, field_key="id_card_back",
            )
        logger.info("群 %s 证件按反面槽落盘 filename=%s status=%s", roomid, filename, status)
        key = self._persist_file(
            roomid, msgid, filename, data, "id_card_back", status,
            folder_label=folder_label, file_hash=file_hash,
        )
        if key == "id_card_back" and status == "received":
            self._promote_id_front_if_ready(roomid)
        return key

    def save_file_message(
        self,
        roomid: str,
        msgid: str,
        filename: str,
        data: bytes,
        *,
        use_llm: bool = False,
    ) -> str:
        """先视觉识别身份证明；非证件不落盘。目录优先公司中文/英文名。"""
        self._last_upload_reject_reason = ""
        self._last_id_verify_msg = ""
        self._force_id_back = False
        from src.storage.file_store import validate_upload

        try:
            validate_upload(filename, data)
        except ValueError as e:
            self._last_upload_reject_reason = str(e)
            logger.warning("上传校验拒绝 room=%s file=%s: %s", roomid, filename, e)
            return REJECTED_UPLOAD

        file_hash = hashlib.sha256(data).hexdigest()
        if getattr(settings, "materials_dedup_enabled", True):
            existing = self.store.find_material_by_hash(roomid, file_hash)
            if existing:
                has_front, has_back = self._id_slot_state(roomid)
                existing_key = str(existing.get("field_key") or "")
                # 反面图曾误存正面时，同 hash 再传需放行以补 id_card_back
                allow_back_fill = (
                    has_front
                    and not has_back
                    and existing_key != "id_card_back"
                    and is_image_bytes(filename, data)
                )
                if allow_back_fill:
                    self._force_id_back = True
                    logger.info(
                        "群 %s 去重放行补反面 existing=%s hash=%s",
                        roomid, existing_key, file_hash[:12],
                    )
                else:
                    logger.info(
                        "群 %s 重复文件跳过 field=%s hash=%s",
                        roomid, existing_key, file_hash[:12],
                    )
                    self._last_upload_reject_reason = "duplicate"
                    return DUPLICATE_FILE

        if (
            getattr(settings, "materials_image_quality_enabled", True)
            and is_image_bytes(filename, data)
        ):
            ok, variance = _check_image_quality(data)
            if not ok:
                logger.info(
                    "群 %s 图片模糊拒绝 filename=%s variance=%.1f",
                    roomid, filename, variance,
                )
                self._last_upload_reject_reason = f"blur variance={variance:.1f}"
                return REJECTED_BLUR

        filename_key = (
            self.classify_by_llm(filename, roomid)
            if use_llm
            else self.classify_by_filename(filename)
        )
        folder_label = self._folder_label(roomid)

        if filename_key == "address_proof":
            return self._persist_file(
                roomid, msgid, filename, data, "address_proof", "received",
                folder_label=folder_label, file_hash=file_hash,
            )

        vision_enabled = (
            settings.wework_id_vision_enabled
            and bool(settings.openai_api_key)
            and is_image_bytes(filename, data)
        )

        if vision_enabled:
            vision = recognize_id_document(data, filename=filename)
            vision = self._apply_ocr_fallback(vision, data)

            min_c = _min_conf()
            has_front, has_back = self._id_slot_state(roomid)

            is_id = vision.ok or (
                vision.id_type != ID_TYPE_UNKNOWN and vision.confidence >= min_c
            ) or (
                vision.side == "back" and vision.confidence >= 0.35
            ) or (
                # 已有正面、缺反面：第二张证图放宽收录（含误判 side=front）
                has_front and not has_back
                and (vision.error or "") not in _VISION_HARD_ERRORS
                and (
                    vision.confidence >= 0.2
                    or vision.id_type != ID_TYPE_UNKNOWN
                    or bool(vision.id_number)
                )
            )

            hard_err = (vision.error or "") in _VISION_HARD_ERRORS
            api_failed = bool(vision.error) and not hard_err and not is_id and (
                vision.error not in (
                    "unreliable", "low_confidence", "number_invalid", "json_parse", "",
                )
            )

            if is_id:
                if getattr(self, "_force_id_back", False):
                    field_key = "id_card_back"
                    if not vision.side:
                        vision.side = "back"
                else:
                    field_key = self._resolve_id_field_key(roomid, vision, filename_key)
                    if field_key == "id_card_back" and not vision.side:
                        vision.side = "back"

                c_status, _detail = self._check_consistency(
                    roomid, vision, field_key=field_key,
                )
                status = self._id_file_status(
                    roomid, vision, field_key=field_key, c_status=c_status,
                )

                self._upsert_vision_fields(
                    roomid, vision, status=status, field_key=field_key,
                )
                key = self._persist_file(
                    roomid, msgid, filename, data, field_key, status,
                    folder_label=folder_label, file_hash=file_hash,
                )
                if key in ("id_card_front", "id_card_back") and status == "received":
                    self._promote_id_front_if_ready(roomid)
                logger.info(
                    "群 %s 证件识别 type=%s ok=%s side=%s name=%s field=%s status=%s",
                    roomid,
                    vision.id_type,
                    vision.ok,
                    vision.side,
                    (vision.full_name or "")[:8],
                    field_key,
                    status,
                )
                return key

            # 模型判非证件：若缺反面则仍按反面收档，避免用户已传却一直「还需要反面」
            if (has_front and not has_back and not hard_err) or getattr(
                self, "_force_id_back", False
            ):
                return self._persist_as_id_back(
                    roomid, msgid, filename, data,
                    folder_label=folder_label,
                    file_hash=file_hash,
                    vision=vision,
                    status="received",
                )

            if not api_failed and not hard_err:
                logger.info(
                    "群 %s 非身份证明图片，跳过存档 filename=%s err=%s",
                    roomid,
                    filename,
                    vision.error,
                )
                return REJECTED_NON_ID

        # 无视觉：文件名启发；已有正面缺反面时未知图归反面
        field_key = filename_key
        has_front, has_back = self._id_slot_state(roomid)
        if getattr(self, "_force_id_back", False):
            field_key = "id_card_back"
        elif field_key == "unknown" or field_key == "id_card_front":
            if has_front and not has_back and is_image_bytes(filename, data):
                field_key = "id_card_back"
        status = "received" if field_key != "unknown" else "needs_review"
        if field_key == "unknown":
            field_key = f"file_{msgid[:8]}"
        return self._persist_file(
            roomid, msgid, filename, data, field_key, status,
            folder_label=folder_label, file_hash=file_hash,
        )

    def notify_classification(
        self,
        field_key: str,
        filename: str,
        *,
        roomid: str = "",
    ) -> str:
        supplement_hint = "证件类型=中国身份证|香港身份证|护照 号码=…"
        materials = self.store.get_materials(roomid) if roomid else {}
        id_type = str((materials.get("id_type") or {}).get("field_value") or "").strip().upper()
        id_number = str((materials.get("id_number") or {}).get("field_value") or "").strip()
        id_type_status = str((materials.get("id_type") or {}).get("status") or "")
        verify_msg = getattr(self, "_last_id_verify_msg", "") or ""
        if not verify_msg:
            if str((materials.get("id_verify_status") or {}).get("field_value") or "") == "mismatch":
                verify_msg = str(
                    (materials.get("id_verify_detail") or {}).get("field_value") or ""
                )

        if field_key == REJECTED_NON_ID:
            return (
                f"已收到图片「{filename}」，未能识别为身份证明，未予存档。"
                "若为证件请重传清晰正反面；也可文字补充："
                f"{supplement_hint}"
            )
        if field_key == REJECTED_UPLOAD:
            reason = getattr(self, "_last_upload_reject_reason", "") or "文件不符合要求"
            return f"文件「{filename}」未保存：{reason}"
        if field_key == DUPLICATE_FILE:
            return f"文件「{filename}」与已收材料内容相同，已跳过重复存档。"
        if field_key == REJECTED_BLUR:
            return (
                f"图片「{filename}」清晰度不足，未能存档。"
                "请重新拍摄清晰正反面后上传。"
            )

        side_tag = {
            "id_card_front": "（正面）",
            "id_card_back": "（反面）",
            "id_card_handheld": "（手持照）",
            "passport": "",
        }.get(field_key, "")

        if verify_msg:
            return (
                f"已收到证件图片「{filename}」{side_tag}，但校验未通过：{verify_msg}。"
                "请核对后重传证件，或更正文字资料中的姓名/身份证号码。"
            )

        file_status = str((materials.get(field_key) or {}).get("status") or "")
        if id_type in ID_TYPE_LABELS and id_number and file_status != "needs_review":
            return (
                f"已识别为：{ID_TYPE_LABELS[id_type]}，号码：{id_number}"
                f"{side_tag}（{filename}）"
            )
        if id_type in ID_TYPE_LABELS and (
            id_type_status == "needs_review" or file_status == "needs_review"
        ):
            return (
                f"已归类为：{ID_TYPE_LABELS[id_type]}{side_tag}（{filename}），"
                "但识别置信不足或信息不完整，已标记待复核。"
                f"请确认清晰度后重传，或文字补充：{supplement_hint}"
            )

        if field_key == "unknown" or field_key.startswith("file_"):
            return (
                f"已收到图片「{filename}」，未能识别为身份证明。"
                "若为证件请重传清晰正反面；也可文字补充："
                f"{supplement_hint}"
            )
        labels = {
            "id_card_front": "身份证明（正面）",
            "id_card_back": "身份证明（反面）",
            "id_card_handheld": "手持身份证明照",
            "passport": "护照",
            "address_proof": "地址证明",
        }
        if field_key in ("id_card_front", "id_card_back", "id_card_handheld", "passport"):
            return (
                f"已收到并归类为：{labels.get(field_key, field_key)}（{filename}）。"
                f"未能可靠识别证件类型/号码，请文字补充：{supplement_hint}"
            )
        return f"已收到并归类为：{labels.get(field_key, field_key)}（{filename}）"
