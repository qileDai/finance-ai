"""工商注册工作流各步骤"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from src.email.imap_client import EmailClient, IcrisAccount
from src.llm.openai_client import LLMClient
from src.materials.packager import collect_materials_from_dict, load_mock_data, package_materials
from src.wework.client import WeWorkClient
from src.feishu.client import FeishuClient

logger = logging.getLogger(__name__)


class StepName(str, Enum):
    WEWORK_CONTACT = "wework"
    FEISHU_CONTACT = "feishu"
    COLLECT_MATERIALS = "collect"
    CONFIRM_MATERIALS = "confirm"
    PACKAGE = "package"
    ICRIS_REGISTER = "register"
    READ_EMAIL = "email"
    ICRIS_LOGIN = "login"
    NOTIFY = "notify"


@dataclass
class WorkflowContext:
    """工作流上下文，在各步骤间传递数据"""

    chat_id: str = "mock_chat_001"
    customer_id: str = "mock_customer_001"
    company_data: dict[str, Any] = field(default_factory=dict)
    package_dir: Path | None = None
    icris_account: IcrisAccount | None = None
    messages: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        logger.info(msg)
        self.messages.append(msg)


@dataclass
class RegistrationWorkflow:
    """香港公司工商注册完整工作流"""

    llm: LLMClient = field(default_factory=LLMClient)
    wework: WeWorkClient = field(default_factory=WeWorkClient)
    feishu: FeishuClient = field(default_factory=FeishuClient)
    email_client: EmailClient = field(default_factory=EmailClient)

    def step_wework_contact(self, ctx: WorkflowContext) -> WorkflowContext:
        """① 进企微群对接客户，发送材料清单，回答材料问题"""
        ctx.log("=== 步骤① 企微群对接客户 ===")
        self.wework.send_material_checklist(ctx.chat_id)

        # Mock 客户提问
        mock_questions = [
            "香港公司注册地址可以用大陆地址吗？",
            "董事一定要是香港居民吗？",
        ]
        for q in mock_questions:
            self.wework.push_mock_customer_message(ctx.chat_id, q)
            answer = self.llm.answer_material_question(q)
            self.wework.send_group_text(ctx.chat_id, f"【回复】{answer}")
            ctx.log(f"客户问: {q}")
            ctx.log(f"已回复: {answer[:80]}...")

        return ctx

    def step_feishu_contact(self, ctx: WorkflowContext) -> WorkflowContext:
        """① 飞书群对接客户，发送 ICRIS 填写模板"""
        ctx.log("=== 步骤① 飞书群对接客户 ===")
        chat_id = ctx.chat_id or self.feishu.resolve_target_chat_id() or "mock_chat_001"
        ctx.chat_id = chat_id
        self.feishu.send_icris_register_form(chat_id)
        self.feishu.send_group_text(
            chat_id,
            "请按模板填写后，@机器人 发送 /开始注册 + 整段内容。",
        )
        ctx.log("已发送 ICRIS 账号注册填写模板")
        return ctx

    def step_collect_materials(self, ctx: WorkflowContext) -> WorkflowContext:
        """② 搜集材料"""
        ctx.log("=== 步骤② 搜集客户材料 ===")
        ctx.company_data = load_mock_data()
        result = collect_materials_from_dict(ctx.company_data)
        if result["complete"]:
            ctx.log("材料已齐全")
        else:
            ctx.log(f"材料缺失: {result['missing']}")
        return ctx

    def step_confirm_materials(self, ctx: WorkflowContext) -> WorkflowContext:
        """② 和客户确认材料"""
        ctx.log("=== 步骤② 确认材料 ===")
        summary = self.llm.confirm_materials_summary(ctx.company_data)
        self.wework.send_group_text(
            ctx.chat_id,
            f"【材料确认】\n{summary}\n\n请确认以上材料是否正确，回复「确认」即可继续。",
        )
        ctx.log("已发送材料确认消息")
        return ctx

    def step_package(self, ctx: WorkflowContext) -> WorkflowContext:
        """③ 打包材料文件夹"""
        ctx.log("=== 步骤③ 打包材料 ===")
        ctx.package_dir = package_materials(ctx.company_data)
        ctx.log(f"材料包路径: {ctx.package_dir}")
        return ctx

    def step_icris_register(self, ctx: WorkflowContext) -> WorkflowContext:
        """④ ICRIS 账号注册（浏览器填写，不提交）"""
        from src.browser.icris_registration import IcrisRegistrationBot
        from src.materials.packager import load_mock_data

        ctx.log("=== 步骤④ ICRIS 账号注册（Mock 填写，不提交）===")
        if not ctx.company_data:
            ctx.company_data = load_mock_data()
            ctx.log("未提供资料，使用 mock 数据")
        bot = IcrisRegistrationBot(self.llm)
        asyncio.run(bot.run(ctx.company_data))
        ctx.log("ICRIS 注册表单已填写（未提交）")
        return ctx

    def step_read_email(self, ctx: WorkflowContext) -> WorkflowContext:
        """⑤ 读取邮箱获取账号"""
        ctx.log("=== 步骤⑤ 读取邮箱账号 ===")
        mock_account = IcrisAccount(
            username="mock_icris_user",
            password="MockPass@2026!",
            raw_subject="[Mock] ICRIS Registration Confirmation",
        )
        ctx.icris_account = self.email_client.fetch_icris_account(mock_account)
        ctx.log(f"获取账号: {ctx.icris_account.username}")
        return ctx

    def step_icris_login(self, ctx: WorkflowContext) -> WorkflowContext:
        """⑥ 登录 ICRIS 填写材料"""
        from src.browser.icris_login import IcrisLoginBot

        ctx.log("=== 步骤⑥ 登录 ICRIS 填写材料 ===")
        if not ctx.icris_account:
            ctx = self.step_read_email(ctx)
        bot = IcrisLoginBot(self.llm)
        asyncio.run(bot.run(ctx.icris_account, ctx.company_data))
        ctx.log("ICRIS 材料已填写（未提交）")
        return ctx

    def step_notify_colleague(self, ctx: WorkflowContext) -> WorkflowContext:
        """⑦ 核对材料，提醒同事后续操作"""
        ctx.log("=== 步骤⑦ 提醒同事 ===")
        company_name = ctx.company_data.get("company_name_en", "Unknown")
        next_steps = [
            "人工核对 ICRIS 表单填写内容",
            "确认材料附件完整性",
            "客户最终确认后提交注册申请",
            "跟进商业登记证缴费",
        ]
        summary = f"材料包: {ctx.package_dir}" if ctx.package_dir else "无"
        notification = self.llm.generate_colleague_notification(
            company_name, summary, next_steps
        )

        from config.settings import settings

        colleague_id = settings.notify_colleague_open_id or "mock_colleague_001"
        full_msg = (
            f"【工商注册流程完成提醒】\n"
            f"公司: {company_name}\n"
            f"{notification}\n\n"
            f"后续事项:\n" + "\n".join(f"  - {s}" for s in next_steps)
        )
        self.wework.send_text(colleague_id, full_msg)
        ctx.log(f"已通知同事 {colleague_id}")
        return ctx

    def run_all(self, ctx: WorkflowContext | None = None) -> WorkflowContext:
        """运行完整流程"""
        ctx = ctx or WorkflowContext()
        steps = [
            self.step_wework_contact,
            self.step_feishu_contact,
            self.step_collect_materials,
            self.step_confirm_materials,
            self.step_package,
            self.step_icris_register,
            self.step_read_email,
            self.step_icris_login,
            self.step_notify_colleague,
        ]
        for step_fn in steps:
            ctx = step_fn(ctx)
        ctx.log("=== 全流程完成 ===")
        return ctx

    def run_step(self, step: StepName, ctx: WorkflowContext | None = None) -> WorkflowContext:
        """运行单个步骤"""
        ctx = ctx or WorkflowContext()
        step_map = {
            StepName.WEWORK_CONTACT: self.step_wework_contact,
            StepName.FEISHU_CONTACT: self.step_feishu_contact,
            StepName.COLLECT_MATERIALS: self.step_collect_materials,
            StepName.CONFIRM_MATERIALS: self.step_confirm_materials,
            StepName.PACKAGE: self.step_package,
            StepName.ICRIS_REGISTER: self.step_icris_register,
            StepName.READ_EMAIL: self.step_read_email,
            StepName.ICRIS_LOGIN: self.step_icris_login,
            StepName.NOTIFY: self.step_notify_colleague,
        }
        fn = step_map.get(step)
        if not fn:
            raise ValueError(f"未知步骤: {step}")
        return fn(ctx)
