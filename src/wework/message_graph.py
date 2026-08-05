"""消息意图分流：classify(+veto) → ActionPlan（兼容旧 MessageRouteResult）"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from src.wework.intent_planner import (
    STEP_ENQUEUE_QA,
    STEP_QUEUED_TIP,
    STEP_REPLY_PROGRESS,
    STEP_SEND_GREETING,
    STEP_SEND_UNCLEAR,
    STEP_UPSERT_MATERIALS,
    ActionPlan,
    build_action_plan,
)
from src.wework.intent_router import (
    INTENT_ASK_PROGRESS,
    INTENT_GREETING,
    INTENT_QA,
    INTENT_SUBMIT_MATERIAL,
    INTENT_UNCLEAR_MATERIAL,
    REPLY_FULL_PROGRESS,
    VALID_REPLY_MODES,
    classify_intent,
    resolve_reply_mode,
)

logger = logging.getLogger(__name__)

ReplyKind = Literal["progress", "material_update", "unclear_hint", "qa", "greeting"]
RouteAction = Literal[
    "ask_progress",
    "submit_material",
    "unclear_material",
    "qa",
    "greeting",
]


@dataclass
class MessageRouteResult:
    action: RouteAction
    fields: dict[str, str] = field(default_factory=dict)
    source: str = "rule"
    reply_kind: ReplyKind = "qa"
    reply_mode: str = ""
    is_correction: bool = False
    plan: ActionPlan | None = None
    confidence: float = 0.0
    veto_applied: list[str] = field(default_factory=list)


def reset_message_graph() -> None:
    """兼容旧测试钩子（图已简化为函数编排）。"""
    return None


def _primary_from_plan(plan: ActionPlan, text: str, status: str, has_materials: bool) -> MessageRouteResult:
    signal = plan.signal
    source = (signal.source if signal else "rule") or "rule"
    confidence = float(signal.confidence if signal else 0.0)
    veto = list(signal.veto_applied if signal else [])
    fields = dict(signal.fields if signal else {})
    is_corr = bool(signal.is_correction if signal else False)

    kinds = plan.step_kinds
    if STEP_UPSERT_MATERIALS in kinds:
        step = next(s for s in plan.steps if s.kind == STEP_UPSERT_MATERIALS)
        return MessageRouteResult(
            action=INTENT_SUBMIT_MATERIAL,
            fields=dict(step.fields or fields),
            source=source,
            reply_kind="material_update",
            is_correction=step.is_correction or is_corr,
            plan=plan,
            confidence=confidence,
            veto_applied=veto,
        )
    if STEP_REPLY_PROGRESS in kinds:
        step = next(s for s in plan.steps if s.kind == STEP_REPLY_PROGRESS)
        mode = step.reply_mode or REPLY_FULL_PROGRESS
        if mode not in VALID_REPLY_MODES:
            mode = resolve_reply_mode(text, status, has_materials=has_materials)
        return MessageRouteResult(
            action=INTENT_ASK_PROGRESS,
            source=source,
            reply_kind="progress",
            reply_mode=mode,
            plan=plan,
            confidence=confidence,
            veto_applied=veto,
        )
    if STEP_SEND_UNCLEAR in kinds:
        return MessageRouteResult(
            action=INTENT_UNCLEAR_MATERIAL,
            source=source,
            reply_kind="unclear_hint",
            plan=plan,
            confidence=confidence,
            veto_applied=veto,
        )
    if STEP_SEND_GREETING in kinds:
        return MessageRouteResult(
            action=INTENT_GREETING,
            source=source,
            reply_kind="greeting",
            plan=plan,
            confidence=confidence,
            veto_applied=veto,
        )
    if STEP_ENQUEUE_QA in kinds or STEP_QUEUED_TIP in kinds:
        return MessageRouteResult(
            action=INTENT_QA,
            source=source,
            reply_kind="qa",
            plan=plan,
            confidence=confidence,
            veto_applied=veto,
        )
    return MessageRouteResult(
        action=INTENT_QA,
        source=source,
        reply_kind="qa",
        plan=plan,
        confidence=confidence,
        veto_applied=veto,
    )


def route_incoming_text(
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> MessageRouteResult:
    """分类 + 规划，返回供状态机执行的结果（含 ActionPlan）。"""
    signal = classify_intent(
        text, status=status, has_materials=has_materials,
    )
    plan = build_action_plan(
        signal, text, status, has_materials=has_materials,
    )
    result = _primary_from_plan(plan, text, status, has_materials)
    logger.debug(
        "message_route action=%s reply_mode=%s steps=%s source=%s veto=%s",
        result.action,
        result.reply_mode,
        plan.step_kinds,
        result.source,
        result.veto_applied,
    )
    return result
