"""证件类型：LLM 判定 + 入库归一 + NNC1-3.1 双栏填写映射。

类型判定以大模型为主；正则只抽号码 / LLM 失败时弱兜底（看标签，不靠号码形态当主判据）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

ICRIS_ID_TYPES = ("HKID", "PRC_ID", "PASSPORT")

# 与 LLMClient.classify_id_document_text 共用；测试断言 prompt 覆盖三种粘贴标签
CLASSIFY_ID_SYSTEM = (
    "你是香港公司注册资料助手。根据客户粘贴的文字，判断董事/申请人使用的证件类型。"
    '只输出 JSON：{"id_type":"HKID"|"PRC_ID"|"PASSPORT","id_number":"..."}。'
    "规则："
    "1) 标签「香港身份证号码」「香港身分證號碼」「香港身份证」等 → HKID。"
    "港证校验位可能写成（2）或 (2)，id_number 须保留校验位。"
    "2) 标签「护照号码」「護照號碼」「护照号」→ PASSPORT。"
    "3) 标签「身份证号码」「居民身份证」且不是香港身份证 → PRC_ID（内地证）。"
    "4) 有多条时取最明确的一条：香港身份证 / 护照优先于笼统的「身份证」。"
    "5) 同时看标签语义和号码；不要只用号码形态猜测。"
    "6) 不要输出其它键或解释。"
)

_TYPE_ALIASES = {
    "HKID": "HKID",
    "HK": "HKID",
    "HK_ID": "HKID",
    "HONGKONG": "HKID",
    "HONG_KONG": "HKID",
    "PRC_ID": "PRC_ID",
    "PRCID": "PRC_ID",
    "PRC": "PRC_ID",
    "CN_ID": "PRC_ID",
    "CNID": "PRC_ID",
    "CHINA_ID": "PRC_ID",
    "CHINAID": "PRC_ID",
    "PASSPORT": "PASSPORT",
    "PPT": "PASSPORT",
    "PP": "PASSPORT",
}


def classify_id_user_prompt(text: str, id_number: str = "") -> str:
    blob = (text or "").strip()
    num = (id_number or "").strip()
    parts = ["请判定证件类型。"]
    if blob:
        parts.append(f"粘贴资料:\n{blob}")
    if num:
        parts.append(f"当前证件号码: {num}")
    if not blob and not num:
        parts.append("（无粘贴资料、无号码）")
    return "\n\n".join(parts)


def alias_id_type(raw: str) -> str:
    """只做别名映射，不猜号码、不默认 PRC_ID。"""
    original = (raw or "").strip()
    if not original:
        return ""
    t = original.upper().replace("-", "_").replace(" ", "")
    t_compact = re.sub(r"[\s_\-]+", "", original.upper())
    if t in _TYPE_ALIASES:
        return _TYPE_ALIASES[t]
    if t_compact in _TYPE_ALIASES:
        return _TYPE_ALIASES[t_compact]
    for key, val in _TYPE_ALIASES.items():
        if key and len(key) >= 4 and key in t:
            return val
    return ""


def coerce_classify_result(data: Any, fallback_number: str = "") -> dict[str, str]:
    if not isinstance(data, dict):
        return {"id_type": "", "id_number": (fallback_number or "").strip()}
    raw_type = str(data.get("id_type") or "").strip()
    id_type = alias_id_type(raw_type)
    if id_type not in ICRIS_ID_TYPES:
        id_type = ""
    id_number = str(data.get("id_number") or fallback_number or "").strip()
    return {"id_type": id_type, "id_number": id_number}


def weak_fallback_id_type(text: str, id_number: str = "") -> dict[str, str]:
    """LLM 失败时的弱兜底：优先看粘贴标签，号码形态只作最后提示。"""
    blob = text or ""
    num = (id_number or "").strip()
    extracted = num
    if not extracted:
        m = re.search(
            r"(?:香港身份证号码|香港身分證號碼|护照号码|護照號碼|身份证号码|身分證號碼)"
            r"\s*[:：]\s*(\S+)",
            blob,
        )
        if m:
            extracted = m.group(1).strip()

    if re.search(r"香港身份证|香港身分證|香港身分证", blob):
        return {"id_type": "HKID", "id_number": extracted}
    if re.search(r"护照号码|護照號碼|护照号|護照號", blob):
        return {"id_type": "PASSPORT", "id_number": extracted}
    if re.search(r"身份证号码|身分證號碼|身份证号|身分证号码", blob):
        return {"id_type": "PRC_ID", "id_number": extracted}

    guessed = _weak_guess_from_number(extracted)
    if guessed:
        logger.warning(
            "id_type 弱兜底：无标签，按号码形态 %s num=%s",
            guessed,
            extracted[:12],
        )
        return {"id_type": guessed, "id_number": extracted}
    return {"id_type": "", "id_number": extracted}


def _weak_guess_from_number(id_number: str) -> str:
    """空类型时的号码弱兜底。18 位内地证（含 X）不得当成护照。"""
    num = (id_number or "").strip()
    if not num:
        return ""
    if re.match(r"^\d{17}[\dXx]$", num):
        return "PRC_ID"
    compact = num.replace("（", "(").replace("）", ")")
    if re.match(r"^[A-Z]{1,2}\d{6}\(?[A0-9]\)?$", compact, re.I):
        return "HKID"
    return ""


def normalize_stored_id_type(
    raw: str,
    id_number: str = "",
    *,
    allow_number_fallback: bool = True,
) -> str:
    """库里已有类型时尊重库；空类型才弱兜底。号码形态不能单独当主判据。"""
    original = (raw or "").strip()
    aliased = alias_id_type(original)
    if aliased:
        return aliased

    if not original and allow_number_fallback:
        guessed = _weak_guess_from_number(id_number)
        if guessed:
            logger.warning(
                "id_type 为空，弱兜底=%s num=%s",
                guessed,
                (id_number or "")[:12],
            )
            return guessed
        return "PRC_ID"

    if original:
        logger.warning("id_type 无法归一 raw=%r，回退 PRC_ID", original)
        return "PRC_ID"
    return "PRC_ID"


def split_hkid_number(id_number: str) -> tuple[str, str]:
    """F570235（2）/ F570235(2) / F5702352 → (F570235, 2)。"""
    s = (id_number or "").strip().upper().replace("（", "(").replace("）", ")")
    m = re.match(r"^([A-Z]{1,2}\d{6})\(?([A0-9])\)?$", s)
    if m:
        return m.group(1), m.group(2)
    return s, ""


def nnc1_identity_fill_plan(id_type: str, id_number: str) -> dict[str, str]:
    """NNC1-3.1：港证填港证栏、护照栏無；非港证港证栏無、护照栏填号。"""
    t = normalize_stored_id_type(id_type, id_number)
    num = (id_number or "").strip()
    if t == "HKID":
        return {
            "hkid": num or "無",
            "passport": "無",
            "passport_country": "",
        }
    return {
        "hkid": "無",
        "passport": num or "無",
        "passport_country": "中国" if t in ("PRC_ID", "PASSPORT") else "",
    }


def s03_id_type_select_pattern(id_type: str) -> str:
    """s03 证件类型下拉：同时匹配 value 码与中文标签。"""
    t = normalize_stored_id_type(id_type, allow_number_fallback=False) or id_type
    return {
        "HKID": r"HKID|香港身分證|香港身分证|香港身份证",
        "PRC_ID": r"PRC_ID|中华人民共和国|中華人民共和國|内地身份证|內地身分",
        "PASSPORT": r"PASSPORT|护照|護照",
    }.get(t, t)


def classify_id_from_text(
    text: str,
    id_number: str = "",
    *,
    llm: Any | None = None,
) -> dict[str, str]:
    """LLM 判定类型；失败才弱兜底。"""
    try:
        client = llm
        if client is None:
            from src.llm.openai_client import LLMClient

            client = LLMClient()
        if hasattr(client, "classify_id_document_text"):
            data = client.classify_id_document_text(text, id_number)
        else:
            data = client.chat_json(
                CLASSIFY_ID_SYSTEM,
                classify_id_user_prompt(text, id_number),
                temperature=0.0,
            )
        result = coerce_classify_result(data, id_number)
        if result.get("id_type") in ICRIS_ID_TYPES:
            return result
        logger.warning("LLM 证件分类结果无效: %s", data)
    except Exception as exc:
        logger.warning("LLM 证件分类失败: %s", exc)

    fb = weak_fallback_id_type(text, id_number)
    logger.warning(
        "证件类型改用弱兜底 id_type=%s num=%s",
        fb.get("id_type"),
        (fb.get("id_number") or "")[:12],
    )
    return fb
