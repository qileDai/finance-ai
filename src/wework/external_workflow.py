"""外部群确认后：打包材料 + ICRIS 注册（供队列 Worker 调用）"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import settings
from src.materials.aggregator import aggregate_company_data, collect_attachment_paths
from src.materials.packager import package_materials
from src.storage.db import ExternalGroupStore
from src.workflow.steps import RegistrationWorkflow, WorkflowContext
from src.wework.external_client import WeWorkExternalClient

logger = logging.getLogger(__name__)


def _company_data_from_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """从 job.payload_json 解析 company_data；无效则 None。"""
    raw = job.get("payload_json") or ""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("job #%s payload_json 解析失败", job.get("id"))
        return None
    return data if isinstance(data, dict) else None


@dataclass
class ExternalGroupWorkflow:
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    external: WeWorkExternalClient = field(default_factory=WeWorkExternalClient)
    workflow: RegistrationWorkflow = field(default_factory=RegistrationWorkflow)

    def prepare_package(
        self,
        roomid: str,
        *,
        customer_id: str = "",
        company_data: dict[str, Any] | None = None,
    ) -> tuple[WorkflowContext, Path]:
        """打包材料并落库 package_dir；不启动浏览器。"""
        materials = self.store.get_materials(roomid)
        if company_data is None:
            company_data = aggregate_company_data(materials)
        attachment_paths = [
            Path(p) for p in collect_attachment_paths(materials) if Path(p).exists()
        ]

        ctx = WorkflowContext(
            chat_id=roomid,
            customer_id=customer_id,
            company_data=company_data,
        )
        ctx.log("=== 外部群材料确认后流程（打包）===")

        package_dir = package_materials(
            company_data, source_files=attachment_paths or None
        )
        ctx.package_dir = package_dir
        ctx.log(f"材料包: {package_dir}")

        self.store.upsert_group(
            roomid,
            company_name=company_data.get("company_name_en", "")
            or company_data.get("company_name_cn", ""),
            package_dir=str(package_dir),
            status="QUEUED",
        )
        return ctx, package_dir

    def run_icris_job(
        self,
        job: dict[str, Any],
        *,
        force_isolated_browser: bool = True,
    ) -> WorkflowContext:
        """执行单个注册任务：优先用 payload 快照 → 打包（若无）→ ICRIS。"""
        roomid = str(job.get("roomid") or "")
        customer_id = str(job.get("customer_id") or "")
        dry_run = bool(int(job.get("dry_run", 1) or 0))
        allow_submit = bool(int(job.get("allow_submit", 0) or 0)) and (not dry_run)

        snapshot = _company_data_from_job(job)
        package_dir_str = str(job.get("package_dir") or "").strip()

        if snapshot is not None:
            if package_dir_str and Path(package_dir_str).is_dir():
                ctx = WorkflowContext(
                    chat_id=roomid,
                    customer_id=customer_id,
                    company_data=snapshot,
                    package_dir=Path(package_dir_str),
                )
                ctx.log(f"使用 payload 快照 + 复用材料包: {package_dir_str}")
            else:
                ctx, package_dir = self.prepare_package(
                    roomid, customer_id=customer_id, company_data=snapshot
                )
                package_dir_str = str(package_dir)
                ctx.log("使用 payload 快照重新打包")
        elif package_dir_str and Path(package_dir_str).is_dir():
            materials = self.store.get_materials(roomid)
            company_data = aggregate_company_data(materials)
            ctx = WorkflowContext(
                chat_id=roomid,
                customer_id=customer_id,
                company_data=company_data,
                package_dir=Path(package_dir_str),
            )
            ctx.log(f"复用材料包: {package_dir_str}")
        else:
            ctx, package_dir = self.prepare_package(roomid, customer_id=customer_id)
            package_dir_str = str(package_dir)

        self.store.upsert_group(
            roomid,
            company_name=(ctx.company_data or {}).get("company_name_en", "")
            or (ctx.company_data or {}).get("company_name_cn", "")
            or str(job.get("company_name") or ""),
            package_dir=package_dir_str,
            status="HANDOFF",
        )

        ctx = self.workflow.step_icris_register(
            ctx,
            dry_run=dry_run,
            allow_submit=allow_submit,
            force_isolated_browser=force_isolated_browser,
        )
        return ctx

    def notify_job_result(
        self,
        job: dict[str, Any],
        *,
        ok: bool,
        package_dir: str = "",
        error: str = "",
    ) -> None:
        roomid = str(job.get("roomid") or "")
        dry_run = bool(int(job.get("dry_run", 1) or 0))
        allow_submit = bool(int(job.get("allow_submit", 0) or 0)) and (not dry_run)
        source = str(job.get("source") or "").strip().lower()
        owner = (self.store.get_group(roomid) or {}).get("owner_userid") or None
        company = ""
        g = self.store.get_group(roomid) or {}
        company = str(g.get("company_name") or job.get("company_name") or "")
        job_id = job.get("id")

        # 后台快速注册无企微会话，跳过客户侧通知
        if source != "admin":
            if ok:
                if allow_submit:
                    customer_msg = (
                        f"【任务 #{job_id}】材料已确认并办理完成。\n"
                        "ICRIS 账号注册流程已执行（含提交开关开启）。\n"
                        "如页面有异常，专员将跟进复核。"
                    )
                else:
                    customer_msg = (
                        f"【任务 #{job_id}】材料已确认并打包完成。\n"
                        "ICRIS 注册表单已自动填写（预览模式，未点击最终提交）。\n"
                        "后续将由专员复核后人工提交，请耐心等候。"
                    )
                try:
                    self.external.send_session_text(
                        roomid, customer_msg, sender_userid=owner
                    )
                except Exception as e:
                    logger.warning("任务成功通知客户失败 room=%s: %s", roomid, e)
            else:
                try:
                    self.external.send_session_text(
                        roomid,
                        f"【任务 #{job_id}】自动办理未成功，专员已收到通知。"
                        "您可继续咨询业务问题，或回复「重新办理」再次排队；"
                        "也可回复「转人工」。",
                        sender_userid=owner,
                    )
                except Exception as e:
                    logger.warning("任务失败通知客户失败 room=%s: %s", roomid, e)

        notify_id = settings.notify_colleague_open_id or owner
        if notify_id:
            try:
                self.external.send_text_to_user(
                    str(notify_id),
                    f"【注册任务 {'成功' if ok else '失败'}】#{job_id}\n"
                    f"群/会话 {roomid}\n"
                    f"公司: {company}\n"
                    f"材料包: {package_dir or job.get('package_dir') or ''}\n"
                    f"dry_run={dry_run} allow_submit={allow_submit}\n"
                    + (f"错误: {error}" if error else ""),
                )
            except Exception as e:
                logger.warning("通知专员失败: %s", e)

    def run_after_confirm(self, roomid: str, customer_id: str = "") -> WorkflowContext:
        """兼容旧调用：同步打包+ICRIS（不经队列）。新路径请用入队 + Worker。"""
        job = {
            "id": 0,
            "roomid": roomid,
            "customer_id": customer_id,
            "dry_run": 1 if settings.dry_run else 0,
            "allow_submit": 1 if settings.icris_allow_submit else 0,
            "package_dir": "",
        }
        try:
            ctx = self.run_icris_job(job, force_isolated_browser=False)
            self.notify_job_result(
                job,
                ok=True,
                package_dir=str(ctx.package_dir or ""),
            )
            return ctx
        except Exception as e:
            logger.exception("ICRIS 注册失败 roomid=%s", roomid)
            self.notify_job_result(job, ok=False, error=str(e))
            raise
