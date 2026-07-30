"""工商注册智能体入口"""

from __future__ import annotations

import logging

from src.workflow.steps import RegistrationWorkflow, WorkflowContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class RegistrationAgent:
    """香港公司工商注册智能体"""

    def __init__(self) -> None:
        self.workflow = RegistrationWorkflow()

    def run_full_pipeline(
        self,
        chat_id: str = "mock_chat_001",
        company_data: dict | None = None,
    ) -> WorkflowContext:
        ctx = WorkflowContext(chat_id=chat_id)
        if company_data:
            ctx.company_data = company_data
        return self.workflow.run_all(ctx)

    def run_registration_only(self) -> WorkflowContext:
        """仅运行 ICRIS 账号注册步骤（步骤④）"""
        ctx = WorkflowContext()
        ctx.company_data = __import__(
            "src.materials.packager", fromlist=["load_mock_data"]
        ).load_mock_data()
        return self.workflow.run_step(
            __import__("src.workflow.steps", fromlist=["StepName"]).StepName.ICRIS_REGISTER,
            ctx,
        )
