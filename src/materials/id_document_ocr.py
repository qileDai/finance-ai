"""证件号码 OCR 兜底（复用 ddddocr；不替代中文姓名识别）"""

from __future__ import annotations

import logging
import re
from typing import Optional

from config.settings import settings
from src.materials.id_document_vision import (
    ID_TYPE_HKID,
    ID_TYPE_PASSPORT,
    ID_TYPE_PRC,
    ID_TYPE_TW,
    ID_TYPE_UNKNOWN,
    normalize_id_number,
    validate_id_number,
)

logger = logging.getLogger(__name__)

_PRC_FIND = re.compile(r"\d{17}[\dXxＸｘ×✕\*]")
_HKID_FIND = re.compile(r"[A-Z]{1,2}\d{6}(?:\([\dA]\)|（[\dA]）)?", re.I)
# 台湾身分证：字母 + 1/2 + 8 位；须优先于港证，避免 A123456789 被截成港证
_TW_FIND = re.compile(r"[A-Z][12]\d{8}", re.I)
_PASSPORT_FIND = re.compile(r"\b[A-Z0-9]{8,9}\b", re.I)

_ocr_engine = None
_ocr_failed = False


def _get_ocr():
    global _ocr_engine, _ocr_failed
    if _ocr_failed:
        return None
    if _ocr_engine is not None:
        return _ocr_engine
    try:
        import ddddocr

        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
        return _ocr_engine
    except Exception as e:
        logger.debug("ddddocr 不可用: %s", e)
        _ocr_failed = True
        return None


def extract_id_number_ocr(
    image_bytes: bytes,
    *,
    hint_type: str = "",
) -> tuple[str, str]:
    """从图片 OCR 文本中抓取证件号码。

    返回 (id_type_guess, id_number)；失败返回 ("", "")。
    """
    if not getattr(settings, "materials_id_ocr_fallback", True):
        return "", ""
    if not image_bytes:
        return "", ""
    ocr = _get_ocr()
    if ocr is None:
        return "", ""
    try:
        text = ocr.classification(image_bytes)
    except Exception as e:
        logger.debug("证件 OCR 失败: %s", e)
        return "", ""
    if not text:
        return "", ""
    raw = str(text).replace(" ", "").replace("\n", "")
    hint = (hint_type or "").upper()

    # 优先按提示类型匹配；TW 始终排在 HKID 前，避免误吞
    ordered: list[tuple[str, re.Pattern[str]]] = []
    if hint == ID_TYPE_PRC:
        ordered = [
            (ID_TYPE_PRC, _PRC_FIND),
            (ID_TYPE_TW, _TW_FIND),
            (ID_TYPE_HKID, _HKID_FIND),
        ]
    elif hint == ID_TYPE_HKID:
        ordered = [
            (ID_TYPE_HKID, _HKID_FIND),
            (ID_TYPE_TW, _TW_FIND),
            (ID_TYPE_PRC, _PRC_FIND),
        ]
    elif hint == ID_TYPE_TW:
        ordered = [
            (ID_TYPE_TW, _TW_FIND),
            (ID_TYPE_PRC, _PRC_FIND),
        ]
    elif hint == ID_TYPE_PASSPORT:
        ordered = [
            (ID_TYPE_PASSPORT, _PASSPORT_FIND),
            (ID_TYPE_PRC, _PRC_FIND),
            (ID_TYPE_TW, _TW_FIND),
            (ID_TYPE_HKID, _HKID_FIND),
        ]
    else:
        ordered = [
            (ID_TYPE_PRC, _PRC_FIND),
            (ID_TYPE_TW, _TW_FIND),
            (ID_TYPE_HKID, _HKID_FIND),
            (ID_TYPE_PASSPORT, _PASSPORT_FIND),
        ]

    for itype, pat in ordered:
        m = pat.search(raw)
        if not m:
            continue
        num = normalize_id_number(itype, m.group(0))
        if itype == ID_TYPE_PRC:
            if not validate_id_number(itype, num):
                continue
        if itype == ID_TYPE_PASSPORT:
            if not re.search(r"[A-Za-z]", num):
                continue
        if itype == ID_TYPE_TW:
            if not (
                validate_id_number(itype, num)
                or re.fullmatch(r"[A-Z][12]\d{8}", num or "", re.I)
            ):
                continue
            logger.info("证件 OCR 兜底命中 type=%s num=%s…", itype, (num or "")[:4])
            return itype, num
        if validate_id_number(itype, num) or (
            itype == ID_TYPE_UNKNOWN and num
        ):
            if validate_id_number(itype, num):
                logger.info("证件 OCR 兜底命中 type=%s num=%s…", itype, num[:4])
                return itype, num
    return "", ""


def enrich_number_from_ocr(
    *,
    image_bytes: bytes,
    id_type: str,
    id_number: str,
) -> tuple[str, str]:
    """Vision 无数码时用 OCR 补号；已有合法号码则原样返回。

    已判定为 TW_ID 时：只允许补台号，不得改成 HKID/PRC。
    """
    t = (id_type or "").upper()
    if id_number and validate_id_number(t or ID_TYPE_PRC, id_number):
        return id_type, id_number
    if t == ID_TYPE_TW and id_number:
        n = normalize_id_number(ID_TYPE_TW, id_number)
        if n and (
            validate_id_number(ID_TYPE_TW, n)
            or re.fullmatch(r"[A-Z][12]\d{8}", n, re.I)
            or (len(n) >= 8 and n.isalnum())
        ):
            return ID_TYPE_TW, n
    if id_number and t == ID_TYPE_UNKNOWN:
        for cand in (ID_TYPE_TW, ID_TYPE_PRC, ID_TYPE_HKID, ID_TYPE_PASSPORT):
            n = normalize_id_number(cand, id_number)
            if validate_id_number(cand, n):
                return cand, n
    otype, onum = extract_id_number_ocr(image_bytes, hint_type=id_type)
    if onum:
        if t == ID_TYPE_TW:
            # 已是台证：仅接受 TW 号码，不改类型
            if otype == ID_TYPE_TW or re.fullmatch(
                r"[A-Z][12]\d{8}", onum or "", re.I
            ):
                return ID_TYPE_TW, normalize_id_number(ID_TYPE_TW, onum) or onum
            return id_type, id_number
        return (otype or id_type or ID_TYPE_UNKNOWN), onum
    return id_type, id_number
