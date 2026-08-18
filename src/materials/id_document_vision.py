"""身份证明图片：多模态识别证件类型 / 正反面 / 姓名 / 号码 / 住址"""

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
ID_TYPE_TW = "TW_ID"
ID_TYPE_UNKNOWN = "unknown"

ID_TYPE_LABELS = {
    ID_TYPE_HKID: "香港身份证",
    ID_TYPE_PRC: "中华人民共和国身份证",
    ID_TYPE_PASSPORT: "护照",
    ID_TYPE_TW: "台湾身份证",
}

ISSUING_CHN = "CHN"
ISSUING_HKG = "HKG"
ISSUING_TWN = "TWN"
ISSUING_OTHER = "OTHER"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_PRC_RE = re.compile(r"^\d{17}[\dXx]$")
_HKID_RE = re.compile(r"^[A-Z]{1,2}\d{6}\(?[\dA]\)?$", re.I)
# 从脏字符串中抓取香港身份证号（含括号校验位 / 无括号 7 位尾）
_HKID_FIND = re.compile(
    r"[A-Z]{1,2}\s*\d{6}\s*(?:\(?\s*[\dA]\s*\)?|\d)",
    re.I,
)
_PASSPORT_RE = re.compile(r"^[A-Z0-9]{5,15}$", re.I)
_TW_ID_RE = re.compile(r"^[A-Z][12]\d{8}$", re.I)
_LATIN_NAME_RE = re.compile(r"[A-Za-z]")

# 身份证末位 X 常见误识字符 → 标准 X
_X_LOOKALIKES = str.maketrans(
    {
        "ｘ": "X",
        "Ｘ": "X",
        "×": "X",
        "✕": "X",
        "х": "X",  # Cyrillic
        "Х": "X",
        "ⅹ": "X",
        "*": "X",
    }
)


def _min_confidence() -> float:
    return float(getattr(settings, "materials_id_min_confidence", 0.55) or 0.55)


def _to_bool(val: Any) -> bool:
    """宽松归一为布尔：true/1/yes/是 → True。"""
    if isinstance(val, bool):
        return val
    s = str(val or "").strip().lower()
    return s in ("true", "1", "yes", "y", "是", "handheld")


def _has_latin(s: str) -> bool:
    return bool(_LATIN_NAME_RE.search(s or ""))


def display_name(name_cn: str, name_en: str, full_name: str = "") -> str:
    """可读展示名：中英都有则拼接，否则取一侧或 full_name。"""
    cn = (name_cn or "").strip()
    en = (name_en or "").strip()
    if cn and en:
        return f"{cn} {en}".strip()
    return cn or en or (full_name or "").strip()


@dataclass
class IdDocumentResult:
    id_type: str = ID_TYPE_UNKNOWN
    id_number: str = ""
    full_name: str = ""
    name_cn: str = ""
    name_en: str = ""
    address_cn: str = ""
    issuing_country: str = ""
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
        if self.id_type == ID_TYPE_TW:
            return "taiwan_id"
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

    def to_admin_fields(self) -> dict[str, str]:
        """管理后台可回填的字段（空值省略）。"""
        out: dict[str, str] = {}
        # TW_ID 不是 ICRIS id_type，仅作住址来源
        if self.id_type in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_PASSPORT):
            out["id_type"] = self.id_type
        if self.id_number:
            out["id_number"] = self.id_number
        if self.name_cn:
            out["director_name_cn"] = self.name_cn
        if self.name_en:
            out["director_name_en"] = self.name_en
        shown = display_name(self.name_cn, self.name_en, self.full_name)
        if shown:
            out["director_name"] = shown
        if self.address_cn:
            out["director_address_cn"] = self.address_cn
        if self.issuing_country:
            out["issuing_country"] = self.issuing_country
        return out


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
    t = (id_type or "").upper()
    num = normalize_id_number(t, id_number)
    if not num:
        return False
    if t == ID_TYPE_PRC:
        return bool(_PRC_RE.match(num))
    if t == ID_TYPE_HKID:
        return bool(_HKID_RE.match(num))
    if t == ID_TYPE_PASSPORT:
        return bool(_PASSPORT_RE.match(num))
    if t == ID_TYPE_TW:
        return bool(_TW_ID_RE.match(num)) or (
            len(num) >= 8 and len(num) <= 12 and num.isalnum()
        )
    return False


