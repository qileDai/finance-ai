"""董事个人住址：香港/非香港、街道与区省市、住址国家。只看董事住址，不看注册地址。"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.materials.countries import (
    normalize_address_country,
    passport_country_option_names,
)

logger = logging.getLogger(__name__)

CLASSIFY_ADDRESS_SYSTEM = (
    "你只根据董事兼股东的个人住址（标签可能是住址或地址）的英文正文判断，"
    "不要看注册地址、公司名或其它字段。"
    "只输出 JSON："
    '{"is_hk":true|false,"street":"...","region":"...","address_country":"ISO3"}。'
    "规则："
    "1) is_hk 仅当该个人住址在香港特区。"
    "Hong Kong / Kowloon / New Territories / NT / N.T. / Tin Shui Wai / Kwun Tong 等均为香港，is_hk=true。"
    "内地、澳门、台湾、国外都是 false。注册地址在香港不能当成个人住址在香港。"
    "2) street = 街道／屋苑／地段／村等（室/楼/座、门牌可并入 street）。"
    "3) region = 区／市／省／州／邮递区号的全部，从第一个 District/City/Province/State 起到省/州/邮编为止；"
    "不能只填 District。不要把国家名放进 region。"
    "例：Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province → "
    'street="Room 110, No. 8, Xili South Road", '
    'region="Nanshan District, Shenzhen City, Guangdong Province"。'
    "4) address_country 用 ISO 3166-1 alpha-3。"
    "内地、香港、澳门、台湾一律 CHN；国外用对应国家（如 UZB）。"
    "5) 无英文住址则各键空/false。不要输出其它键或解释。"
)

_HK_EN_RE = re.compile(
    r"hong\s*kong|kowloon|new\s*territories|\bhksar\b|\bhk\s*island\b|"
    r"\bN\.?\s*T\.?\b|"
    r"tin\s*shui\s*wai|yuen\s*long|tuen\s*mun|sha\s*tin|kwun\s*tong|"
    r"tsuen\s*wan|kwai\s*chung|tai\s*po|fanling|sheung\s*shui|"
    r"tseung\s*kwan\s*o|sai\s*kung|tung\s*chung|mong\s*kok|"
    r"tsim\s*sha\s*tsui|sham\s*shui\s*po|wong\s*tai\s*sin|"
    r"causeway\s*bay|wan\s*chai|aberdeen",
    re.I,
)
_HK_CN_RE = re.compile(r"香港|九[龍龙]|新界")
_NON_HK_EN_RE = re.compile(
    r"shenzhen|guangdong|beijing|shanghai|nanshan|hangzhou|guangzhou|"
    r"uzbekistan|tashkent|province\b",
    re.I,
)
_ADMIN_PART_RE = re.compile(
    r"\b(districts?|city|cities|province|state|prefecture|"
    r"count(?:y|ies)|territory|postal(?:\s*code)?|zip(?:\s*code)?|"
    r"region|oblast|viloyat)\b",
    re.I,
)


def english_address_is_hk(address_en: str) -> bool:
    """只根据董事个人英文住址关键字判断是否香港特区。"""
    return bool(_HK_EN_RE.search(address_en or ""))


def chinese_address_is_hk(address_cn: str) -> bool:
    return bool(_HK_CN_RE.search(address_cn or ""))


def english_address_looks_non_hk(address_en: str) -> bool:
    """董事英文住址明显是内地城市/省或国外（用于否决模型误报香港）。"""
    en = address_en or ""
    if english_address_is_hk(en):
        return False
    if _NON_HK_EN_RE.search(en):
        return True
    parts = [p.strip() for p in re.split(r"[,，]", en) if p.strip()]
    if parts:
        iso = normalize_address_country(parts[-1])
        if iso and iso not in ("CHN", "HKG", "MAC", "TWN"):
            return True
    return False


def classify_address_user_prompt(address_en: str) -> str:
    en = (address_en or "").strip()
    if not en:
        return "请判定住址。证件相关行:（无英文住址）"
    return f"请只根据下面这一行英文住址拆分并判定类型，忽略其它资料。\n\n住址英文:\n{en}"


def split_english_street_region(address_en: str) -> tuple[str, str]:
    """英文住址：从第一段行政单位（District/City/Province 等）起到末尾为 region。"""
    address = (address_en or "").strip()
    if not address:
        return "", ""
    parts = [p.strip() for p in re.split(r"[,，]", address) if p.strip()]
    if not parts:
        return "", ""
    country = normalize_address_country(parts[-1])
    body = parts[:-1] if country and len(parts) > 1 else parts
    if not body:
        return "", ""
    idx = next((i for i, p in enumerate(body) if _ADMIN_PART_RE.search(p)), -1)
    if idx >= 0:
        return ", ".join(body[:idx]), ", ".join(body[idx:])
    if len(body) >= 3:
        return ", ".join(body[:-2]), ", ".join(body[-2:])
    if len(body) == 2:
        return body[0], body[1]
    return body[0], ""


def _region_is_truncated(llm_region: str, rule_region: str) -> bool:
    """LLM 的 region 是规则 region 的前缀/真子集（缺了市省）。"""
    a = re.sub(r"\s+", " ", (llm_region or "").strip().lower()).rstrip(",")
    b = re.sub(r"\s+", " ", (rule_region or "").strip().lower()).rstrip(",")
    if not b or a == b:
        return False
    if not a:
        return True
    if b.startswith(a) and len(b) > len(a):
        return True
    a_parts = [p.strip() for p in a.split(",") if p.strip()]
    b_parts = [p.strip() for p in b.split(",") if p.strip()]
    if a_parts and all(p in b_parts for p in a_parts) and len(b_parts) > len(a_parts):
        return True
    return False


def _truthy_hk(raw: Any) -> bool:
    if raw is True:
        return True
    s = str(raw or "").strip().lower()
    return s in ("1", "true", "yes", "hk")


def coerce_address_result(
    data: Any, address_en: str = "", address_cn: str = ""
) -> dict[str, str]:
    en = (address_en or "").strip()
    cn = (address_cn or "").strip()
    if not isinstance(data, dict):
        data = {}
    street = str(
        data.get("street") or data.get("director_address_street") or ""
    ).strip()
    region = str(
        data.get("region") or data.get("director_address_region") or ""
    ).strip()
    llm_hk = _truthy_hk(
        data.get("is_hk") if "is_hk" in data else data.get("address_is_hk")
    )
    keyword_hk = english_address_is_hk(en) or chinese_address_is_hk(cn)
    if not en and not cn:
        is_hk = False
    elif keyword_hk:
        is_hk = True
    elif english_address_looks_non_hk(en):
        is_hk = False
    else:
        is_hk = llm_hk

    country = normalize_address_country(str(data.get("address_country") or ""))
    if is_hk:
        country = "CHN"
    if not country and en:
        country = _guess_country_from_en(en)
    if is_hk or english_address_looks_greater_china(en):
        if not country:
            country = "CHN"
        elif country in ("HKG", "MAC", "TWN"):
            country = "CHN"

    if en and (not street and not region):
        street, region = split_english_street_region(en)
    elif en:
        rule_street, rule_region = split_english_street_region(en)
        if rule_region and _region_is_truncated(region, rule_region):
            street, region = rule_street, rule_region
    region = _strip_country_token(region, country)
    return {
        "director_address_street": street,
        "director_address_region": region,
        "address_country": country,
        "address_is_hk": "1" if is_hk else "0",
    }


def english_address_looks_greater_china(address_en: str) -> bool:
    t = (address_en or "").lower()
    return bool(
        re.search(
            r"\bchina\b|taiwan|taipei|macao|macau|hong\s*kong|kowloon|"
            r"guangdong|shenzhen|beijing|shanghai|province",
            t,
        )
    )


def _guess_country_from_en(address_en: str) -> str:
    parts = [p.strip() for p in re.split(r"[,，]", address_en or "") if p.strip()]
    for token in reversed(parts):
        iso = normalize_address_country(token)
        if iso:
            return iso
    return normalize_address_country(address_en) or ""


def _strip_country_token(region: str, country_iso: str) -> str:
    if not region or not country_iso:
        return region
    names = [n.lower() for n in passport_country_option_names(country_iso) if n]
    names.extend([country_iso.lower(), "uzbekistan"])
    parts = [p.strip() for p in re.split(r"[,，]", region) if p.strip()]
    kept = [p for p in parts if p.lower() not in names and p.lower() not in {"prc"}]
    return ", ".join(kept)


def weak_fallback_address(address_en: str) -> dict[str, str]:
    en = (address_en or "").strip()
    if not en:
        return {
            "director_address_street": "",
            "director_address_region": "",
            "address_country": "",
            "address_is_hk": "0",
        }
    street, region = split_english_street_region(en)
    is_hk = english_address_is_hk(en)
    country = _guess_country_from_en(en)
    if is_hk or english_address_looks_greater_china(en):
        country = country or "CHN"
        if country in ("HKG", "MAC", "TWN"):
            country = "CHN"
    region = _strip_country_token(region, country)
    return {
        "director_address_street": street,
        "director_address_region": region,
        "address_country": country,
        "address_is_hk": "1" if is_hk else "0",
    }


def classify_director_address(
    address_en: str,
    address_cn: str = "",
    *,
    llm: Any | None = None,
) -> dict[str, str]:
    """只把董事个人住址交给 LLM；不看注册地址。"""
    en = (address_en or "").strip()
    cn = (address_cn or "").strip()
    if not en:
        fb = weak_fallback_address("")
        if chinese_address_is_hk(cn):
            fb["address_is_hk"] = "1"
            fb["address_country"] = fb.get("address_country") or "CHN"
        return fb
    try:
        client = llm
        data: Any = None
        if client is not None and hasattr(client, "classify_director_address"):
            data = client.classify_director_address(en)
        elif client is not None and hasattr(client, "chat_json"):
            data = client.chat_json(
                CLASSIFY_ADDRESS_SYSTEM,
                classify_address_user_prompt(en),
                temperature=0.0,
            )
        elif client is None:
            from src.llm.openai_client import LLMClient

            data = LLMClient().classify_director_address(en)
        if isinstance(data, dict) and data:
            out = coerce_address_result(data, en, cn)
            if out.get("director_address_street") or out.get("director_address_region") or out.get("address_country") or out.get("address_is_hk") == "1":
                return out
        if data is not None:
            logger.warning("住址英文 LLM 分类结果无效: %s", data)
    except Exception as exc:
        logger.warning("住址英文 LLM 分类失败: %s", exc)
    fb = weak_fallback_address(en)
    if chinese_address_is_hk(cn):
        fb["address_is_hk"] = "1"
        fb["address_country"] = fb.get("address_country") or "CHN"
    return fb
