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
INTENT_GREETING = "greeting"

# ask_progress 子模式（状态机选模板）
REPLY_RECEIVED = "received"
REPLY_MISSING = "missing"
REPLY_FULL_PROGRESS = "full_progress"
REPLY_CASE_STATUS = "case_status"
REPLY_KNOWLEDGE_CHECKLIST = "knowledge_checklist"

VALID_REPLY_MODES = frozenset(
    {
        REPLY_RECEIVED,
        REPLY_MISSING,
        REPLY_FULL_PROGRESS,
        REPLY_CASE_STATUS,
        REPLY_KNOWLEDGE_CHECKLIST,
    }
)

# 冲突表（规则 + 单测）：
# 收集到哪些资料了 → received
# 还要收集哪些发出来 → missing
# 香港注册需要哪些资料 → knowledge_checklist
# 我还需要交哪些 → missing
# 董事资料怎么填 → qa
# 办得怎么样了 → case_status
# 进度 → full_progress
# 邮箱是 a@b.com → submit_material

_RECEIVED_RE = re.compile(
    r"(收集到哪些|已收哪些|收了哪些|收到哪些|交了哪些|交了什么|我交了什么|"
    r"收到什么资料|收集了什么|你们收到)",
)

_MISSING_RE = re.compile(
    r"(还缺什么|还缺啥|还差什么|还差啥|差什么资料|缺什么资料|缺哪些|还缺哪些|还差哪些|"
    r"我还缺|还要收集|还要准备|我还需要交|我还要交|缺什么$|缺啥)",
)

_CASE_STATUS_RE = re.compile(
    r"(办得怎么样|办理得怎么样|办理进度|注册进度|我的单怎么样|现在办得|"
    r"办理到哪|办得怎样|办理得怎样|单子怎么样)",
)

_FULL_PROGRESS_RE = re.compile(
    r"(资料进度|材料进度|收集进度|进度怎么样|进度如何|查一下进度|看看进度|"
    r"材料齐了吗|资料齐了吗|发一下进度|进度.*发出来|收齐了吗|收齐没有|"
    r"我的资料|^进度$|/进度|/progress)",
)

# 知识型总清单（无「我的/本会话」时优先）
_KNOWLEDGE_CHECKLIST_RE = re.compile(
    r"(一般|通常|香港公司注册需要|香港注册需要|注册香港公司需要|"
    r"开户需要哪些|开户需要什么|注册需要准备什么|注册一般需要)",
)

# 歧义：资料清单类（需结合 has_materials / 会话标记消歧）
_AMBIGUOUS_CHECKLIST_RE = re.compile(
    r"(需要哪些资料|需要什么资料|要哪些资料|要准备哪些|资料清单|材料清单|"
    r"发一下清单|把.*清单.*发|清单.*发出来|资料.*发出来)",
)

_SESSION_MARKER_RE = re.compile(
    r"(我的|当前|已经|还差|还缺|本会话|本群|我们这|交了|收到了|收集到)",
)

_CORRECTION_RE = re.compile(r"(改成|更正|修改|换成|改为|修改为|更正为)")

_PROGRESS_PATTERNS = (
    r"还缺什么",
    r"还缺啥",
    r"还差什么",
    r"还差啥",
    r"差什么资料",
    r"缺什么资料",
    r"缺哪些",
    r"缺啥",
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
    r"收集到哪些",
    r"已收哪些",
    r"收了哪些",
    r"收到哪些",
    r"交了哪些",
    r"交了什么",
    r"我交了什么",
    r"我还缺",
    r"还要收集",
    r"还要准备",
    r"要准备哪些",
    r"需要哪些资料",
    r"需要什么资料",
    r"要哪些资料",
    r"资料清单",
    r"材料清单",
    r"发一下进度",
    r"发一下清单",
    r"把.*清单.*发",
    r"清单.*发出来",
    r"进度.*发出来",
    r"资料.*发出来",
    r"收齐了吗",
    r"收齐没有",
    r"我的资料",
    r"办得怎么样",
    r"办理得怎么样",
    r"办理进度",
    r"注册进度",
    r"我的单怎么样",
    r"现在办得",
    r"办理到哪",
    r"香港注册需要",
    r"香港公司注册需要",
)

