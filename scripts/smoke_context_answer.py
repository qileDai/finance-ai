# -*- coding: utf-8 -*-
"""会话上下文应答 + 意图 reply_mode + ActionPlan 冒烟验收"""
from __future__ import annotations

import json
from pathlib import Path

from src.wework.message_graph import reset_message_graph, route_incoming_text
from src.wework.intent_planner import build_action_plan
from src.wework.intent_router import (
    INTENT_ASK_PROGRESS,
    INTENT_GREETING,
    INTENT_QA,
    INTENT_SUBMIT_MATERIAL,
    REPLY_CASE_STATUS,
    REPLY_FULL_PROGRESS,
    REPLY_KNOWLEDGE_CHECKLIST,
    REPLY_MISSING,
    REPLY_RECEIVED,
    batch_has_mixed_session_and_biz,
    classify_intent,
    classify_intent_rules,
    looks_like_session_state_query,
    merge_intent_with_veto,
    resolve_reply_mode,
    IntentResult,
)
from src.materials.checklist import (
    format_case_status_text,
    format_knowledge_checklist_text,
    format_missing_text,
    format_progress_text,
    format_received_text,
    format_materials_snapshot,
    progress_summary,
)
from src.agent.context_rewrite import rewrite_with_context

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "intent_routing_cases.json"


def _assert_golden() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(cases) >= 40, f"golden set too small: {len(cases)}"
    for i, c in enumerate(cases):
        text = c["text"]
        status = c.get("status") or ""
        has_m = bool(c.get("has_materials"))
        r = classify_intent(text, status=status, has_materials=has_m)
        assert r.intent == c["intent"], (i, text, r.intent, c["intent"])
        expect_mode = c.get("reply_mode") or ""
        assert (r.reply_mode or "") == expect_mode, (
            i,
            text,
            r.reply_mode,
            expect_mode,
        )
        route = route_incoming_text(text, status=status, has_materials=has_m)
        assert route.action == c["intent"], (i, text, route.action, c["intent"])
        if c["intent"] == INTENT_ASK_PROGRESS:
            assert route.reply_mode == expect_mode, (
                i,
                text,
                route.reply_mode,
                expect_mode,
            )
        if c.get("expect_fields"):
            for fk in c["expect_fields"]:
                assert fk in r.fields, (i, text, r.fields, fk)
        if c.get("is_correction"):
            assert r.is_correction, (i, text, "expected correction")
        expect_steps = c.get("plan_steps")
        if expect_steps is not None:
            assert route.plan is not None, (i, text)
            assert route.plan.step_kinds == expect_steps, (
                i,
                text,
                route.plan.step_kinds,
                expect_steps,
            )


def _assert_veto() -> None:
    rule = classify_intent_rules(
        "香港注册需要哪些资料", "WELCOMED", has_materials=False
    )
    fake_model = IntentResult(
        intent=INTENT_ASK_PROGRESS,
        reply_mode=REPLY_MISSING,
        source="llm",
        confidence=0.9,
    )
    merged = merge_intent_with_veto(
        rule,
        fake_model,
        "香港注册需要哪些资料",
        "WELCOMED",
        has_materials=False,
    )
    assert merged.reply_mode == REPLY_KNOWLEDGE_CHECKLIST
    assert "keep_knowledge" in merged.veto_applied


def main() -> None:
    reset_message_graph()
    _assert_golden()
    _assert_veto()

    assert classify_intent("你好").intent == INTENT_GREETING
    assert classify_intent("注册要多久").intent == INTENT_QA
    assert looks_like_session_state_query("收集到哪些资料")
    assert not looks_like_session_state_query("注册要多久")
    assert not looks_like_session_state_query(
        "香港注册需要哪些资料", has_materials=False
    )
    assert resolve_reply_mode("香港注册需要哪些资料") == REPLY_KNOWLEDGE_CHECKLIST
    assert resolve_reply_mode(
        "需要哪些资料", "COLLECTING", has_materials=True
    ) == REPLY_MISSING
    assert resolve_reply_mode("办得怎么样了") == REPLY_CASE_STATUS
    assert resolve_reply_mode("收集到哪些了") == REPLY_RECEIVED
    assert resolve_reply_mode("还缺啥") == REPLY_MISSING
    assert resolve_reply_mode("进度") == REPLY_FULL_PROGRESS

    assert batch_has_mixed_session_and_biz(["还缺啥", "开户要多久"])
    assert not batch_has_mixed_session_and_biz(["开户要多久", "注册费用多少"])

    sig = classify_intent("还缺啥\n开户要多久", "COLLECTING", has_materials=True)
    plan = build_action_plan(sig, "还缺啥\n开户要多久", "COLLECTING", has_materials=True)
    assert plan.step_kinds == ["reply_progress", "enqueue_qa"], plan.step_kinds

    # HUMAN 不封锁业务 QA
    human_route = route_incoming_text(
        "开户要多久", status="HUMAN", has_materials=True,
    )
    assert human_route.action == INTENT_QA
    assert human_route.plan and human_route.plan.step_kinds == ["enqueue_qa"]
    human_prog = route_incoming_text(
        "还缺啥", status="HUMAN", has_materials=True,
    )
    assert human_prog.action == INTENT_ASK_PROGRESS
    assert human_prog.plan and "reply_progress" in human_prog.plan.step_kinds

    text = format_progress_text({})
    assert "尚未收到" in text and "还需要" in text
    assert "已收集" in text
    assert "当前会话" in text

    mats = {
        "company_name_en": {"field_value": "ABC LTD", "status": "received"},
        "directors": {"field_value": "张三", "status": "received"},
        "id_number": {"field_value": "A123456(7)", "status": "received"},
        "id_card_front": {
            "file_path": "/tmp/x.jpg",
            "status": "needs_review",
        },
    }
    p = progress_summary(mats)
    assert "拟用公司英文名" in p["received_labels"]
    assert p.get("received_details")
    assert p.get("needs_review_labels")

    ft = format_progress_text(mats, mode=REPLY_FULL_PROGRESS, channel="kf")
    assert "已收集" in ft and "拟用公司英文名" in ft and "ABC LTD" in ft
    assert "还需要" in ft

    recv = format_received_text(mats, channel="kf")
    assert "ABC LTD" in recv
    grp = format_received_text(mats, channel="group")
    assert "A123456(7)" not in grp

    miss = format_missing_text(mats, channel="kf")
    assert "还需要" in miss

    know = format_knowledge_checklist_text()
    assert "香港公司注册" in know or "材料清单" in know

    case = format_case_status_text(
        group_status="QUEUED",
        job={"id": 9, "status": "pending"},
        materials=mats,
        channel="kf",
    )
    assert "办理" in case or "任务" in case

    snap = format_materials_snapshot(mats)
    assert "已收集" in snap and "还需要" in snap

    corr = classify_intent(
        "邮箱改成 new@x.com", status="COLLECTING", has_materials=True
    )
    assert corr.intent == INTENT_SUBMIT_MATERIAL
    assert corr.is_correction
    assert corr.fields.get("contact_email") == "new@x.com"

    q = rewrite_with_context(
        "刚才说的要多久",
        history=["客户: 注册要多久", "助手: 约4个工作日"],
    )
    assert "注册" in q and "多久" in q, q

    print("ALL OK")


if __name__ == "__main__":
    main()