def _to_halfwidth(s: str) -> str:
    out: list[str] = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _format_hkid(raw: str) -> str:
    """把各种 OCR/视觉脏串整理成 A123456(7) / AB123456(A) 形式。"""
    s = _to_halfwidth(raw or "")
    s = (
        s.upper()
        .replace("（", "(")
        .replace("）", ")")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace(" ", "")
        .replace("\u3000", "")
        .replace("-", "")
        .replace("_", "")
        .replace(".", "")
        .replace("·", "")
        .replace(":", "")
        .replace("：", "")
        .replace("號", "")
        .replace("号", "")
        .replace("碼", "")
        .replace("码", "")
    )
    # 去掉常见前缀，避免 No.A123456 → OA123456
    s = re.sub(r"^(?:NO|NR|ID|HKID|HK|身份证|身分證|香港)+", "", s)
    # 在串中找所有候选，优先「非字母紧前」的匹配
    candidates: list[str] = []
    for m in re.finditer(r"[A-Z]{1,2}\d{6}\(?[\dA]\)?", s, re.I):
        start = m.start()
        # 仅当紧前是拉丁字母时跳过（避免 No.A… → OA…）；中文前缀允许
        if start > 0 and ("A" <= s[start - 1] <= "Z"):
            continue
        candidates.append(m.group(0))
    token = candidates[0] if candidates else ""
    if not token:
        m2 = re.search(r"([A-Z]{1,2})(\d{6})([\dA])?", s)
        if not m2:
            return s
        if m2.start() > 0 and ("A" <= s[m2.start() - 1] <= "Z"):
            return s
        prefix, digits, check = m2.group(1), m2.group(2), m2.group(3) or ""
        if check:
            return f"{prefix}{digits}({check})"
        return f"{prefix}{digits}"
    m3 = re.match(r"^([A-Z]{1,2})(\d{6})\(?([\dA])\)?$", token, re.I)
    if m3:
        prefix, digits, check = m3.group(1).upper(), m3.group(2), m3.group(3).upper()
        return f"{prefix}{digits}({check})"
    m4 = re.match(r"^([A-Z]{1,2})(\d{6})(\d)$", token, re.I)
    if m4:
        return f"{m4.group(1).upper()}{m4.group(2)}({m4.group(3)})"
    m5 = re.match(r"^([A-Z]{1,2})(\d{6})$", token, re.I)
    if m5:
        return f"{m5.group(1).upper()}{m5.group(2)}"
    return token.upper()


def normalize_id_number(id_type: str, id_number: str) -> str:
    """归一化证件号；内地证处理末位 X；港证整理括号与脏前缀。"""
    raw = _to_halfwidth(id_number or "")
    raw = (
        raw.replace(" ", "")
        .replace("\u3000", "")
        .replace("-", "")
        .replace("_", "")
        .replace(".", "")
        .replace("·", "")
    )
    t = (id_type or "").upper()
    if t == ID_TYPE_HKID:
        return _format_hkid(id_number or "")
    if t == ID_TYPE_PRC or (not t and re.search(r"\d{17}", raw)):
        cand = raw.translate(_X_LOOKALIKES).upper()
        m = re.search(r"\d{17}[\dX]", cand)
        if m:
            return m.group(0)
        if re.fullmatch(r"\d{17}", cand):
            return cand
        return cand
    if t in (ID_TYPE_PASSPORT, ID_TYPE_TW):
        return raw.upper()
    # 未知类型：优先尝试港证 / 内地证形态
    hkid = _format_hkid(id_number or "")
    if _HKID_RE.match(hkid):
        return hkid
    return raw.upper() if raw else ""


def salvage_id_number(id_type: str, id_number: str) -> str:
    """校验失败时尽量抢救号码（内地证末位 X / 港证括号格式）。"""
    t = (id_type or "").upper()
    if t == ID_TYPE_HKID:
        n = _format_hkid(id_number or "")
        if _HKID_RE.match(n):
            return n
        # 宽松：至少字母+6位数字
        m = re.search(r"([A-Z]{1,2}\d{6})", n, re.I)
        if m:
            base = m.group(1).upper()
            # 尝试带上紧随的校验位
            rest = n[m.end() :]
            cm = re.match(r"^\(?([\dA])\)?", rest, re.I)
            if cm:
                return f"{base}({cm.group(1).upper()})"
            return base
        return n if n else ""
    n = normalize_id_number(t, id_number)
    if n and validate_id_number(t, n):
        return n
    if not t or t == ID_TYPE_UNKNOWN:
        n_hk = salvage_id_number(ID_TYPE_HKID, id_number)
        if n_hk and (_HKID_RE.match(n_hk) or re.match(r"^[A-Z]{1,2}\d{6}", n_hk)):
            return n_hk
        n2 = normalize_id_number(ID_TYPE_PRC, id_number)
        if validate_id_number(ID_TYPE_PRC, n2):
            return n2
    return n if n else ""


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


