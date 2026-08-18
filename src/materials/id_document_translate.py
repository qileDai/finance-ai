"""证件字段翻译：住址中→英、护照英文名补全"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_LATIN_RE = re.compile(r"[A-Za-z]")


def repair_prc_address_ocr(address_cn: str) -> str:
    """极轻量内地证住址 OCR 纠错：仅修已知换行漏字模式。

    南山区 +「丽南路」且无「西丽」→「西丽南路」（西丽/南路被换行拆开时易漏「西」）。
    """
    text = (address_cn or "").strip()
    if not text:
        return text
    if "南山区" in text and "丽南路" in text and "西丽" not in text:
        return text.replace("丽南路", "西丽南路", 1)
    return text


def looks_like_latin_name(name: str) -> bool:
    """是否像可用的拉丁/英文姓名（至少 2 个字母）。"""
    s = (name or "").strip()
    if not s:
        return False
    letters = _LATIN_RE.findall(s)
    return len(letters) >= 2


def translate_address_cn_to_en(address_cn: str) -> str:
    """将中文住址译为英文；禁止编造门牌/楼层。失败返回空串。"""
    text = (address_cn or "").strip()
    if not text:
        return ""
    try:
        from src.llm.openai_client import LLMClient

        client = LLMClient()
        data = client.chat_json(
            system=(
                "你是地址翻译助手。只把给定中文地址忠实译为英文地址，"
                "保留门牌、楼层、室号数字与专有名词拼音/官方英文。"
                "不要编造原文没有的信息；不要输出解释。"
                '只输出 JSON: {"address_en":"..."}'
            ),
            user=f"中文地址：{text}",
            temperature=0.0,
        )
        en = str(data.get("address_en") or data.get("en") or "").strip()
        return en
    except Exception as exc:
        logger.warning("住址翻译失败: %s", exc)
        return ""


def ensure_passport_english_name(name_cn: str, name_en: str) -> str:
    """护照英文名：已有合格拉丁名则原样；否则用中文名译出拼音式英文名。"""
    existing = (name_en or "").strip()
    if looks_like_latin_name(existing):
        return existing
    cn = (name_cn or "").strip()
    if not cn:
        return existing
    try:
        from src.llm.openai_client import LLMClient

        client = LLMClient()
        data = client.chat_json(
            system=(
                "你是护照姓名罗马化助手。把中文姓名转为护照常用的英文拼音姓名"
                "（姓在前大写、名在后，空格分隔，如 ZHANG SAN）。"
                "不要编造无关信息；不要输出解释。"
                '只输出 JSON: {"name_en":"..."}'
            ),
            user=f"中文名：{cn}",
            temperature=0.0,
        )
        en = str(data.get("name_en") or data.get("en") or "").strip()
        return en or existing
    except Exception as exc:
        logger.warning("护照英文名翻译失败: %s", exc)
        return existing


def enrich_extracted_fields(fields: dict[str, str]) -> dict[str, str]:
    """对识别结果做翻译补全（仅填空，不覆盖已有非空值）。

    入参/出参均为管理后台字段键。
    """
    out = {k: (v or "").strip() for k, v in (fields or {}).items() if (v or "").strip()}

    addr_cn = out.get("director_address_cn", "")
    if addr_cn:
        fixed = repair_prc_address_ocr(addr_cn)
        if fixed != addr_cn:
            out["director_address_cn"] = fixed
            addr_cn = fixed
    if addr_cn and not out.get("director_address_en"):
        en = translate_address_cn_to_en(addr_cn)
        if en:
            out["director_address_en"] = en

    id_type = (out.get("id_type") or "").upper()
    name_cn = out.get("director_name_cn", "")
    name_en = out.get("director_name_en", "")
    # 护照/截图缺英文名时补罗马化；港/台中文名勿走此路径
    if id_type in ("PASSPORT", "SCREENSHOT") and name_cn and not looks_like_latin_name(name_en):
        fixed = ensure_passport_english_name(name_cn, name_en)
        if fixed:
            out["director_name_en"] = fixed
            name_en = fixed

    # 同步 director_name 展示
    cn = out.get("director_name_cn", "")
    en = out.get("director_name_en", "")
    if cn or en:
        shown = f"{cn} {en}".strip() if cn and en else (cn or en)
        if shown and not out.get("director_name"):
            out["director_name"] = shown
        elif shown and out.get("director_name") in (cn, en, ""):
            out["director_name"] = shown

    return out
