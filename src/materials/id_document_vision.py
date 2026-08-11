"""身份证明图片：多模态识别证件类型 / 正反面 / 姓名 / 号码"""

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


def _min_confidence() -> float:
    return float(getattr(settings, "materials_id_min_confidence", 0.55) or 0.55)


def _to_bool(val: Any) -> bool:
    """宽松归一为布尔：true/1/yes/是 → True。"""
    if isinstance(val, bool):
        return val
    s = str(val or "").strip().lower()
    return s in ("true", "1", "yes", "y", "是", "handheld")


@dataclass
class IdDocumentResult:
    id_type: str = ID_TYPE_UNKNOWN
    id_number: str = ""
    full_name: str = ""
    confidence: float = 0.0
    ok: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # 视觉判定的正反面（"front" | "back"）；护照或无法判断为空
    side: str = ""
    # 视觉判定是否手持证件拍照
    is_handheld: bool = False

    @property
    def type_label(self) -> str:
        return ID_TYPE_LABELS.get(self.id_type, "未知证件")

    @property
    def file_field_key(self) -> str:
        """映射到 group_materials 文件字段。"""
        if self.side == "back":
            return "id_card_back"
        if self.id_type == ID_TYPE_PASSPORT:
            return "passport"
        if self.is_handheld:
            return "id_card_handheld"
        if self.id_type in (ID_TYPE_HKID, ID_TYPE_PRC):
            if not getattr(settings, "wework_id_vision_side_classify_enabled", True):
                return "id_card_front"
            return "id_card_front"
        return "unknown"

    @property
    def classify_ok(self) -> bool:
        """分类把握是否达到阈值（允许无号码，如反面）。"""
        if self.id_type == ID_TYPE_UNKNOWN and self.side != "back":
            return False
        return self.confidence >= _min_confidence() or (
            self.side == "back" and self.confidence >= max(0.4, _min_confidence() - 0.15)
        )


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


def normalize_person_name(name: str) -> str:
    """姓名比对用归一化：去空格、统一间隔符、繁转简、全半角粗归一。"""
    s = (name or "").strip()
    if not s:
        return ""
    s = s.replace("·", "").replace(".", "").replace("．", "").replace(" ", "")
    s = s.replace("\u3000", "")
    # 全角字母数字 → 半角
    out: list[str] = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out)
    try:
        import zhconv

        s = zhconv.convert(s, "zh-cn")
    except Exception:
        pass
    return s.upper()


def names_match(a: str, b: str) -> bool:
    na, nb = normalize_person_name(a), normalize_person_name(b)
    if not na or not nb:
        return True  # 缺一侧不判冲突
    return na == nb


def id_numbers_match(id_type: str, a: str, b: str) -> bool:
    na = normalize_id_number(id_type, a)
    nb = normalize_id_number(id_type, b)
    if not na or not nb:
        return True
    return na.upper() == nb.upper()


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
    if raw_type in ("PP", "PASSPORT"):
        raw_type = ID_TYPE_PASSPORT
    if raw_type not in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_PASSPORT):
        raw_type = ID_TYPE_UNKNOWN

    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    number = normalize_id_number(raw_type, str(data.get("id_number") or ""))
    if number and raw_type == ID_TYPE_UNKNOWN:
        # 有合法号码时可反推类型
        if _PRC_RE.match(number):
            raw_type = ID_TYPE_PRC
        elif _HKID_RE.match(number.replace("（", "(").replace("）", ")")):
            raw_type = ID_TYPE_HKID

    full_name = str(data.get("full_name") or data.get("name") or "").strip()
    side = str(data.get("side") or "").strip().lower()
    if side not in ("front", "back"):
        side = ""
    is_handheld = _to_bool(data.get("is_handheld"))

    number_ok = bool(number) and (
        raw_type == ID_TYPE_UNKNOWN or validate_id_number(raw_type, number)
    )
    if number and not number_ok:
        number = ""

    # ok：分类可靠且（有合法号码，或反面允许无数）
    classified = raw_type != ID_TYPE_UNKNOWN or side == "back"
    conf_ok = conf >= _min_confidence() or (
        side == "back" and conf >= max(0.4, _min_confidence() - 0.15)
    )
    ok = bool(
        classified
        and conf_ok
        and (number_ok or side == "back" or (raw_type == ID_TYPE_PASSPORT and number_ok))
    )
    # 正面/护照：无号码则 ok=False，但仍可分类
    if raw_type in (ID_TYPE_HKID, ID_TYPE_PRC) and side != "back" and not number_ok:
        ok = False
    if raw_type == ID_TYPE_PASSPORT and not number_ok:
        ok = False

    return IdDocumentResult(
        id_type=raw_type,
        id_number=number if number_ok else "",
        full_name=full_name,
        confidence=conf,
        ok=ok,
        raw=data,
        side=side,
        is_handheld=is_handheld,
    )


def recognize_id_document(image_bytes: bytes, *, filename: str = "") -> IdDocumentResult:
    """多模态 LLM：每张图强制分类 PRC_ID/HKID/PASSPORT + 正反面，并抽姓名/号码。"""
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
            "你是香港公司注册材料助手。对每张证件图片必须完成四类判别："
            "内地身份证正面、内地身份证反面、香港身份证正/反面、护照资料页；"
            "或判定为非证件。只输出 JSON，不要解释。"
            "中国大陆/香港身份证的国徽面、机读码面（反面）也是有效身份证明；"
            "即使看不见人像，也应输出对应 id_type，并将 side 设为 back。"
        )
        user_text = (
            "请判别本图并读取信息。\n"
            "id_type 只能是: HKID（香港身份证）、PRC_ID（中国大陆居民身份证）、"
            "PASSPORT（护照）、unknown（确非证件，如风景/聊天截图）。\n"
            "side: front（人像/照片面）/ back（国徽面、机读码面、非人像反面）；"
            "护照资料页填 front 或空字符串。国徽/「居民身份证」背面务必 side=back。\n"
            "full_name: 证件上的中文或英文姓名；反面读不到则空字符串。\n"
            "id_number: 证件号码；反面尽量读机读码中的号码，读不清则空字符串。\n"
            "confidence: 0~1，表示对「证件类型与正反面分类」的把握。\n"
            "is_handheld: 是否有人手持证件拍照。\n"
            '输出格式: {"id_type":"PRC_ID","side":"front","full_name":"张三",'
            '"id_number":"110101199001011234","confidence":0.9,"is_handheld":false}'
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
            if result.id_type != ID_TYPE_UNKNOWN and result.id_number and not validate_id_number(
                result.id_type, result.id_number
            ):
                result.id_number = ""
                result.error = "number_invalid"
            elif result.confidence < _min_confidence() and not (
                result.side == "back" and result.confidence >= 0.4
            ):
                result.error = result.error or "low_confidence"
            else:
                result.error = result.error or "unreliable"
        logger.info(
            "证件视觉识别 type=%s ok=%s conf=%.2f side=%s name=%s field=%s err=%s",
            result.id_type,
            result.ok,
            result.confidence,
            result.side,
            (result.full_name or "")[:8],
            result.file_field_key,
            result.error,
        )
        return result
    except Exception as exc:
        logger.warning("证件视觉识别失败: %s", exc)
        return IdDocumentResult(error=str(exc)[:200])