def _normalize_issuing(raw: str, id_type: str) -> str:
    s = (raw or "").strip().upper()
    aliases = {
        "CN": ISSUING_CHN,
        "CHINA": ISSUING_CHN,
        "PRC": ISSUING_CHN,
        "CHN": ISSUING_CHN,
        "HK": ISSUING_HKG,
        "HKG": ISSUING_HKG,
        "HONGKONG": ISSUING_HKG,
        "HONG_KONG": ISSUING_HKG,
        "TW": ISSUING_TWN,
        "TWN": ISSUING_TWN,
        "TAIWAN": ISSUING_TWN,
        "ROC": ISSUING_TWN,
    }
    if s in aliases:
        return aliases[s]
    if s in (ISSUING_CHN, ISSUING_HKG, ISSUING_TWN, ISSUING_OTHER):
        return s
    if id_type == ID_TYPE_PRC:
        return ISSUING_CHN
    if id_type == ID_TYPE_HKID:
        return ISSUING_HKG
    if id_type == ID_TYPE_TW:
        return ISSUING_TWN
    return s or ""


def _parse_vision_payload(data: dict[str, Any]) -> IdDocumentResult:
    raw_type = str(data.get("id_type") or "").strip().upper()
    if raw_type in ("CN_ID", "CHINA_ID", "MAINLAND", "PRC"):
        raw_type = ID_TYPE_PRC
    if raw_type in ("HK", "HK_ID", "HONGKONG"):
        raw_type = ID_TYPE_HKID
    if raw_type in ("PP", "PASSPORT"):
        raw_type = ID_TYPE_PASSPORT
    if raw_type in ("TW", "TW_ID", "TAIWAN_ID", "ROC_ID", "TAIWAN"):
        raw_type = ID_TYPE_TW
    if raw_type not in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_PASSPORT, ID_TYPE_TW):
        raw_type = ID_TYPE_UNKNOWN

    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    raw_id_number = str(data.get("id_number") or "")
    number = normalize_id_number(raw_type, raw_id_number)
    if number and not validate_id_number(raw_type or ID_TYPE_UNKNOWN, number):
        # 按已知/推断类型抢救（港证优先于误走内地证清空）
        salvage_as = raw_type if raw_type != ID_TYPE_UNKNOWN else ID_TYPE_UNKNOWN
        salvaged = salvage_id_number(salvage_as, raw_id_number)
        if salvaged:
            number = salvaged
    if number and raw_type == ID_TYPE_UNKNOWN:
        if _PRC_RE.match(number):
            raw_type = ID_TYPE_PRC
        elif _HKID_RE.match(number) or re.match(r"^[A-Z]{1,2}\d{6}", number, re.I):
            raw_type = ID_TYPE_HKID
            number = salvage_id_number(ID_TYPE_HKID, number) or number
        elif _TW_ID_RE.match(number):
            raw_type = ID_TYPE_TW

    name_cn = str(data.get("name_cn") or "").strip()
    name_en = str(data.get("name_en") or "").strip()
    full_name = str(data.get("full_name") or data.get("name") or "").strip()
    if not name_cn and not name_en and full_name:
        # 兼容旧字段：按是否含拉丁粗分
        if _has_latin(full_name) and not re.search(r"[\u4e00-\u9fff]", full_name):
            name_en = full_name
        elif re.search(r"[\u4e00-\u9fff]", full_name) and not _has_latin(full_name):
            name_cn = full_name
        else:
            # 混合：CJK 与拉丁拆分
            cjk = "".join(
                c for c in full_name if not c.isascii() or c in "·•、・"
            ).strip()
            latin = "".join(c for c in full_name if c.isascii()).strip()
            name_cn = cjk or name_cn
            name_en = latin or name_en
    if not full_name:
        full_name = display_name(name_cn, name_en)

    address_cn = str(
        data.get("address_cn") or data.get("address") or data.get("住址") or ""
    ).strip()
    if address_cn and raw_type in (ID_TYPE_PRC, ID_TYPE_TW, ID_TYPE_UNKNOWN, ""):
        from src.materials.id_document_translate import repair_prc_address_ocr

        address_cn = repair_prc_address_ocr(address_cn)
    issuing_country = _normalize_issuing(
        str(data.get("issuing_country") or data.get("nationality") or ""),
        raw_type,
    )

    side = str(data.get("side") or "").strip().lower()
    if side not in ("front", "back"):
        side = ""
    is_handheld = _to_bool(data.get("is_handheld"))

    number_ok = bool(number) and (
        raw_type == ID_TYPE_UNKNOWN or validate_id_number(raw_type, number)
    )
    # 港证：校验失败再抢救，至少保留字母+6位数字
    if (not number_ok) and raw_type in (ID_TYPE_HKID, ID_TYPE_UNKNOWN, ""):
        salvaged = salvage_id_number(ID_TYPE_HKID, raw_id_number or number)
        if salvaged and (
            validate_id_number(ID_TYPE_HKID, salvaged)
            or re.match(r"^[A-Z]{1,2}\d{6}", salvaged, re.I)
        ):
            number = salvaged
            if raw_type in (ID_TYPE_UNKNOWN, ""):
                raw_type = ID_TYPE_HKID
            number_ok = True
    # 内地证：末位 X
    if (not number_ok) and raw_type in (ID_TYPE_PRC, ID_TYPE_UNKNOWN, ""):
        salvaged = salvage_id_number(ID_TYPE_PRC, raw_id_number or number)
        if salvaged and validate_id_number(ID_TYPE_PRC, salvaged):
            number = salvaged
            if raw_type in (ID_TYPE_UNKNOWN, ""):
                raw_type = ID_TYPE_PRC
            number_ok = True
    if number and not number_ok:
        # 港证宽松保留；内地证宽松保留；否则清空
        if raw_type == ID_TYPE_HKID and re.match(r"^[A-Z]{1,2}\d{6}", number, re.I):
            number_ok = True
        elif re.fullmatch(r"\d{17}[\dX]", normalize_id_number(ID_TYPE_PRC, raw_id_number or number) or ""):
            number = normalize_id_number(ID_TYPE_PRC, raw_id_number or number)
            number_ok = True
            if raw_type in (ID_TYPE_UNKNOWN, ""):
                raw_type = ID_TYPE_PRC
        else:
            # 仍保留原始可读串供展示（避免港证被整段丢掉）
            keep = salvage_id_number(raw_type or ID_TYPE_HKID, raw_id_number) or raw_id_number.strip()
            if raw_type == ID_TYPE_HKID and keep:
                number = _format_hkid(keep)
                number_ok = bool(number)
            else:
                number = ""

    classified = raw_type != ID_TYPE_UNKNOWN or side == "back"
    conf_ok = conf >= _min_confidence() or (
        side == "back" and conf >= max(0.4, _min_confidence() - 0.15)
    )
    ok = bool(
        classified
        and conf_ok
        and (number_ok or side == "back" or (raw_type == ID_TYPE_PASSPORT and number_ok))
    )
    if raw_type in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_TW) and side != "back" and not number_ok:
        ok = False
    if raw_type == ID_TYPE_PASSPORT and not number_ok:
        ok = False

    return IdDocumentResult(
        id_type=raw_type,
        id_number=number if number_ok else "",
        full_name=full_name,
        name_cn=name_cn,
        name_en=name_en,
        address_cn=address_cn,
        issuing_country=issuing_country,
        confidence=conf,
        ok=ok,
        raw=data,
        side=side,
        is_handheld=is_handheld,
    )


