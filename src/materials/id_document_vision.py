"""身份证明图片：多模态识别证件类型 + 号码（不接独立 OCR SDK）"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

ID_TYPE_HKID = "HKID"
ID_TYPE_PRC = "PRC_ID"
ID_TYPE_PASSPORT = "PASSPORT"
ID_TYPE_UNKNOWN = "unknown"

ID_TYPE_LABELS = {
    ID_TYPE_HKID: "香港身份证",
    ID_TYPE_PRC: "中华人民共和国身份证",
    ID_TYPE_PASSPORT: "护照",
}

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_PRC_RE = re.compile(r"^\d{17}[\dXx]$")
_HKID_RE = re.compile(r"^[A-Z]{1,2}\d{6}\(?[\dA]\)?$", re.I)
_PASSPORT_RE = re.compile(r"^[A-Z0-9]{5,15}$", re.I)

_MIN_CONFIDENCE = 0.55


@dataclass
class IdDocumentResult:
    id_type: str = ID_TYPE_UNKNOWN
    id_number: str = ""
    confidence: float = 0.0
    ok: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def type_label(self) -> str:
        return ID_TYPE_LABELS.get(self.id_type, "未知证件")

    @property
    def file_field_key(self) -> str:
        if self.id_type == ID_TYPE_PASSPORT:
            return "passport"
        if self.id_type in (ID_TYPE_HKID, ID_TYPE_PRC):
            return "id_card_front"
        return "unknown"


def is_image_bytes(filename: str, data: bytes) -> bool:
    """是否按图片处理（本期仅常见图片格式）"""
    ext = Path(filename or "").suffix.lower()
    if ext in _IMAGE_EXTS:
        return True
    if not data or len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


def validate_id_number(id_type: str, id_number: str) -> bool:
    num = (id_number or "").strip().replace(" ", "").upper()
    if not num:
        return False
    t = (id_type or "").upper()
    if t == ID_TYPE_PRC:
        return bool(_PRC_RE.match(num))
    if t == ID_TYPE_HKID:
        compact = num.replace("（", "(").replace("）", ")")
        return bool(_HKID_RE.match(compact))
    if t == ID_TYPE_PASSPORT:
        return bool(_PASSPORT_RE.match(num))
    return False


def normalize_id_number(id_type: str, id_number: str) -> str:
    num = (id_number or "").strip().replace(" ", "")
    t = (id_type or "").upper()
    if t == ID_TYPE_PRC:
        return num.upper()
    if t == ID_TYPE_HKID:
        return num.upper().replace("（", "(").replace("）", ")")
    if t == ID_TYPE_PASSPORT:
        return num.upper()
    return num


def _guess_mime(filename: str, data: bytes) -> str:
    mime, _ = mimetypes.guess_type(filename or "")
    if mime and mime.startswith("image/"):
        return mime
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _parse_vision_payload(data: dict[str, Any]) -> IdDocumentResult:
    raw_type = str(data.get("id_type") or "").strip().upper()
    if raw_type in ("CN_ID", "CHINA_ID", "MAINLAND", "PRC"):
        raw_type = ID_TYPE_PRC
    if raw_type in ("HK", "HK_ID", "HONGKONG"):
        raw_type = ID_TYPE_HKID
    if raw_type not in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_PASSPORT):
        raw_type = ID_TYPE_UNKNOWN

    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    number = normalize_id_number(raw_type, str(data.get("id_number") or ""))
    ok = (
        raw_type != ID_TYPE_UNKNOWN
        and validate_id_number(raw_type, number)
        and conf >= _MIN_CONFIDENCE
    )
    return IdDocumentResult(
        id_type=raw_type if ok or raw_type != ID_TYPE_UNKNOWN else ID_TYPE_UNKNOWN,
        id_number=number if ok else (number if validate_id_number(raw_type, number) else ""),
        confidence=conf,
        ok=ok,
        raw=data,
    )


def recognize_id_document(image_bytes: bytes, *, filename: str = "") -> IdDocumentResult:
    """多模态 LLM 看图：判定 HKID / PRC_ID / PASSPORT 并抽出号码。"""
    if not settings.wework_id_vision_enabled:
        return IdDocumentResult(error="vision_disabled")
    if not settings.openai_api_key:
        return IdDocumentResult(error="no_api_key")
    if not image_bytes or not is_image_bytes(filename, image_bytes):
        return IdDocumentResult(error="not_image")

    try:
        from openai import OpenAI

        model = (settings.openai_vision_model or "").strip() or settings.openai_model
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base,
        )
        mime = _guess_mime(filename, image_bytes)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        system = (
            "你是香港公司注册材料助手。根据证件图片判断类型并读取证件号码。"
            "只输出 JSON，不要解释。"
        )
        user_text = (
            "判断图片中的身份证明类型，并读取证件号码。\n"
            "id_type 只能是: HKID（香港身份证）、PRC_ID（中国大陆居民身份证）、"
            "PASSPORT（护照）、unknown（非证件或无法判断）。\n"
            "id_number: 证件上的号码字符串；读不清则空字符串。\n"
            "confidence: 0~1 的浮点数。\n"
            '输出格式: {"id_type":"PRC_ID","id_number":"...","confidence":0.9}'
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw_text = response.choices[0].message.content or "{}"
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("证件视觉 JSON 解析失败: %s", raw_text[:200])
            return IdDocumentResult(error="json_parse", raw={"text": raw_text[:500]})

        result = _parse_vision_payload(payload if isinstance(payload, dict) else {})
        if not result.ok:
            # 类型对但校验失败 / 低置信 → 不静默入库号码
            if result.id_type != ID_TYPE_UNKNOWN and not validate_id_number(
                result.id_type, result.id_number
            ):
                result.id_number = ""
                result.ok = False
                result.error = "number_invalid"
            elif result.confidence < _MIN_CONFIDENCE:
                result.error = result.error or "low_confidence"
            else:
                result.error = result.error or "unreliable"
        logger.info(
            "证件视觉识别 type=%s ok=%s conf=%.2f err=%s",
            result.id_type,
            result.ok,
            result.confidence,
            result.error,
        )
        return result
    except Exception as exc:
        logger.warning("证件视觉识别失败: %s", exc)
        return IdDocumentResult(error=str(exc)[:200])
