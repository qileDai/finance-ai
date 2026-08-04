"""LangGraph 消息意图分流：classify → material/progress/unclear/qa（无副作用）"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from src.materials.form_parser import extract_material_fields
from src.wework.intent_router import (
    INTENT_ASK_PROGRESS,
    INTENT_QA,
    INTENT_SUBMIT_MATERIAL,
    INTENT_UNCLEAR_MATERIAL,
    classify_intent,
)

logger = logging.getLogger(__name__)

ReplyKind = Literal["progress", "material_update", "unclear_hint", "qa"]
RouteAction = Literal[
    "ask_progress",
    "submit_material",
    "unclear_material",
    "qa",
]


class MessageGraphState(TypedDict, total=False):
    text: str
    status: str
    intent: str
    fields: dict[str, str]
    source: str
    action: str
    reply_kind: str


@dataclass
class MessageRouteResult:
    action: RouteAction
    fields: dict[str, str] = field(default_factory=dict)
    source: str = "rule"
    reply_kind: ReplyKind = "qa"


def _node_classify(state: MessageGraphState) -> dict[str, Any]:
    result = classify_intent(state.get("text") or "", status=state.get("status") or "")
    return {
        "intent": result.intent,
        "fields": dict(result.fields or {}),
        "source": result.source,
    }


def _route_by_intent(state: MessageGraphState) -> str:
    intent = state.get("intent") or INTENT_QA
    if intent == INTENT_ASK_PROGRESS:
        return "prepare_progress"
    if intent == INTENT_SUBMIT_MATERIAL:
        return "extract_fields"
    if intent == INTENT_UNCLEAR_MATERIAL:
        return "prepare_unclear"
    return "prepare_qa"


def _node_extract_fields(state: MessageGraphState) -> dict[str, Any]:
    fields = dict(state.get("fields") or {})
    if not fields:
        fields = extract_material_fields(state.get("text") or "")
    # 抽不出字段时降为 unclear，避免空 submit
    if not fields:
        return {
            "fields": {},
            "intent": INTENT_UNCLEAR_MATERIAL,
            "action": INTENT_UNCLEAR_MATERIAL,
            "reply_kind": "unclear_hint",
        }
    return {"fields": fields}


def _node_prepare_progress(state: MessageGraphState) -> dict[str, Any]:
    return {
        "action": INTENT_ASK_PROGRESS,
        "reply_kind": "progress",
    }


def _node_prepare_material(state: MessageGraphState) -> dict[str, Any]:
    # extract 已可能改写为 unclear
    if (state.get("action") == INTENT_UNCLEAR_MATERIAL) or (
        state.get("intent") == INTENT_UNCLEAR_MATERIAL and not (state.get("fields") or {})
    ):
        return {
            "action": INTENT_UNCLEAR_MATERIAL,
            "reply_kind": "unclear_hint",
        }
    return {
        "action": INTENT_SUBMIT_MATERIAL,
        "reply_kind": "material_update",
        "fields": dict(state.get("fields") or {}),
    }


def _node_prepare_unclear(state: MessageGraphState) -> dict[str, Any]:
    # unclear 路径仍尝试补抽字段；有字段则升级为 material_update
    fields = dict(state.get("fields") or {})
    if not fields:
        fields = extract_material_fields(state.get("text") or "")
    if fields:
        return {
            "fields": fields,
            "action": INTENT_SUBMIT_MATERIAL,
            "reply_kind": "material_update",
        }
    return {
        "fields": {},
        "action": INTENT_UNCLEAR_MATERIAL,
        "reply_kind": "unclear_hint",
    }


def _node_prepare_qa(state: MessageGraphState) -> dict[str, Any]:
    return {
        "action": INTENT_QA,
        "reply_kind": "qa",
    }


def _after_extract(state: MessageGraphState) -> str:
    if state.get("action") == INTENT_UNCLEAR_MATERIAL or (
        state.get("intent") == INTENT_UNCLEAR_MATERIAL and not (state.get("fields") or {})
    ):
        return "prepare_unclear"
    return "prepare_material"


def build_message_graph():
    g = StateGraph(MessageGraphState)
    g.add_node("classify", _node_classify)
    g.add_node("extract_fields", _node_extract_fields)
    g.add_node("prepare_progress", _node_prepare_progress)
    g.add_node("prepare_material", _node_prepare_material)
    g.add_node("prepare_unclear", _node_prepare_unclear)
    g.add_node("prepare_qa", _node_prepare_qa)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify",
        _route_by_intent,
        {
            "prepare_progress": "prepare_progress",
            "extract_fields": "extract_fields",
            "prepare_unclear": "prepare_unclear",
            "prepare_qa": "prepare_qa",
        },
    )
    g.add_conditional_edges(
        "extract_fields",
        _after_extract,
        {
            "prepare_material": "prepare_material",
            "prepare_unclear": "prepare_unclear",
        },
    )
    g.add_edge("prepare_progress", END)
    g.add_edge("prepare_material", END)
    g.add_edge("prepare_unclear", END)
    g.add_edge("prepare_qa", END)
    return g.compile()


_compiled_graph = None


def get_message_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_message_graph()
    return _compiled_graph


def route_incoming_text(text: str, status: str = "") -> MessageRouteResult:
    """运行意图分流图，返回供状态机执行的动作（无 IO）。"""
    graph = get_message_graph()
    out = graph.invoke(
        {
            "text": (text or "").strip(),
            "status": status or "",
            "fields": {},
            "intent": "",
            "source": "rule",
            "action": "",
            "reply_kind": "",
        }
    )
    action = str(out.get("action") or INTENT_QA)
    if action not in (
        INTENT_ASK_PROGRESS,
        INTENT_SUBMIT_MATERIAL,
        INTENT_UNCLEAR_MATERIAL,
        INTENT_QA,
    ):
        action = INTENT_QA
    reply_kind = str(out.get("reply_kind") or "qa")
    if reply_kind not in ("progress", "material_update", "unclear_hint", "qa"):
        reply_kind = "qa"
    result = MessageRouteResult(
        action=action,  # type: ignore[arg-type]
        fields=dict(out.get("fields") or {}),
        source=str(out.get("source") or "rule"),
        reply_kind=reply_kind,  # type: ignore[arg-type]
    )
    logger.debug(
        "message_graph action=%s source=%s fields=%s",
        result.action,
        result.source,
        list(result.fields.keys()),
    )
    return result
