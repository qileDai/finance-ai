"""轻量意图分类：规则优先，可选 LLM 回退（由 message_graph 编排）"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from src.materials.form_parser import (
    extract_material_fields,
    text_has_material_keyword,
    text_looks_like_material_submit,
)

logger = logging.getLogger(__name__)

INTENT_SUBMIT_MATERIAL = "submit_material"
INTENT_ASK_PROGRESS = "ask_progress"
INTENT_QA = "qa"
INTENT_UNCLEAR_MATERIAL = "unclear_material"

_PROGRESS_PATTERNS = (
    r"还缺什么",
    r"还差什么",
    r"差什么资料",
    r"缺什么资料",
    r"缺哪些",
    r"还缺哪些",
    r"资料进度",
    r"材料进度",
    r"收集进度",
    r"进度怎么样",
    r"进度如何",
    r"查一下进度",
    r"看看进度",
    r"材料齐了吗",
    r"资料齐了吗",
    r"还差哪些",
    r"缺什么$",
    r"^进度$",
)

_QA_HINT = re.compile(
    r"(多久|怎样|怎么|如何|需要注意|注意事项|流程|步骤|费用|多少钱|可以吗|能不能|是否|"
    r"什么意思|为什么|区别|有什么用|开户|年审|审计)",
)


@dataclass
class IntentResult:
    intent: str
    fields: dict[str, str] = field(default_factory=dict)
    source: str = "rule"  # rule | llm | fallback


def _is_ask_progress(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t in ("/进度", "/progress", "进度"):
        return True
    for pat in _PROGRESS_PATTERNS:
        if re.search(pat, t):
            return True
    return False


def _looks_like_qa_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t or "？" in t:
        return True
    if _QA_HINT.search(t) and not text_looks_like_material_submit(t):
        return True
    return False


def _llm_classify(text: str, status: str) -> IntentResult | None:
    """规则不确定时：单次短 JSON 分类；失败返回 None"""
    try:
        from src.llm.openai_client import LLMClient

        client = LLMClient()
        system = (
            "你是企业微信客服意图分类器。根据客户消息判断意图，只输出 JSON：\n"
            '{"intent":"submit_material|ask_progress|qa|unclear_material",'
            '"fields":{"company_name_en":"...","directors":"..."}}\n'
            "规则：\n"
            "- submit_material：客户在提交/补充注册资料（公司名、股东、董事、地址、邮箱等）\n"
            "- ask_progress：询问还缺什么资料/材料进度\n"
            "- unclear_material：像在交资料但看不出具体字段\n"
            "- qa：业务咨询问答（流程、时长、注意事项等）\n"
            "fields 仅在能明确抽出时填写，键用英文："
            "company_name_en,company_name_cn,registered_office,contact_email,"
            "contact_phone,founder_members,directors,company_secretary,"
            "business_desc,br_certificate_years,applicant_name,applicant_email,applicant_phone。"
            "无法确定字段时 fields 为空对象。"
        )
        user = f"会话状态: {status}\n客户消息:\n{text[:800]}"
        data: dict[str, Any] = client.chat_json(system, user, temperature=0.0)
        intent = str(data.get("intent") or "").strip()
        if intent not in (
            INTENT_SUBMIT_MATERIAL,
            INTENT_ASK_PROGRESS,
            INTENT_QA,
            INTENT_UNCLEAR_MATERIAL,
        ):
            return None
        raw_fields = data.get("fields") or {}
        fields: dict[str, str] = {}
        if isinstance(raw_fields, dict):
            for k, v in raw_fields.items():
                if isinstance(k, str) and v is not None and str(v).strip():
                    fields[k.strip()] = str(v).strip()
        return IntentResult(intent=intent, fields=fields, source="llm")
    except Exception as exc:
        logger.warning("意图 LLM 回退失败: %s", exc)
        return None


def classify_intent(text: str, status: str = "") -> IntentResult:
    """规则优先；不确定且开启 WEWORK_INTENT_LLM_FALLBACK 时调一次 LLM。"""
    t = (text or "").strip()
    if not t:
        return IntentResult(intent=INTENT_QA, source="rule")

    if _is_ask_progress(t):
        return IntentResult(intent=INTENT_ASK_PROGRESS, source="rule")

    fields = extract_material_fields(t)
    if fields:
        return IntentResult(intent=INTENT_SUBMIT_MATERIAL, fields=fields, source="rule")

    if text_looks_like_material_submit(t):
        # 像表单但未抽出字段
        return IntentResult(intent=INTENT_UNCLEAR_MATERIAL, source="rule")

    collecting = status in ("COLLECTING", "REVIEW")
    if collecting and text_has_material_keyword(t):
        # 收集中提到资料关键词：优先材料链路，避免进 RAG
        if settings.wework_intent_llm_fallback:
            llm = _llm_classify(t, status)
            if llm and llm.intent != INTENT_QA:
                if llm.intent == INTENT_SUBMIT_MATERIAL and not llm.fields:
                    llm.intent = INTENT_UNCLEAR_MATERIAL
                return llm
        return IntentResult(intent=INTENT_UNCLEAR_MATERIAL, source="rule")

    # 弱信号：有资料词但非明确问答 → 可选 LLM
    if (
        settings.wework_intent_llm_fallback
        and text_has_material_keyword(t)
        and not _looks_like_qa_question(t)
    ):
        llm = _llm_classify(t, status)
        if llm:
            if llm.intent == INTENT_SUBMIT_MATERIAL and not llm.fields:
                return IntentResult(intent=INTENT_UNCLEAR_MATERIAL, fields={}, source="llm")
            return llm

    return IntentResult(intent=INTENT_QA, source="rule")
