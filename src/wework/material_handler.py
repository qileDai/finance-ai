"""群文件接收、分类与入库"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from config.settings import settings
from src.llm.openai_client import LLMClient
from src.materials.checklist import FILE_FIELD_KEYS
from src.materials.id_document_vision import (
    ID_TYPE_LABELS,
    is_image_bytes,
    recognize_id_document,
)
from src.storage.db import ExternalGroupStore
from src.storage.file_store import save_bytes

logger = logging.getLogger(__name__)

REJECTED_NON_ID = "rejected_non_id"
REJECTED_UPLOAD = "rejected_upload"

CLASSIFY_RULES: list[tuple[str, str]] = [
    (r"身份证|id.?card|hkid", "id_card_front"),
    (r"护照|passport", "passport"),
    (r"地址|address|水电|账单", "address_proof"),
    (r"背面|back|反面", "id_card_back"),
]

# 视觉调用失败（非「模型判定非证件」）时仍按文件名落盘，避免接口故障丢材料
_VISION_HARD_ERRORS = frozenset({
    "vision_disabled",
    "no_api_key",
    "not_image",
})


@dataclass
class MaterialHandler:
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    llm: LLMClient = field(default_factory=LLMClient)

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
        )
        logger.info("群 %s 文件已入库 field=%s path=%s", roomid, field_key, dest)
        return field_key

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
        # 先做大小/类型校验，避免大文件进视觉
        from src.storage.file_store import validate_upload

        try:
            validate_upload(filename, data)
        except ValueError as e:
            self._last_upload_reject_reason = str(e)
            logger.warning("上传校验拒绝 room=%s file=%s: %s", roomid, filename, e)
            return REJECTED_UPLOAD

        filename_key = (
            self.classify_by_llm(filename, roomid)
            if use_llm
            else self.classify_by_filename(filename)
        )
        folder_label = self._folder_label(roomid)

        # 地址证明：注册材料，始终保存
        if filename_key == "address_proof":
            return self._persist_file(
                roomid, msgid, filename, data, "address_proof", "received",
                folder_label=folder_label,
            )

        vision_enabled = (
            settings.wework_id_vision_enabled
            and bool(settings.openai_api_key)
            and is_image_bytes(filename, data)
        )

        if vision_enabled:
            vision = recognize_id_document(data, filename=filename)
            is_id = vision.ok or (
                vision.id_type != "unknown" and vision.confidence >= 0.55
            )
            hard_err = (vision.error or "") in _VISION_HARD_ERRORS
            # 接口/系统硬错误：回退按文件名保存，避免丢件
            api_failed = bool(vision.error) and not hard_err and not is_id and (
                vision.error not in (
                    "unreliable", "low_confidence", "number_invalid", "json_parse", "",
                )
            )

            if is_id:
                field_key = filename_key
                if field_key not in ("id_card_back",) and vision.file_field_key != "unknown":
                    field_key = vision.file_field_key
                if field_key == "unknown" or field_key.startswith("file_"):
                    field_key = vision.file_field_key if vision.file_field_key != "unknown" else "id_card_front"
                status = "received" if vision.ok else "needs_review"
                if vision.id_type != "unknown":
                    self.store.upsert_material(
                        roomid,
                        "id_type",
                        field_value=vision.id_type,
                        file_path="",
                        source="id_vision",
                        status=status,
                    )
                if vision.ok and vision.id_number:
                    self.store.upsert_material(
                        roomid,
                        "id_number",
                        field_value=vision.id_number,
                        file_path="",
                        source="id_vision",
                        status="received",
                    )
                elif vision.id_type != "unknown":
                    self.store.upsert_material(
                        roomid,
                        "id_type",
                        field_value=vision.id_type,
                        file_path="",
                        source="id_vision",
                        status="needs_review",
                    )
                logger.info(
                    "群 %s 证件视觉识别 type=%s ok=%s",
                    roomid,
                    vision.id_type,
                    vision.ok,
                )
                return self._persist_file(
                    roomid, msgid, filename, data, field_key, status,
                    folder_label=folder_label,
                )

            if not api_failed and not hard_err:
                # 模型判定非身份证明：不落盘、不入库
                logger.info(
                    "群 %s 非身份证明图片，跳过存档 filename=%s err=%s",
                    roomid,
                    filename,
                    vision.error,
                )
                return REJECTED_NON_ID

        # 非图片 / 视觉关闭 / 视觉硬错误或 API 失败：按文件名保存
        field_key = filename_key
        status = "received" if field_key != "unknown" else "needs_review"
        if field_key == "unknown":
            field_key = f"file_{msgid[:8]}"
        return self._persist_file(
            roomid, msgid, filename, data, field_key, status,
            folder_label=folder_label,
        )

    def notify_classification(
        self,
        field_key: str,
        filename: str,
        *,
        roomid: str = "",
    ) -> str:
        # 客户可见引导（内部入库仍用 PRC_ID/HKID/PASSPORT）
        supplement_hint = "证件类型=中国身份证|香港身份证|护照 号码=…"
        materials = self.store.get_materials(roomid) if roomid else {}
        id_type = str((materials.get("id_type") or {}).get("field_value") or "").strip().upper()
        id_number = str((materials.get("id_number") or {}).get("field_value") or "").strip()
        id_type_status = str((materials.get("id_type") or {}).get("status") or "")

        if field_key == REJECTED_NON_ID:
            return (
                f"已收到图片「{filename}」，未能识别为身份证明，未予存档。"
                "若为证件请重传清晰正反面；也可文字补充："
                f"{supplement_hint}"
            )
        if field_key == REJECTED_UPLOAD:
            reason = getattr(self, "_last_upload_reject_reason", "") or "文件不符合要求"
            return f"文件「{filename}」未保存：{reason}"

        if id_type in ID_TYPE_LABELS and id_number:
            return (
                f"已识别为：{ID_TYPE_LABELS[id_type]}，号码：{id_number}"
                f"（{filename}）"
            )
        if id_type in ID_TYPE_LABELS and id_type_status == "needs_review":
            return (
                f"已识别证件类型为：{ID_TYPE_LABELS[id_type]}，但未能可靠读取号码"
                f"（{filename}）。请文字补充：证件类型={ID_TYPE_LABELS[id_type]} 号码=…"
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
            "passport": "护照",
            "address_proof": "地址证明",
        }
        if field_key in ("id_card_front", "id_card_back", "passport"):
            return (
                f"已收到并归类为：{labels.get(field_key, field_key)}（{filename}）。"
                f"未能可靠识别证件类型/号码，请文字补充：{supplement_hint}"
            )
        return f"已收到并归类为：{labels.get(field_key, field_key)}（{filename}）"
