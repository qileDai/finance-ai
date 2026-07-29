"""外部群确认后触发打包与 ICRIS 注册"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from src.materials.aggregator import aggregate_company_data, collect_attachment_paths
from src.materials.packager import package_materials
from src.storage.db import ExternalGroupStore
from src.workflow.steps import RegistrationWorkflow, WorkflowContext
from src.wework.external_client import WeWorkExternalClient

logger = logging.getLogger(__name__)


@dataclass
class ExternalGroupWorkflow:
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    external: WeWorkExternalClient = field(default_factory=WeWorkExternalClient)
    workflow: RegistrationWorkflow = field(default_factory=RegistrationWorkflow)

    def run_after_confirm(self, roomid: str, customer_id: str = "") -> WorkflowContext:
        """CONFIRMED → 打包 → ICRIS 注册（dry_run）→ 通知群"""
        materials = self.store.get_materials(roomid)
        company_data = aggregate_company_data(materials)
        attachment_paths = [Path(p) for p in collect_attachment_paths(materials) if Path(p).exists()]

        ctx = WorkflowContext(
            chat_id=roomid,
            customer_id=customer_id,
            company_data=company_data,
        )
        ctx.log("=== 外部群材料确认后流程 ===")

        package_dir = package_materials(company_data, source_files=attachment_paths or None)
        ctx.package_dir = package_dir
        ctx.log(f"材料包: {package_dir}")

        self.store.upsert_group(
            roomid,
            company_name=company_data.get("company_name_en", ""),
            package_dir=str(package_dir),
            status="HANDOFF",
        )

        owner = (self.store.get_group(roomid) or {}).get("owner_userid") or None

        try:
            ctx = self.workflow.step_icris_register(ctx)
            icris_user = company_data.get("icris_account", {}).get("username", "")
            self.external.send_group_text(
                roomid,
                "材料已确认并打包完成。\n"
                f"ICRIS 账号注册表单已填写（dry_run={settings.dry_run}，未提交）。\n"
                f"材料包路径: {package_dir}\n"
                f"申请人: {company_data.get('applicant', {}).get('name_en', '')}",
                sender_userid=owner,
            )
        except Exception as e:
            logger.exception("ICRIS 注册失败 roomid=%s", roomid)
            self.external.send_group_text(
                roomid,
                f"材料已打包，但 ICRIS 自动填写失败: {e}\n请专员人工处理。",
                sender_userid=owner,
            )

        notify_id = settings.notify_colleague_open_id or (self.store.get_group(roomid) or {}).get("owner_userid")
        if notify_id:
            self.external.send_text_to_user(
                str(notify_id),
                f"【外部群注册完成】群 {roomid}\n"
                f"公司: {company_data.get('company_name_en', '')}\n"
                f"材料包: {package_dir}",
            )

        return ctx
