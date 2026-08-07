"""任务编排：路由 QA / 材料 / 转人工"""

from __future__ import annotations

import re

from src.agent.models import AgentContext, QAResult, TaskType
from src.agent.qa_agent import QAAgent

MATERIAL_COMMANDS = {"/资料", "/docs", "/填表", "/form", "/模板", "/template", "/进度", "/progress"}
HUMAN_COMMANDS = {"转人工", "/转人工", "/human"}


class TaskOrchestrator:
    def __init__(self, qa_agent: QAAgent | None = None, store=None) -> None:
        self.qa_agent = qa_agent or QAAgent(store=store)

    @staticmethod
    def classify(text: str) -> TaskType:
        stripped = text.strip()
        if stripped in HUMAN_COMMANDS:
            return TaskType.HUMAN
        if stripped in MATERIAL_COMMANDS:
            return TaskType.MATERIAL
        if stripped in ("确认", "确认无误", "/确认") or re.match(r"^确认[。!！]?$", stripped):
            return TaskType.MATERIAL
        return TaskType.QA

    def run_qa(self, ctx: AgentContext) -> QAResult:
        return self.qa_agent.run(
            ctx.question,
            scope=ctx.scope,
            roomid=ctx.roomid,
            history=ctx.history,
            group_meta=ctx.group_meta,
        )
