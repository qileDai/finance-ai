"""入 QA 前轻量指代改写：把「刚才那个」补成可检索的独立问句。"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_DEIXIS = re.compile(
    r"(刚才|上次|上面|前面|那个|那个说的|上面说的|刚才说的|"
    r"我的资料|我的号码|我的身份证|这[个份]|那[个份])"
)

_DURATION = re.compile(r"多久|周期|多长时间|要几天")
_ID_NUM = re.compile(r"号码|身份证号|证件号")
_ID_TYPE = re.compile(r"证件类型|什么证|身份证还是护照")


def needs_context_rewrite(question: str) -> bool:
    return bool(_DEIXIS.search(question or ""))


def rewrite_with_context(
    question: str,
    *,
    history: list[str] | None = None,
    materials_summary: str = "",
) -> str:
    """规则改写；失败则返回原问句。"""
    q = (question or "").strip()
    if not q or not needs_context_rewrite(q):
        return q

    history = history or []
    last_assistant = ""
    last_customer = ""
    for line in reversed(history):
        if not last_assistant and line.startswith("助手:"):
            last_assistant = line.removeprefix("助手:").strip()
        if not last_customer and line.startswith("客户:"):
            last_customer = line.removeprefix("客户:").strip()
        if last_assistant and last_customer:
            break

    # 我的号码 / 身份证
    if _ID_NUM.search(q) or ("号码" in q and ("我" in q or "身份证" in q)):
        if "证件号码=" in materials_summary or "号码已填" in materials_summary:
            return "我提交的身份证明号码是多少？请根据已收集材料据实回答。"
        return "我的身份证明号码是多少？若材料中没有请说明尚未识别。"

    if _ID_TYPE.search(q) or ("证件" in q and "什么" in q and "我" in q):
        if "证件类型=" in materials_summary:
            return "我提交的身份证明类型是什么？请根据已收集材料据实回答。"
        return "我的身份证明类型是什么？若材料中没有请说明尚未识别。"

    if "我的资料" in q and not _DURATION.search(q):
        return "根据我当前会话，已收集哪些资料、还需要哪些资料？"

    # 刚才说的 + 多久 → 结合上文
    if ("刚才" in q or "上面" in q or "前面" in q or "那个" in q) and (
        _DURATION.search(q) or "多久" in q
    ):
        hint = last_assistant or last_customer or ""
        if "开户" in hint or "开户" in q:
            return "香港银行开户审核要多久？"
        if "商证" in hint:
            return "商业登记证多久能出来？"
        return "香港公司注册要多久？审核周期是多少？"

    # 泛指「刚才那个」：拼上上一轮助手要点
    if last_assistant:
        snippet = last_assistant[:120].replace("\n", " ")
        rewritten = f"{q}（承接上文：{snippet}）"
        logger.info("指代改写: %s → %s", q[:40], rewritten[:80])
        return rewritten

    return q
