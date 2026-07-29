"""群文件接收、分类与入库"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.llm.openai_client import LLMClient
from src.materials.checklist import FILE_FIELD_KEYS
from src.storage.db import ExternalGroupStore
from src.storage.file_store import save_bytes

logger = logging.getLogger(__name__)

CLASSIFY_RULES: list[tuple[str, str]] = [
    (r"身份证|id.?card|hkid", "id_card_front"),
    (r"护照|passport", "passport"),
    (r"地址|address|水电|账单", "address_proof"),
    (r"背面|back|反面", "id_card_back"),
]


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

    def save_file_message(
        self,
        roomid: str,
        msgid: str,
        filename: str,
        data: bytes,
        *,
        use_llm: bool = False,
    ) -> str:
        """保存群文件并写入 group_materials"""
        dest = save_bytes(roomid, f"{msgid}_{filename}", data)
        field_key = self.classify_by_llm(filename, roomid) if use_llm else self.classify_by_filename(filename)
        status = "received" if field_key != "unknown" else "needs_review"
        if field_key == "unknown":
            field_key = f"file_{msgid[:8]}"

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

    def notify_classification(self, field_key: str, filename: str) -> str:
        if field_key == "unknown" or field_key.startswith("file_"):
            return f"已收到文件「{filename}」，需人工确认类型，请补充说明或重新命名后上传。"
        labels = {
            "id_card_front": "身份证明（正面）",
            "id_card_back": "身份证明（反面）",
            "passport": "护照",
            "address_proof": "地址证明",
        }
        return f"已收到并归类为：{labels.get(field_key, field_key)}（{filename}）"
