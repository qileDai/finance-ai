"""确定性意图规划：IntentSignal → ActionPlan（无 LLM）"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.wework.intent_router import (
    INTENT_ASK_PROGRESS,
    INTENT_GREETING,
    INTENT_QA,
    INTENT_SUBMIT_MATERIAL,
    INTENT_UNCLEAR_MATERIAL,
    REPLY_FULL_PROGRESS,
    REPLY_KNOWLEDGE_CHECKLIST,
    VALID_REPLY_MODES,
    IntentResult,
    IntentSpan,
    heuristic_spans,
    resolve_reply_mode,
)

STEP_SEND_GREETING = "send_greeting"
STEP_REPLY_PROGRESS = "reply_progress"
STEP_UPSERT_MATERIALS = "upsert_materials"
STEP_SEND_UNCLEAR = "send_unclear_hint"
STEP_ENQUEUE_QA = "enqueue_qa"
STEP_QUEUED_TIP = "noop_queued_tip"

QUEUED_LIKE = frozenset({"CONFIRMED", "QUEUED", "HANDOFF"})


@dataclass
class PlanStep:
    kind: str
    reply_mode: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    text: str = ""
    is_correction: bool = False


@dataclass
class ActionPlan:
    steps: list[PlanStep] = field(default_factory=list)
    signal: IntentResult | None = None

    @property
    def step_kinds(self) -> list[str]:
        return [s.kind for s in self.steps]


def _session_text(spans: list[IntentSpan], fallback: str) -> str:
    parts = [s.text for s in spans if s.label == "session"]
    return "\n".join(parts) if parts else fallback


def _biz_text(spans: list[IntentSpan], fallback: str) -> str:
    parts = [s.text for s in spans if s.label == "biz"]
    return "\n".join(parts) if parts else fallback


def _material_text(spans: list[IntentSpan], fallback: str) -> str:
    parts = [s.text for s in spans if s.label == "material"]
    return "\n".join(parts) if parts else fallback


def build_action_plan(
    signal: IntentResult,
    text: str,
    status: str = "",
    *,
    has_materials: bool = False,
) -> ActionPlan:
    """根据合并后的 IntentSignal 与状态产出有序动作计划。"""
    t = (text or "").strip()
    spans = list(signal.spans or []) or heuristic_spans(
        t, status, has_materials=has_materials
    )
    has_session = any(s.label == "session" for s in spans)
    has_biz = any(s.label == "biz" for s in spans)
    has_material_span = any(s.label == "material" for s in spans)
    steps: list[PlanStep] = []

    # HUMAN 与普通态同一套 plan（不封锁 QA）；QUEUED 门禁仍生效

    # 材料入库
    if signal.intent == INTENT_SUBMIT_MATERIAL and signal.fields:
        steps.append(
            PlanStep(
                kind=STEP_UPSERT_MATERIALS,
                fields=dict(signal.fields),
                is_correction=bool(signal.is_correction),
                text=_material_text(spans, t) if has_material_span else t,
            )
        )
    elif signal.intent == INTENT_UNCLEAR_MATERIAL and not signal.fields:
        steps.append(PlanStep(kind=STEP_SEND_UNCLEAR, text=t))

    # 混句：session + biz
    if has_session and has_biz:
        sess = _session_text(spans, t)
        mode = resolve_reply_mode(sess, status, has_materials=has_materials)
        if signal.reply_mode in VALID_REPLY_MODES and signal.intent == INTENT_ASK_PROGRESS:
            # 若主意图已是 knowledge，优先用之
            if signal.reply_mode == REPLY_KNOWLEDGE_CHECKLIST or mode == REPLY_KNOWLEDGE_CHECKLIST:
                mode = REPLY_KNOWLEDGE_CHECKLIST
            elif signal.reply_mode != REPLY_KNOWLEDGE_CHECKLIST:
                mode = signal.reply_mode or mode
        steps.append(PlanStep(kind=STEP_REPLY_PROGRESS, reply_mode=mode, text=sess))
        steps.append(
            PlanStep(kind=STEP_ENQUEUE_QA, text=_biz_text(spans, t))
        )
        return ActionPlan(steps=_dedupe_steps(steps), signal=signal)

    # 主意图分支
    if signal.intent == INTENT_GREETING and not steps:
        steps.append(PlanStep(kind=STEP_SEND_GREETING, text=t))
        return ActionPlan(steps=steps, signal=signal)

    if signal.intent == INTENT_ASK_PROGRESS:
        mode = signal.reply_mode or resolve_reply_mode(
            t, status, has_materials=has_materials
        )
        if mode not in VALID_REPLY_MODES:
            mode = REPLY_FULL_PROGRESS
        steps.append(PlanStep(kind=STEP_REPLY_PROGRESS, reply_mode=mode, text=t))
        return ActionPlan(steps=_dedupe_steps(steps), signal=signal)

    if signal.intent == INTENT_QA:
        if status in QUEUED_LIKE and not has_biz and not _looks_biz(t):
            # 办理中闲聊
            steps.append(PlanStep(kind=STEP_QUEUED_TIP, text=t))
        else:
            steps.append(PlanStep(kind=STEP_ENQUEUE_QA, text=_biz_text(spans, t) or t))
        return ActionPlan(steps=_dedupe_steps(steps), signal=signal)

    if signal.intent == INTENT_SUBMIT_MATERIAL and signal.fields:
        return ActionPlan(steps=_dedupe_steps(steps), signal=signal)

    if signal.intent == INTENT_UNCLEAR_MATERIAL:
        return ActionPlan(steps=_dedupe_steps(steps), signal=signal)

    # 兜底
    if not steps:
        if status in QUEUED_LIKE:
            steps.append(PlanStep(kind=STEP_QUEUED_TIP, text=t))
        else:
            steps.append(PlanStep(kind=STEP_ENQUEUE_QA, text=t))
    return ActionPlan(steps=_dedupe_steps(steps), signal=signal)


def _looks_biz(text: str) -> bool:
    from src.wework.intent_router import looks_like_strong_biz_qa, _looks_like_qa_question

    return bool(looks_like_strong_biz_qa(text) or _looks_like_qa_question(text))


def _dedupe_steps(steps: list[PlanStep]) -> list[PlanStep]:
    """同 kind 最多保留一步（材料/进度/QA 各 1）。"""
    seen: set[str] = set()
    out: list[PlanStep] = []
    for s in steps:
        if s.kind in seen:
            continue
        # knowledge 与 missing 不会同出；若意外双 progress，保留第一个
        seen.add(s.kind)
        out.append(s)
    return out
