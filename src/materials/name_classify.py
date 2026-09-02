"""董事姓名：原文入库，LLM 拆中文姓名 + 英文姓氏 + 英文名字。括号种类不写死。"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

CLASSIFY_NAME_SYSTEM = (
    "你把董事姓名原文拆成三栏，间隔符可能是 【】 [] （） () 「」 或其它包裹，不要假设只有一种。"
    "英文里逗号可能是 , 或 ，。只输出 JSON："
    '{"name_cn":"...","surname_en":"...","given_en":"..."}。'
    "规则："
    "1) 例 張慧斌【ZHANG，Huibin】 → name_cn=張慧斌, surname_en=ZHANG, given_en=Huibin。"
    "2) name_cn 只留汉字姓名，不要带括号和英文。"
    "3) 纯中文：surname_en、given_en 为空。"
    "4) 纯英文：name_cn 为空；姓在前（ZHANG / CHAN）其余为名。"
    "5) 不要改写原文没有的字母；不要输出其它键。"
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WRAPPER_PAIRS = (
    ("【", "】"),
    ("[", "]"),
    ("（", "）"),
    ("(", ")"),
    ("「", "」"),
    ("『", "』"),
    ("｛", "｝"),
    ("{", "}"),
    ("<", ">"),
    ("〈", "〉"),
    ("《", "》"),
)


def classify_name_user_prompt(raw_name: str) -> str:
    name = (raw_name or "").strip()
    if not name:
        return "请拆分姓名。（无原文）"
    return f"请拆分下面这一条姓名原文（不要看其它资料）:\n{name}"


def _cjk_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if _CJK_RE.match(ch)).strip()


def _split_latin_surname_given(latin: str) -> tuple[str, str]:
    text = (latin or "").replace("，", ",").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "", ""
    if "," in text:
        a, b = text.split(",", 1)
        return a.strip(), b.strip()
    parts = [p for p in text.split(" ") if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _extract_wrapped_latin(raw: str) -> tuple[str, str]:
    """返回 (中文侧原文, 包裹内拉丁)。找不到包裹则 ("", "")。"""
    s = (raw or "").strip()
    for left, right in _WRAPPER_PAIRS:
        i = s.find(left)
        j = s.rfind(right)
        if i >= 0 and j > i:
            inner = s[i + len(left) : j].strip()
            outer = (s[:i] + s[j + len(right) :]).strip()
            if re.search(r"[A-Za-z]", inner):
                return outer, inner
    return "", ""


def weak_fallback_name(raw_name: str) -> dict[str, str]:
    s = (raw_name or "").strip()
    if not s:
        return {"director_name_cn": "", "director_surname_en": "", "director_given_en": ""}
    outer, inner = _extract_wrapped_latin(s)
    if inner:
        surname, given = _split_latin_surname_given(inner)
        name_cn = _cjk_only(outer) or _cjk_only(s)
        return {
            "director_name_cn": name_cn,
            "director_surname_en": surname,
            "director_given_en": given,
        }
    if _CJK_RE.search(s) and not re.search(r"[A-Za-z]", s):
        return {
            "director_name_cn": _cjk_only(s) or s,
            "director_surname_en": "",
            "director_given_en": "",
        }
    if re.search(r"[A-Za-z]", s) and not _CJK_RE.search(s):
        surname, given = _split_latin_surname_given(s)
        return {
            "director_name_cn": "",
            "director_surname_en": surname,
            "director_given_en": given,
        }
    # 中英混排但无成对括号：汉字 vs 拉丁硬拆，英文按逗号/空格
    name_cn = _cjk_only(s)
    latin = re.sub(r"[^\x00-\x7F]+", " ", s)
    latin = re.sub(r"[\[\]\(\)\{\}<>]", " ", latin)
    surname, given = _split_latin_surname_given(latin)
    return {
        "director_name_cn": name_cn,
        "director_surname_en": surname,
        "director_given_en": given,
    }


def coerce_name_result(data: Any, raw_name: str = "") -> dict[str, str]:
    if not isinstance(data, dict):
        return weak_fallback_name(raw_name)
    name_cn = str(
        data.get("name_cn") or data.get("director_name_cn") or ""
    ).strip()
    surname = str(data.get("surname_en") or data.get("director_surname_en") or "").strip()
    given = str(data.get("given_en") or data.get("director_given_en") or "").strip()
    name_cn = _cjk_only(name_cn) or name_cn
    if re.search(r"[A-Za-z【\[（(]", name_cn):
        name_cn = _cjk_only(name_cn)
    if not name_cn and not surname and not given:
        return weak_fallback_name(raw_name)
    fb = weak_fallback_name(raw_name)
    if fb.get("director_surname_en") and not surname:
        surname = fb["director_surname_en"]
        given = given or fb.get("director_given_en") or ""
    if fb.get("director_name_cn") and not name_cn:
        name_cn = fb["director_name_cn"]
    return {
        "director_name_cn": name_cn,
        "director_surname_en": surname,
        "director_given_en": given,
    }


def classify_director_name(
    raw_name: str,
    *,
    llm: Any | None = None,
) -> dict[str, str]:
    """只把姓名原文交给 LLM；director_name 原文由调用方另行保存。"""
    name = (raw_name or "").strip()
    if not name:
        return weak_fallback_name("")
    try:
        client = llm
        data: Any = None
        if client is not None and hasattr(client, "classify_director_name"):
            data = client.classify_director_name(name)
        elif client is not None and hasattr(client, "chat_json"):
            data = client.chat_json(
                CLASSIFY_NAME_SYSTEM,
                classify_name_user_prompt(name),
                temperature=0.0,
            )
        elif client is None:
            from src.llm.openai_client import LLMClient

            data = LLMClient().classify_director_name(name)
        if isinstance(data, dict) and data:
            out = coerce_name_result(data, name)
            if out.get("director_name_cn") or out.get("director_surname_en"):
                return out
        if data is not None:
            logger.warning("姓名 LLM 拆分结果无效: %s", data)
    except Exception as exc:
        logger.warning("姓名 LLM 拆分失败: %s", exc)
    return weak_fallback_name(name)