def recognize_id_document(
    image_bytes: bytes,
    *,
    filename: str = "",
    expected_id_type: str = "",
) -> IdDocumentResult:
    """多模态 LLM：分类证件并抽取姓名/号码/住址等。

    expected_id_type: 用户指定类型时写入提示，优先按该类型抽取。
    """
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

        expected = (expected_id_type or "").strip().upper()
        expect_hint = ""
        if expected in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_PASSPORT, ID_TYPE_TW):
            expect_hint = (
                f"用户已指定本图为 {ID_TYPE_LABELS.get(expected, expected)}（id_type={expected}），"
                f"请按该类型抽取字段；若明显不是该证件仍可输出真实类型。\n"
            )

        system = (
            "你是香港公司注册材料助手。对每张证件图片必须完成判别并抽取结构化字段。"
            "只输出 JSON，不要解释。"
            "中国大陆/香港身份证的国徽面、机读码面（反面）也是有效身份证明；"
            "即使看不见人像，也应输出对应 id_type，并将 side 设为 back。"
        )
        user_text = (
            expect_hint
            + "请判别本图并读取信息。\n"
            "id_type 只能是:\n"
            "- HKID（香港身份证）\n"
            "- PRC_ID（中国大陆居民身份证）\n"
            "- PASSPORT（护照资料页）\n"
            "- TW_ID（台湾国民身份证）\n"
            "- unknown（确非证件）\n"
            "side: front（人像/照片面）/ back（国徽面、机读码面）；"
            "护照资料页填 front 或空字符串。\n"
            "按类型抽取：\n"
            "- PRC_ID: name_cn=中文姓名；id_number=18位身份证号（末位可能是数字或大写字母X，"
            "必须完整输出，勿漏写X，勿写成小写x或其它符号）；"
            "address_cn=住址中文（正面优先）：必须按证件原文逐字抄录，禁止漏字/改字；"
            "若住址分两行印刷，必须拼成一行完整地址，行末字与下行首字都要保留"
            "（例如上行以「西丽」结尾、下行以「南路」开头时，结果须含「西丽南路」，"
            "不得写成「丽南路」）；反光遮挡处尽量根据可见笔画推断，仍不确定则照可见部分抄录\n"
            "- HKID: name_cn=中文名；name_en=英文名；"
            "id_number=香港身份证号码，格式如 A123456(7) 或 AB123456(A)，必须含字母前缀+6位数字+校验位，"
            "校验位用半角括号包裹；勿漏读号码\n"
            "- PASSPORT: name_en=护照英文名；name_cn=中文名可空；id_number=护照号；"
            "issuing_country=CHN/HKG/TWN/OTHER（台湾护照务必 TWN）\n"
            "- TW_ID: name_cn；id_number；address_cn=户籍/住址中文"
            "（同样逐字抄录，多行住址拼成一行）\n"
            "full_name: 兼容字段，可用 name_cn/name_en 拼接。\n"
            "confidence: 0~1；is_handheld: 是否手持证件。\n"
            "输出 JSON 示例: "
            '{"id_type":"PRC_ID","side":"front","name_cn":"张三","name_en":"",'
            '"full_name":"张三","id_number":"110101199001011234",'
            '"address_cn":"北京市东城区…","issuing_country":"CHN",'
            '"confidence":0.9,"is_handheld":false}'
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
            if (
                result.id_type != ID_TYPE_UNKNOWN
                and result.id_number
                and not validate_id_number(result.id_type, result.id_number)
            ):
                # 港证宽松：字母+6位数字仍保留展示
                if result.id_type == ID_TYPE_HKID and re.match(
                    r"^[A-Z]{1,2}\d{6}", result.id_number, re.I
                ):
                    result.id_number = salvage_id_number(ID_TYPE_HKID, result.id_number) or result.id_number
                else:
                    fixed = salvage_id_number(result.id_type, result.id_number)
                    if fixed and (
                        validate_id_number(result.id_type, fixed)
                        or (
                            result.id_type == ID_TYPE_HKID
                            and re.match(r"^[A-Z]{1,2}\d{6}", fixed, re.I)
                        )
                    ):
                        result.id_number = fixed
                    else:
                        result.id_number = ""
                        result.error = "number_invalid"
            elif result.confidence < _min_confidence() and not (
                result.side == "back" and result.confidence >= 0.4
            ):
                result.error = result.error or "low_confidence"
            else:
                result.error = result.error or "unreliable"
        logger.info(
            "证件视觉识别 type=%s ok=%s conf=%.2f side=%s name=%s addr=%s country=%s field=%s err=%s",
            result.id_type,
            result.ok,
            result.confidence,
            result.side,
            (result.full_name or "")[:8],
            bool(result.address_cn),
            result.issuing_country,
            result.file_field_key,
            result.error,
        )
        return result
    except Exception as exc:
        logger.warning("证件视觉识别失败: %s", exc)
        return IdDocumentResult(error=str(exc)[:200])