# 会话材料/进度问法（供误进 QA 时二次拦截）；排除强业务知识问与知识总清单
_SESSION_STATE_QUERY = re.compile(
    r"(收集到哪些|已收哪些|收了哪些|收到哪些|交了哪些|交了什么|"
    r"我交了什么|我还缺|还要收集|还要准备|我还需要交|"
    r"发一下进度|收齐了吗|我的资料|"
    r"还缺什么|还缺啥|还差什么|还差啥|缺哪些|缺啥|资料进度|材料进度|收集进度|"
    r"办得怎么样|办理进度|注册进度|现在办得)"
)

_STRONG_BIZ_QA = re.compile(
    r"(多久|多长时间|周期|注意事项|要注意|费用|多少钱|开户流程|怎么注册|如何注册|"
    r"年审|审计|面签注意|怎么填|如何填|怎样填)"
)

_QA_HINT = re.compile(
    r"(多久|怎样|怎么|如何|需要注意|注意事项|流程|步骤|费用|多少钱|可以吗|能不能|是否|"
    r"什么意思|为什么|区别|有什么用|开户|年审|审计)",
)

_BUSINESS_HINT = re.compile(
    r"开户|注册|董事|股东|面签|年审|资料|材料|多久|周期|ICRIS|香港|银行|"
    r"费用|审计|商证|公司|填表|转人工"
)

_GREETING_EXACT = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "哈罗",
        "hi",
        "hello",
        "hey",
        "在吗",
        "在不在",
        "在嘛",
        "有人吗",
        "早上好",
        "上午好",
        "中午好",
        "下午好",
        "晚上好",
        "谢谢",
        "谢谢你",
        "谢谢您",
        "感谢",
        "多谢",
        "谢谢啦",
        "好的",
        "好",
        "收到",
        "明白",
        "了解",
        "嗯",
        "嗯嗯",
        "哦",
        "喔",
        "噢",
        "ok",
        "okay",
        "kk",
        "拜拜",
        "再见",
        "回见",
        "对",
        "是的",
        "是",
        "没错",
        "对的",
        "就是这个",
        "就是",
    }
)

_GREETING_PATTERN = re.compile(
    r"^(你好|您好)[啊呀哈哇]?$|"
    r"^(hi|hello|hey)[!！.。]*$|"
    r"^在吗[?？]?$|"
    r"^谢谢(你|您|啦|了)?[!！.。]*$|"
    r"^[哈啊哦嗯喔噢]+$",
    re.IGNORECASE,
)

_SHORT_ACK_RE = re.compile(r"^(对|是的|是|没错|对的|就是这个|就是)[!！.。]?$")


@dataclass
class IntentSpan:
    text: str
    label: str  # session | biz | material


@dataclass
class IntentResult:
    intent: str
    fields: dict[str, str] = field(default_factory=dict)
    source: str = "rule"  # rule | llm | fallback | merge
    reply_mode: str = ""  # ask_progress 子模式；其它意图为空
    is_correction: bool = False  # 材料修正/覆盖
    confidence: float = 0.0
    spans: list[IntentSpan] = field(default_factory=list)
    veto_applied: list[str] = field(default_factory=list)
    rule_intent: str = ""
    rule_mode: str = ""
    model_intent: str = ""
    model_mode: str = ""
    model_confidence: float = 0.0


# 兼容别名
IntentSignal = IntentResult


