"""快速注册粘贴解析：LLM 为主，正则简繁弱兜底。"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.materials.address_classify import classify_director_address
from src.materials.countries import is_taiwan_issuing, normalize_issuing_iso
from src.materials.id_type_classify import (
    ICRIS_ID_TYPES,
    refine_id_type,
    weak_fallback_id_type,
)
from src.materials.name_classify import classify_director_name

logger = logging.getLogger(__name__)

PARSE_FIELD_KEYS = (
    "company_name_cn",
    "company_name_en",
    "registered_capital",
    "business_desc",
    "registered_office_cn",
    "registered_office_en",
    "director_name",
    "id_number",
    "id_type",
    "issuing_country",
    "contact_email",
    "director_address_cn",
    "director_address_en",
    "office_flat_floor",
    "office_building",
    "office_street",
    "office_district",
)

PARSE_QUICK_REGISTER_SYSTEM = (
    "你是香港公司快速注册资料提取助手。从客户粘贴的整段文字提取字段。"
    "标签可能是简体或繁体，也可能带括号国籍（如 住址(Uzbekistan)、住址（乌兹别克斯坦））。"
    "只输出 JSON，键只能用下面这些："
    "company_name_cn, company_name_en, registered_capital, business_desc, "
    "registered_office_cn, registered_office_en, director_name, id_number, "
    "id_type, issuing_country, contact_email, director_address_cn, "
    "director_address_en, office_flat_floor, office_building, office_street, "
    "office_district, taiwan_passport。"
    "规则："
    "1) 简繁同等：注册地址/註冊地址、经营范围/經營範圍、身份证/身分證、护照/護照。"
    "2) 董事+股东下面的「住址」或「地址」都是个人住址（含 住址英文/地址英文/英文地址，"
    "标签后可跟括号国籍）。按正文分栏：含汉字→ director_address_cn；拉丁字母为主 → "
    "director_address_en。禁止因为标签是「住址/地址」就放进中文栏。"
    "注册地址/註冊地址/注册办事处/建议地址/办事处地址 才是 registered_office_*，不要和个人住址混用。"
    "3) director_name 必须保留括号内英文整串，例如 張慧斌【ZHANG，Huibin】，不要只抽汉字。"
    "4) id_type 只根据证件标签行判定（身份证号码 / 香港身份证 / 护照号码），"
    "不要根据注册地址或住址里的「香港」判断证件类型；id_number 保留原文校验位如（2）。"
    "5) issuing_country 用 ISO 3166-1 alpha-3（如 UZB、CHN、TWN、MAC、USA）。"
    "台湾护照/中華民國/TWN/台灣 → issuing_country 必须是 TWN，且 taiwan_passport=true。"
    "澳门护照/澳門/MAC → issuing_country 必须是 MAC。"
    "6) 不要编造原文没有的字段；没有的键省略。taiwan_passport 仅 true/false。"
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def looks_like_english_address(s: str) -> bool:
    ascii_letters = len(re.findall(r"[A-Za-z]", s or ""))
    return ascii_letters >= 5 and not _CJK_RE.search((s or "").lstrip())


def looks_like_cjk_text(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def coerce_parse_result(data: Any, source_text: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k in PARSE_FIELD_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    llm_type = str(data.get("id_type") or out.get("id_type") or "")
    id_number = str(out.get("id_number") or "")
    refined = refine_id_type(source_text, id_number, llm_type)
    if refined in ICRIS_ID_TYPES:
        out["id_type"] = refined
    else:
        out.pop("id_type", None)

    tw_flag = data.get("taiwan_passport")
    taiwan = tw_flag is True or str(tw_flag).strip().lower() in ("1", "true", "yes")
    issuing_raw = str(out.get("issuing_country") or data.get("issuing_country") or "")
    if is_taiwan_issuing(issuing_raw) or taiwan:
        taiwan = True
        out["issuing_country"] = "TWN"
    else:
        iso = normalize_issuing_iso(issuing_raw)
        if iso:
            out["issuing_country"] = iso
        elif "issuing_country" in out:
            out.pop("issuing_country", None)
    if taiwan:
        out["taiwan_passport"] = True
        if not out.get("issuing_country"):
            out["issuing_country"] = "TWN"
    return out


def attach_director_structure(
    result: dict[str, Any], *, llm: Any | None = None
) -> dict[str, Any]:
    """用住址英文 / 姓名原文做结构化拆分，不覆盖 issuing_country 与姓名原文。"""
    if not result:
        return result
    en = str(result.get("director_address_en") or "").strip()
    cn = str(result.get("director_address_cn") or "").strip()
    if en or cn:
        addr = classify_director_address(en, cn, llm=llm)
        result["director_address_street"] = str(
            addr.get("director_address_street") or ""
        )
        result["director_address_region"] = str(
            addr.get("director_address_region") or ""
        )
        if addr.get("address_country"):
            result["address_country"] = str(addr.get("address_country") or "")
        result["address_is_hk"] = str(addr.get("address_is_hk") or "0")
    raw_name = str(result.get("director_name") or "").strip()
    if raw_name:
        named = classify_director_name(raw_name, llm=llm)
        if named.get("director_name_cn"):
            result["director_name_cn"] = named["director_name_cn"]
        if named.get("director_surname_en"):
            result["director_surname_en"] = named["director_surname_en"]
        if named.get("director_given_en"):
            result["director_given_en"] = named["director_given_en"]
    return result


def _strip_leading_number(s: str) -> str:
    return re.sub(r"^\s*\d+\s*[、.）)]\s*", "", s).strip()


def _value_after_label(stripped: str) -> str:
    idx = re.search(r"[:：]", stripped)
    if idx:
        return stripped[idx.end() :].strip()
    return ""


def parse_registration_text_regex(raw: str) -> dict[str, Any]:
    """简繁关键字弱兜底。住址按正文判中/英文，不靠标签。"""
    result: dict[str, Any] = {}
    if not raw:
        return result
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    rules: list[tuple[str, re.Pattern[str]]] = [
        (
            "director_address_cn",
            re.compile(
                r"^(住址中文|住址（中文）|中文住址|住址（繁）|地址中文|中文地址|地址（中文）)"
            ),
        ),
        (
            "director_address_en",
            re.compile(
                r"^(住址英文|住址（英文）|英文住址|地址英文|英文地址|地址（英文）)"
            ),
        ),
        ("company_name_cn", re.compile(r"^(公司中文名|公司中文名称|中文名)")),
        ("company_name_en", re.compile(r"^(公司英文名|公司英文名称|英文名)")),
        ("registered_capital", re.compile(r"^(注册资本|註冊資本)")),
        ("business_desc", re.compile(r"^(经营范围|經營範圍|业务范围|業務範圍)")),
        (
            "director_name",
            re.compile(
                r"^董事\s*[+＋、,，&＆]?\s*股东|^股东\s*[+＋、,，&＆]?\s*董事|"
                r"^董事兼股东|^董事|^股东|^董事兼股東|^股東"
            ),
        ),
        (
            "id_number",
            re.compile(
                r"^(香港身份证号?码?|香港身分證號?碼?|香港身分证号?码?|"
                r"身份证号?码?|身分證號?碼?|证件号|證件號|护照号|護照號)"
            ),
        ),
        (
            "contact_email",
            re.compile(r"^(联络邮箱|聯絡郵箱|邮箱|電郵|电邮|电子邮件|電子郵件)"),
        ),
        (
            "office_flat_floor",
            re.compile(r"^室[／/]楼[／/]座|^室[／/]樓[／/]座|^楼层|^樓層"),
        ),
        ("office_building", re.compile(r"^(大厦|大廈|大楼|大樓)")),
        (
            "office_street",
            re.compile(r"^街道[／/]屋苑[／/]地段[／/]村|^街道"),
        ),
        ("office_district", re.compile(r"^区|^區")),
    ]

    known_key_re = re.compile(
        r"^(住址中文|住址英文|住址（中文|住址（英文|中文住址|英文住址|"
        r"地址中文|地址英文|地址（中文|地址（英文|中文地址|英文地址|"
        r"住址|居住地址|地址|"
        r"公司中文名|公司中文名称|中文名|公司英文名|公司英文名称|英文名|"
        r"注册资本|註冊資本|经营范围|經營範圍|业务范围|業務範圍|董事|股东|股東|"
        r"香港身份证|香港身分證|香港身分证|身份证|身分證|证件号|證件號|护照号|護照號|"
        r"注册地址|註冊地址|公司名称|公司名稱|联络邮箱|聯絡郵箱|邮箱|電郵|"
        r"注册办事处|註冊辦事處|建议地址|建議地址|办事处地址|辦事處地址|"
        r"室[／/]楼|室[／/]樓|大厦|大廈|大楼|大樓|街道|区|區)"
    )
    _office_label_re = re.compile(
        r"^(注册地址|註冊地址|注册办事处|註冊辦事處|"
        r"建议地址|建議地址|办事处地址|辦事處地址)"
    )
    _director_addr_line_re = re.compile(
        r"^(?:"
        r"住址中文|住址英文|住址（中文）|住址（英文）|中文住址|英文住址|"
        r"地址中文|地址英文|地址（中文）|地址（英文）|中文地址|英文地址|"
        r"居住地址|住址|地址"
        r")(?:\s*[（(][^）)]+[）)])?\s*[:：]?\s*(.*)$"
    )

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        stripped = _strip_leading_number(line)

        company_name_match = re.match(r"^公司名称\s*[:：]\s*(\S.*)$", stripped)
        if not company_name_match:
            company_name_match = re.match(r"^公司名稱\s*[:：]\s*(\S.*)$", stripped)
        if company_name_match:
            after = company_name_match.group(1).strip()
            next_key = re.search(
                r"\s+(中文名|英文名|公司中文名|公司英文名|注册资本|註冊資本|"
                r"经营范围|經營範圍|董事|股东|股東|身份证|身分證|注册地址|註冊地址|"
                r"联络邮箱|聯絡郵箱|邮箱|住址)",
                after,
            )
            val = after[: next_key.start()].strip() if next_key else after
            if val:
                if looks_like_cjk_text(val):
                    result["company_name_cn"] = val
                else:
                    result["company_name_en"] = val
            continue

        if _office_label_re.match(stripped):
            after = _office_label_re.sub("", stripped).strip()
            after = re.sub(r"^[:：]\s*", "", after).strip()
            if after:
                if looks_like_english_address(after):
                    result["registered_office_en"] = after
                else:
                    result["registered_office_cn"] = after
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if (
                    nxt
                    and not re.match(r"^\s*\d+\s*[、.）)]", nxt)
                    and not known_key_re.match(_strip_leading_number(nxt))
                    and looks_like_english_address(nxt)
                ):
                    result["registered_office_en"] = nxt
            continue

        addr_line = _director_addr_line_re.match(stripped)
        if addr_line and not _office_label_re.match(stripped):
            val = (addr_line.group(1) or "").strip()
            if val:
                if looks_like_english_address(val):
                    result["director_address_en"] = val
                else:
                    result["director_address_cn"] = val
            continue

        for field, pat in rules:
            if pat.match(stripped):
                val = _value_after_label(stripped)
                if val:
                    result[field] = val
                break

    typed = weak_fallback_id_type(raw, str(result.get("id_number") or ""))
    if typed.get("id_number") and not result.get("id_number"):
        result["id_number"] = typed["id_number"]
    refined = refine_id_type(
        raw, str(result.get("id_number") or ""), str(result.get("id_type") or "")
    )
    if refined:
        result["id_type"] = refined
    if is_taiwan_issuing(raw) or re.search(r"台湾护照|台灣護照|臺灣護照", raw):
        result["taiwan_passport"] = True
        result.setdefault("issuing_country", "TWN")
    return result


def parse_quick_register_text(text: str, *, llm: Any | None = None) -> dict[str, Any]:
    """LLM 抽字段；失败或空结果才正则弱兜底。"""
    blob = (text or "").strip()
    if not blob:
        return {}
    try:
        client = llm
        if client is None:
            from src.llm.openai_client import LLMClient

            client = LLMClient()
        if hasattr(client, "parse_quick_register_text"):
            data = client.parse_quick_register_text(blob)
        else:
            data = client.chat_json(
                PARSE_QUICK_REGISTER_SYSTEM,
                f"粘贴资料:\n{blob}",
                temperature=0.0,
            )
        result = coerce_parse_result(data, source_text=blob)
        if result:
            result["source"] = "llm"
            return attach_director_structure(result, llm=client)
        logger.warning("快速注册 LLM 解析结果为空，改用正则兜底")
    except Exception as exc:
        logger.warning("快速注册 LLM 解析失败: %s", exc)

    fb = parse_registration_text_regex(blob)
    if fb:
        fb["source"] = "regex"
        attach_director_structure(fb, llm=llm)
    return fb
