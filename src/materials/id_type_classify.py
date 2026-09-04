"""证件类型：只根据证件标签 + 号码判定（不看住址/注册地址）。"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.materials.zh_norm import to_hans

logger = logging.getLogger(__name__)

ICRIS_ID_TYPES = ("HKID", "PRC_ID", "PASSPORT")

# 证件标签行（匹配用简体；原文经 to_hans 后再搜）
_ID_LABEL_LINE_RE = re.compile(
    r"香港身份证|"
    r"身份证号码|身份证号|"
    r"护照号码|护照号|"
    r"证件号"
)

CLASSIFY_ID_SYSTEM = (
    "你只根据「证件标签 + 号码」判断类型，不要看住址或注册地址。"
    '只输出 JSON：{"id_type":"HKID"|"PRC_ID"|"PASSPORT","id_number":"..."}。'
    "规则："
    "1) 标签「香港身份证号码」「香港身分證號碼」「香港身份证」等 → HKID。"
    "港证校验位可能写成（2）或 (2)，id_number 须保留校验位。"
    "2) 标签「护照号码」「護照號碼」「護照号码」「护照号」→ PASSPORT。"
    "3) 标签「身份证号码」「居民身份证」且不是香港身份证 → PRC_ID（内地证）。"
    "4) 有多条时：香港身份证 / 护照优先于笼统的「身份证」。"
    "5) 注册地址或住址里出现「香港」不能当作港证。"
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


def extract_id_label_lines(text: str) -> str:
    """只保留含证件标签的行，去掉住址/注册地址等。"""
    lines: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _ID_LABEL_LINE_RE.search(to_hans(line)):
            lines.append(line)
    return "\n".join(lines)


def classify_id_user_prompt(text: str, id_number: str = "") -> str:
    snippet = extract_id_label_lines(text)
    num = (id_number or "").strip()
    parts = ["请只根据证件标签与号码判定类型，忽略地址。"]
    if snippet:
        parts.append(f"证件相关行:\n{snippet}")
    if num:
        parts.append(f"当前证件号码: {num}")
    if not snippet and not num:
        parts.append("（无证件标签行、无号码）")
    return "\n\n".join(parts)


def alias_id_type(raw: str) -> str:
    """只做别名映射，不猜号码、不做子串误伤。"""
    original = (raw or "").strip()
    if not original:
        return ""
    t = original.upper().replace("-", "_").replace(" ", "")
    t_compact = re.sub(r"[\s_\-]+", "", original.upper())
    if t in _TYPE_ALIASES:
        return _TYPE_ALIASES[t]
    if t_compact in _TYPE_ALIASES:
        return _TYPE_ALIASES[t_compact]
    return ""


def refine_id_type(text: str, id_number: str = "", llm_type: str = "") -> str:
    """主判据：证件标签行。住址/注册地址不参与。"""
    snippet = extract_id_label_lines(text)
    hans = to_hans(snippet)
    if re.search(r"香港身份证", hans):
        return "HKID"
    if re.search(r"护照号码|护照号", hans):
        return "PASSPORT"
    if re.search(r"身份证号码|身份证号", hans):
        return "PRC_ID"
    aliased = alias_id_type(llm_type)
    if aliased in ICRIS_ID_TYPES:
        return aliased
    return ""


def coerce_classify_result(
    data: Any, fallback_number: str = "", source_text: str = ""
) -> dict[str, str]:
    if not isinstance(data, dict):
        raw_type, id_number = "", (fallback_number or "").strip()
    else:
        raw_type = str(data.get("id_type") or "").strip()
        id_number = str(data.get("id_number") or fallback_number or "").strip()
    id_type = refine_id_type(source_text, id_number, raw_type)
    if id_type not in ICRIS_ID_TYPES:
        id_type = ""
    return {"id_type": id_type, "id_number": id_number}


def weak_fallback_id_type(text: str, id_number: str = "") -> dict[str, str]:
    """只看证件标签行，不看住址。"""
    snippet = extract_id_label_lines(text)
    num = (id_number or "").strip()
    extracted = num
    if not extracted:
        m = re.search(
            r"(?:香港身份证号码|护照号码|身份证号码)\s*[:：]\s*(\S+)",
            to_hans(snippet),
        )
        if m:
            extracted = m.group(1).strip()
    refined = refine_id_type(text, extracted, "")
    if refined:
        return {"id_type": refined, "id_number": extracted}
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


def s04_identity_fill_values(
    id_type: str, id_number: str, issuing_country: str = ""
) -> dict[str, str]:
    """s04：港证拆主体/校验位；护照填号+签发国 ISO；内地证整号单框。"""
    from src.materials.countries import normalize_issuing_iso

    t = normalize_stored_id_type(id_type, id_number)
    num = (id_number or "").strip()
    if t == "HKID":
        main, check = split_hkid_number(num)
        return {
            "id_type": "HKID",
            "hkid_main": main,
            "hkid_check": check,
            "number": "",
            "passport_country_iso": "",
        }
    if t == "PASSPORT":
        iso = normalize_issuing_iso(issuing_country)
        return {
            "id_type": "PASSPORT",
            "hkid_main": "",
            "hkid_check": "",
            "number": num,
            "passport_country_iso": iso,
        }
    return {
        "id_type": "PRC_ID",
        "hkid_main": "",
        "hkid_check": "",
        "number": num,
        "passport_country_iso": "",
    }


def nnc1_identity_fill_plan(
    id_type: str, id_number: str, issuing_country: str = ""
) -> dict[str, str]:
    """NNC1-3.1：港证填港证栏、护照栏無；非港证港证栏無、护照栏填号。"""
    from src.materials.countries import passport_country_option_names

    t = normalize_stored_id_type(id_type, id_number)
    num = (id_number or "").strip()
    if t == "HKID":
        return {
            "hkid": num or "無",
            "passport": "無",
            "passport_country": "",
        }
    names = passport_country_option_names(issuing_country)
    return {
        "hkid": "無",
        "passport": num or "無",
        "passport_country": names[0] if names else "中國",
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
    """只把证件标签行交给 LLM；标签规则覆盖误判。"""
    snippet = extract_id_label_lines(text)
    try:
        client = llm
        if client is None:
            from src.llm.openai_client import LLMClient

            client = LLMClient()
        if hasattr(client, "classify_id_document_text"):
            data = client.classify_id_document_text(snippet, id_number)
        else:
            data = client.chat_json(
                CLASSIFY_ID_SYSTEM,
                classify_id_user_prompt(snippet, id_number),
                temperature=0.0,
            )
        result = coerce_classify_result(data, id_number, source_text=text)
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