def _normalize_social(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    for ch in ("？", "?", "。", "！", "!", "，", ",", "、", "~", "～"):
        t = t.replace(ch, "")
    return t


def resolve_reply_mode(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> str:
    """将进度类说法解析为 reply_mode。"""
    t = (text or "").strip()
    if not t:
        return REPLY_FULL_PROGRESS
    if t in ("/进度", "/progress", "进度"):
        return REPLY_FULL_PROGRESS
    if t in ("/资料", "/docs"):
        return REPLY_KNOWLEDGE_CHECKLIST

    if _CASE_STATUS_RE.search(t):
        return REPLY_CASE_STATUS
    if _RECEIVED_RE.search(t):
        return REPLY_RECEIVED
    if _MISSING_RE.search(t):
        return REPLY_MISSING
    if _FULL_PROGRESS_RE.search(t):
        return REPLY_FULL_PROGRESS

    # 明确知识型（一般/香港注册需要…）且无强会话标记 → knowledge
    if _KNOWLEDGE_CHECKLIST_RE.search(t) and not _SESSION_MARKER_RE.search(t):
        return REPLY_KNOWLEDGE_CHECKLIST

    if _AMBIGUOUS_CHECKLIST_RE.search(t) or _KNOWLEDGE_CHECKLIST_RE.search(t):
        if _SESSION_MARKER_RE.search(t):
            return REPLY_MISSING
        if _KNOWLEDGE_CHECKLIST_RE.search(t) and not re.search(r"我|还缺|还差", t):
            return REPLY_KNOWLEDGE_CHECKLIST
        # 「需要哪些资料」默认：收集中或已有材料 → 会话缺项；否则知识总清单
        if has_materials or status in ("COLLECTING", "REVIEW"):
            return REPLY_MISSING
        return REPLY_KNOWLEDGE_CHECKLIST

    # 「发出来」搭配资料/材料/进度/清单
    if re.search(r"发出来|发给我|发一下", t) and re.search(
        r"资料|材料|进度|清单|缺", t
    ):
        if re.search(r"缺|还要|还差", t):
            return REPLY_MISSING
        if re.search(r"进度", t):
            return REPLY_FULL_PROGRESS
        if has_materials or status in ("COLLECTING", "REVIEW"):
            return REPLY_MISSING
        return REPLY_KNOWLEDGE_CHECKLIST

    return REPLY_FULL_PROGRESS


def _is_ask_progress(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t in ("/进度", "/progress", "进度"):
        return True
    for pat in _PROGRESS_PATTERNS:
        if re.search(pat, t):
            return True
    if re.search(r"发出来|发给我|发一下", t) and re.search(
        r"资料|材料|进度|清单|缺", t
    ):
        return True
    # 知识总清单 / 歧义清单说法也走 ask_progress + reply_mode
    if _KNOWLEDGE_CHECKLIST_RE.search(t) or _AMBIGUOUS_CHECKLIST_RE.search(t):
        return True
    return False


def is_knowledge_checklist_query(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> bool:
    if not _is_ask_progress(text) and not _AMBIGUOUS_CHECKLIST_RE.search(text or ""):
        if not _KNOWLEDGE_CHECKLIST_RE.search(text or ""):
            return False
    return (
        resolve_reply_mode(text, status, has_materials=has_materials)
        == REPLY_KNOWLEDGE_CHECKLIST
    )


def looks_like_session_state_query(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> bool:
    """是否应走会话态直答（材料/进度/办理），而非 RAG。

    知识型总清单不算会话态（应由 knowledge_checklist 模板处理）。
    """
    t = (text or "").strip()
    if not t:
        return False
    if is_knowledge_checklist_query(t, status, has_materials=has_materials):
        return False
    if _STRONG_BIZ_QA.search(t) and not _SESSION_STATE_QUERY.search(t):
        return False
    if _is_ask_progress(t):
        mode = resolve_reply_mode(t, status, has_materials=has_materials)
        return mode != REPLY_KNOWLEDGE_CHECKLIST
    if _SESSION_STATE_QUERY.search(t) and not _STRONG_BIZ_QA.search(t):
        return True
    return False


def looks_like_strong_biz_qa(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_STRONG_BIZ_QA.search(t))


def batch_has_mixed_session_and_biz(texts: list[str]) -> bool:
    """防抖合并前：同时含会话态句与强业务句 → 不应合成一轮 QA。"""
    has_session = False
    has_biz = False
    for raw in texts:
        t = (raw or "").strip()
        if not t:
            continue
        if looks_like_session_state_query(t) or _is_ask_progress(t):
            # knowledge 不算会话态，但也不该和业务混进同一 QA
            has_session = True
        if is_knowledge_checklist_query(t):
            has_session = True
        if looks_like_strong_biz_qa(t) and not looks_like_session_state_query(t):
            has_biz = True
    return has_session and has_biz


def is_short_ack(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_SHORT_ACK_RE.match(t)) or _normalize_social(t) in {
        "对",
        "是的",
        "是",
        "没错",
        "对的",
        "就是这个",
        "就是",
    }


def _looks_like_qa_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t or "？" in t:
        if _is_greeting(t):
            return False
        # 「还缺什么？」仍是进度
        if _is_ask_progress(t):
            return False
        return True
    # 「董事资料怎么填 / 开户要多久」优先 QA，即使带资料关键词
    if _STRONG_BIZ_QA.search(t) and not _is_ask_progress(t):
        return True
    if _QA_HINT.search(t) and not text_looks_like_material_submit(t):
        # 纯进度问法不算业务 QA
        if _is_ask_progress(t) and not _STRONG_BIZ_QA.search(t):
            return False
        return True
    return False


def _is_greeting(text: str) -> bool:
    """寒暄/致谢/短应答：不进 RAG。短确认「对」在状态机结合上一轮再处理。"""
    t = (text or "").strip()
    if not t:
        return False
    if t in ("/进度", "/progress", "进度", "/资料", "/docs"):
        return False
    # 「还缺啥」等极短进度句勿当寒暄
    if _is_ask_progress(t) or _MISSING_RE.search(t) or _RECEIVED_RE.search(t):
        return False
    n = _normalize_social(t)
    if not n:
        return False
    if n in _GREETING_EXACT:
        return True
    if _GREETING_PATTERN.match(n):
        return True
    if (
        len(n) <= 4
        and not _BUSINESS_HINT.search(n)
        and not text_has_material_keyword(t)
        and not any(ch.isdigit() for ch in n)
        and not _SESSION_STATE_QUERY.search(t)
    ):
        return True
    return False


def heuristic_spans(text: str, status: str = "", *, has_materials: bool = False) -> list[IntentSpan]:
    """按行拆 session/biz/material span，供 Planner 混句编排。"""
    spans: list[IntentSpan] = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        lines = [(text or "").strip()] if (text or "").strip() else []
    for ln in lines:
        if extract_material_fields(ln) or (
            text_looks_like_material_submit(ln) and not _looks_like_qa_question(ln)
        ):
            spans.append(IntentSpan(text=ln, label="material"))
        elif looks_like_session_state_query(ln, status, has_materials=has_materials) or (
            _is_ask_progress(ln)
            and resolve_reply_mode(ln, status, has_materials=has_materials)
            != REPLY_KNOWLEDGE_CHECKLIST
        ):
            spans.append(IntentSpan(text=ln, label="session"))
        elif is_knowledge_checklist_query(ln, status, has_materials=has_materials):
            spans.append(IntentSpan(text=ln, label="session"))
        elif looks_like_strong_biz_qa(ln) or _looks_like_qa_question(ln):
            spans.append(IntentSpan(text=ln, label="biz"))
        else:
            spans.append(IntentSpan(text=ln, label="biz"))
    return spans


def _llm_classify(
    text: str,
    status: str,
    *,
    has_materials: bool = False,
) -> IntentResult | None:
    """规则不确定时：单次短 JSON 分类；失败返回 None"""
    try:
        from src.llm.openai_client import LLMClient

        intent_model = (getattr(settings, "wework_intent_model", "") or "").strip()
        timeout = float(getattr(settings, "wework_intent_timeout_seconds", 8.0) or 8.0)
        client = LLMClient(
            model=intent_model or None,
            timeout_seconds=timeout,
        )
        system = (
            "你是企业微信客服意图分类器。根据客户消息判断意图，只输出 JSON：\n"
            '{"intent":"greeting|submit_material|ask_progress|qa|unclear_material",'
            '"reply_mode":"received|missing|full_progress|case_status|knowledge_checklist|",'
            '"confidence":0.0,'
            '"fields":{"company_name_en":"..."},'
            '"spans":[{"text":"...","label":"session|biz|material"}]}\n'
            "规则：\n"
            "- greeting：寒暄/致谢/短应答，非业务咨询\n"
            "- submit_material：提交/补充/更正注册资料\n"
            "- ask_progress：材料进度/已收/还缺/办理状态，或通用资料总清单\n"
            "  reply_mode：received|missing|full_progress|case_status|knowledge_checklist\n"
            "- unclear_material：像交资料但看不出字段\n"
            "- qa：业务咨询（流程、时长、怎么填等）\n"
            "「香港注册需要哪些资料」且无「我的/还缺」→ ask_progress + knowledge_checklist\n"
            "「我还缺哪些」→ ask_progress + missing\n"
            "混句请拆 spans；confidence 0~1。"
        )
        user = (
            f"会话状态: {status}\n已有材料: {bool(has_materials)}\n"
            f"客户消息:\n{text[:800]}"
        )
        data: dict[str, Any] = client.chat_json(system, user, temperature=0.0)
        intent = str(data.get("intent") or "").strip()
        if intent not in (
            INTENT_GREETING,
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
        reply_mode = str(data.get("reply_mode") or "").strip()
        if intent == INTENT_ASK_PROGRESS:
            if reply_mode not in VALID_REPLY_MODES:
                reply_mode = resolve_reply_mode(
                    text, status, has_materials=has_materials
                )
        else:
            reply_mode = ""
        try:
            conf = float(data.get("confidence") or 0.7)
        except (TypeError, ValueError):
            conf = 0.7
        conf = max(0.0, min(1.0, conf))
        spans: list[IntentSpan] = []
        raw_spans = data.get("spans") or []
        if isinstance(raw_spans, list):
            for item in raw_spans:
                if not isinstance(item, dict):
                    continue
                st = str(item.get("text") or "").strip()
                lab = str(item.get("label") or "").strip()
                if st and lab in ("session", "biz", "material"):
                    spans.append(IntentSpan(text=st, label=lab))
        if not spans:
            spans = heuristic_spans(text, status, has_materials=has_materials)
        return IntentResult(
            intent=intent,
            fields=fields,
            source="llm",
            reply_mode=reply_mode,
            confidence=conf,
            spans=spans,
            is_correction=bool(fields and _CORRECTION_RE.search(text or "")),
        )
    except Exception as exc:
        logger.warning("意图 LLM 回退失败: %s", exc)
        return None


def _should_try_llm(text: str, status: str) -> bool:
    """资料|进度|办理 歧义或收集中有资料词时尝试 LLM。"""
    if not settings.wework_intent_llm_fallback:
        return False
    t = text or ""
    if text_has_material_keyword(t):
        return True
    if re.search(r"资料|材料|进度|办理|清单", t):
        return True
    if status in ("COLLECTING", "REVIEW"):
        return True
    if "\n" in t and len([x for x in t.splitlines() if x.strip()]) >= 2:
        return True
    return False


def classify_intent_rules(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> IntentResult:
    """仅规则分类（带 confidence），不调模型。"""
    t = (text or "").strip()
    spans = heuristic_spans(t, status, has_materials=has_materials)
    if not t:
        return IntentResult(
            intent=INTENT_GREETING,
            source="rule",
            confidence=0.99,
            spans=spans,
            rule_intent=INTENT_GREETING,
        )

    if _is_greeting(t) and not _CORRECTION_RE.search(t):
        return IntentResult(
            intent=INTENT_GREETING,
            source="rule",
            confidence=0.95,
            spans=spans,
            rule_intent=INTENT_GREETING,
        )

    fields = extract_material_fields(t)
    if fields and _CORRECTION_RE.search(t):
        return IntentResult(
            intent=INTENT_SUBMIT_MATERIAL,
            fields=fields,
            source="rule",
            is_correction=True,
            confidence=0.93,
            spans=spans,
            rule_intent=INTENT_SUBMIT_MATERIAL,
        )

    if _is_ask_progress(t):
        mode = resolve_reply_mode(t, status, has_materials=has_materials)
        return IntentResult(
            intent=INTENT_ASK_PROGRESS,
            source="rule",
            reply_mode=mode,
            confidence=0.92,
            spans=spans,
            rule_intent=INTENT_ASK_PROGRESS,
            rule_mode=mode,
        )

    if fields:
        return IntentResult(
            intent=INTENT_SUBMIT_MATERIAL,
            fields=fields,
            source="rule",
            is_correction=bool(_CORRECTION_RE.search(t)),
            confidence=0.9,
            spans=spans,
            rule_intent=INTENT_SUBMIT_MATERIAL,
        )

    if _looks_like_qa_question(t):
        conf = 0.88 if looks_like_strong_biz_qa(t) else 0.8
        return IntentResult(
            intent=INTENT_QA,
            source="rule",
            confidence=conf,
            spans=spans,
            rule_intent=INTENT_QA,
        )

    if text_looks_like_material_submit(t):
        return IntentResult(
            intent=INTENT_UNCLEAR_MATERIAL,
            source="rule",
            confidence=0.6,
            spans=spans,
            rule_intent=INTENT_UNCLEAR_MATERIAL,
        )

    collecting = status in ("COLLECTING", "REVIEW")
    if collecting and text_has_material_keyword(t):
        return IntentResult(
            intent=INTENT_UNCLEAR_MATERIAL,
            source="rule",
            confidence=0.55,
            spans=spans,
            rule_intent=INTENT_UNCLEAR_MATERIAL,
        )

    return IntentResult(
        intent=INTENT_QA,
        source="rule",
        confidence=0.55,
        spans=spans,
        rule_intent=INTENT_QA,
    )


def merge_intent_with_veto(
    rule: IntentResult,
    model: IntentResult | None,
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> IntentResult:
    """规则与小模型合并；否决护栏禁止覆盖会话事实路径。"""
    min_conf = float(getattr(settings, "wework_intent_min_confidence", 0.55) or 0.55)
    out = IntentResult(
        intent=rule.intent,
        fields=dict(rule.fields or {}),
        source="rule",
        reply_mode=rule.reply_mode or "",
        is_correction=rule.is_correction,
        confidence=rule.confidence,
        spans=list(rule.spans or []),
        veto_applied=[],
        rule_intent=rule.intent,
        rule_mode=rule.reply_mode or "",
        model_intent="",
        model_mode="",
        model_confidence=0.0,
    )
    if model is None:
        return out

    out.model_intent = model.intent
    out.model_mode = model.reply_mode or ""
    out.model_confidence = model.confidence
    if model.confidence < min_conf:
        out.veto_applied.append("low_confidence_drop_model")
        return out

    # 默认采用模型，再逐项否决
    final_intent = model.intent
    final_mode = model.reply_mode or ""
    final_fields = dict(model.fields or {}) or dict(rule.fields or {})
    final_corr = bool(model.is_correction or rule.is_correction)
    final_spans = list(model.spans or []) or list(rule.spans or [])
    veto: list[str] = []

    if (
        rule.intent == INTENT_ASK_PROGRESS
        and rule.reply_mode == REPLY_KNOWLEDGE_CHECKLIST
        and not _SESSION_MARKER_RE.search(text or "")
    ):
        if final_mode == REPLY_MISSING or (
            final_intent == INTENT_ASK_PROGRESS and final_mode == REPLY_MISSING
        ):
            veto.append("keep_knowledge")
            final_intent = INTENT_ASK_PROGRESS
            final_mode = REPLY_KNOWLEDGE_CHECKLIST

    if rule.intent == INTENT_ASK_PROGRESS and rule.reply_mode in (
        REPLY_MISSING,
        REPLY_RECEIVED,
        REPLY_CASE_STATUS,
        REPLY_FULL_PROGRESS,
    ):
        if final_intent == INTENT_QA:
            veto.append("keep_session_progress")
            final_intent = rule.intent
            final_mode = rule.reply_mode

    if rule.intent == INTENT_QA and looks_like_strong_biz_qa(text):
        if final_intent in (INTENT_UNCLEAR_MATERIAL, INTENT_SUBMIT_MATERIAL) and not final_fields:
            veto.append("keep_strong_biz_qa")
            final_intent = INTENT_QA
            final_mode = ""

    if rule.intent == INTENT_SUBMIT_MATERIAL and rule.fields:
        if final_intent != INTENT_SUBMIT_MATERIAL:
            veto.append("keep_submit_fields")
            final_intent = INTENT_SUBMIT_MATERIAL
            final_fields = dict(rule.fields)
            final_corr = rule.is_correction

    if final_intent == INTENT_ASK_PROGRESS and final_mode not in VALID_REPLY_MODES:
        final_mode = resolve_reply_mode(text, status, has_materials=has_materials)

    if final_intent == INTENT_SUBMIT_MATERIAL and not final_fields:
        final_intent = INTENT_UNCLEAR_MATERIAL

    out.intent = final_intent
    out.reply_mode = final_mode if final_intent == INTENT_ASK_PROGRESS else ""
    out.fields = final_fields
    out.is_correction = final_corr
    out.spans = final_spans
    out.confidence = max(rule.confidence, model.confidence)
    out.source = "merge" if veto else "llm"
    out.veto_applied = veto
    return out


def classify_intent(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> IntentResult:
    """规则优先；低置信/歧义时小模型 + 否决合并。"""
    rule = classify_intent_rules(text, status, has_materials=has_materials)
    # 高置信规则短路
    if rule.confidence >= 0.85 and rule.intent != INTENT_UNCLEAR_MATERIAL:
        # 混句仍允许模型补 spans（可选）：多行且含 session+biz 时试一次
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        mixed = len(lines) >= 2 and batch_has_mixed_session_and_biz(lines)
        if not mixed:
            return rule

    if not _should_try_llm(text, status) and rule.confidence >= 0.85:
        return rule

    if not settings.wework_intent_llm_fallback:
        return rule

    model = _llm_classify(text, status, has_materials=has_materials)
    if model and model.intent == INTENT_SUBMIT_MATERIAL and not model.fields:
        model.intent = INTENT_UNCLEAR_MATERIAL
    return merge_intent_with_veto(
        rule, model, text, status, has_materials=has_materials
    )


def classify_intent_signal(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> IntentResult:
    """企业级入口别名。"""
    return classify_intent(text, status, has_materials=has_materials)
