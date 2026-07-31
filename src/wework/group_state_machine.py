"""企业微信外部群会话状态机（Phase 1–3）"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import PROJECT_ROOT, settings
from src.llm.openai_client import LLMClient
from src.materials.aggregator import aggregate_company_data, is_ready_for_confirm
from src.materials.checklist import format_progress_text
from src.materials.form_parser import fields_to_material_rows, parse_registration_form
from src.storage.db import ExternalGroupStore
from src.wework.external_client import WeWorkExternalClient
from src.wework.external_workflow import ExternalGroupWorkflow
from src.wework.kf_session import (
    build_kf_roomid,
    is_kf_session,
    parse_kf_roomid,
)

logger = logging.getLogger(__name__)

GROUP_STATUS_INIT = "INIT"
GROUP_STATUS_WELCOMED = "WELCOMED"
GROUP_STATUS_QA = "QA"
GROUP_STATUS_COLLECTING = "COLLECTING"
GROUP_STATUS_REVIEW = "REVIEW"
GROUP_STATUS_CONFIRMED = "CONFIRMED"
GROUP_STATUS_HANDOFF = "HANDOFF"
GROUP_STATUS_HUMAN = "HUMAN"

WELCOME_ADVISOR_NAME = "邓"


def build_welcome_message(phone: str = "", *, channel: str = "group") -> str:
    phone_display = phone.strip() if phone and phone.strip() else "待补充"
    greeting = "您好" if channel == "kf" else "大家好 / 您好"
    msg = (
        f"{greeting}，我是赢态财务集团 - 香港业务专属服务老师：{WELCOME_ADVISOR_NAME}老师。"
        f"很荣幸能为您服务，接下来由我全程一对一跟进贵司的香港公司注册、开户及年审等香港公司服务事宜。"
        f"为确保服务高效顺畅，我会在每个关键节点主动向您汇报进度。"
        f"我的电话：【{phone_display}】，有任何疑问随时找我哈"
    )
    if channel == "group":
        msg += "详细问题也可点击群名片联系微信客服，AI 将 7×24 自动回复。"
    return msg


PASTE_FORM_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "company_registration_form.md"

DEBOUNCE_SECONDS = 5.0


@dataclass
class PendingBatch:
    roomid: str
    msgids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    from_id: str = ""
    timer: threading.Timer | None = None


@dataclass
class GroupStateMachine:
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    external: WeWorkExternalClient = field(default_factory=WeWorkExternalClient)
    llm: LLMClient = field(default_factory=LLMClient)
    ext_workflow: ExternalGroupWorkflow = field(default_factory=ExternalGroupWorkflow)
    _pending: dict[str, PendingBatch] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _owner(self, roomid: str) -> str | None:
        g = self.store.get_group(roomid) or {}
        return g.get("owner_userid") or self.external.default_owner_userid or None

    def _form_url(self, roomid: str) -> str:
        if not settings.collect_form_enabled:
            raise RuntimeError("H5 表单未启用")
        token = self.store.ensure_form_token(roomid)
        base = (settings.collect_form_base_url or "").strip().rstrip("/")
        if not base:
            base = f"http://127.0.0.1:{settings.wework_external_callback_port}"
        return f"{base}/collect/form/{token}"

    def _maybe_ensure_form_token(self, roomid: str) -> None:
        if settings.collect_form_enabled:
            self.store.ensure_form_token(roomid)

    def _load_paste_template(self) -> str:
        return PASTE_FORM_TEMPLATE_PATH.read_text(encoding="utf-8")

    def _resolve_external_userid(
        self, roomid: str, from_id: str = "",
    ) -> str | None:
        if is_kf_session(roomid):
            parsed = parse_kf_roomid(roomid)
            if parsed:
                return parsed[1]
            rest = roomid.removeprefix("kf:")
            return rest if rest.startswith("wm") else None
        if from_id.startswith("wm"):
            return from_id
        return None

    def _send_paste_template(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        template = self._load_paste_template()
        target = "本会话" if is_kf_session(roomid) else "本群"
        msg = (
            f"【填写模板】请按下方格式填写，完成后整段粘贴到{target}发送：\n\n"
            f"{template}"
        )
        self._safe_send(roomid, msg, to_external_userid=to_external_userid)

    def _send_form_link(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        self._safe_send(
            roomid,
            f"请在线填写注册资料：{self._form_url(roomid)}",
            to_external_userid=to_external_userid,
        )

    def _handle_fill_form_command(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        if settings.collect_form_enabled:
            self._send_form_link(roomid, to_external_userid=to_external_userid)
        else:
            self._send_paste_template(roomid, to_external_userid=to_external_userid)

    def _safe_send(
        self,
        roomid: str,
        content: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        try:
            self.external.send_session_text(
                roomid,
                content,
                sender_userid=self._owner(roomid),
                to_external_userid=to_external_userid,
            )
        except Exception as e:
            logger.warning("会话 %s 发消息失败: %s", roomid, e)

    def handle_group_created(self, roomid: str, *, force: bool = False) -> None:
        detail = self.external.get_group_chat(roomid) or {}
        name = str(detail.get("name") or "")
        owner = str(detail.get("owner") or self.external.default_owner_userid or "")

        existing = self.store.get_group(roomid)
        if (
            not force
            and existing
            and existing.get("status") == GROUP_STATUS_WELCOMED
            and existing.get("welcomed_at")
        ):
            logger.info("群 %s 已欢迎过，跳过（加 force=True 可重发）", roomid)
            return

        self._maybe_ensure_form_token(roomid)
        self.store.upsert_group(roomid, name=name, owner_userid=owner, status=GROUP_STATUS_INIT)

        try:
            welcome_bundle = build_welcome_message(
                settings.wework_welcome_advisor_phone, channel="group",
            )
            self.external.send_group_text(roomid, welcome_bundle, sender_userid=owner or None)
        except Exception as e:
            logger.exception("群 %s 欢迎语发送失败: %s", roomid, e)
            return

        welcomed_at = datetime.now(timezone.utc).isoformat()
        self.store.upsert_group(
            roomid, name=name, owner_userid=owner,
            status=GROUP_STATUS_WELCOMED, welcomed_at=welcomed_at,
        )
        logger.info("群 %s 欢迎语已发送 → WELCOMED", roomid)

        if settings.wework_welcome_auto_checklist:
            try:
                self._send_checklist(roomid)
                logger.info("群 %s 已自动发送注册资料清单", roomid)
            except Exception as e:
                logger.warning("群 %s 自动发清单失败: %s", roomid, e)

    def _save_agent_run(self, roomid: str, combined: str, result) -> None:
        from config.settings import settings
        import json

        if not settings.agent_log_runs or not roomid:
            return
        trace_data = [
            {"step": t.step, "attempt": t.attempt, "data": t.data}
            for t in result.trace
        ]
        self.store.insert_agent_run(
            run_id=result.run_id,
            roomid=roomid,
            question=combined,
            final_answer=result.answer,
            retrieval_score=result.retrieval_score,
            answer_score=result.answer_score,
            confidence=result.confidence,
            action=result.action.value,
            retries=result.retries,
            trace_json=json.dumps(trace_data, ensure_ascii=False),
        )

    def _build_agent_context(self, combined: str, roomid: str):
        from config.settings import settings
        from src.agent.models import AgentContext

        history: list[str] = []
        group_meta: dict[str, str] = {}
        if roomid:
            limit = settings.agent_context_history_limit
            history = self.store.get_recent_messages(roomid, limit=limit)
            group = self.store.get_group(roomid) or {}
            if group.get("name"):
                group_meta["name"] = str(group["name"])
            if group.get("status"):
                group_meta["status"] = str(group["status"])
            if is_kf_session(roomid):
                group_meta["channel"] = "kf"
            elif roomid.startswith("wr"):
                group_meta["channel"] = "group"
        return AgentContext(
            question=combined,
            roomid=roomid,
            scope="hk",
            history=history,
            group_meta=group_meta,
        )

    def _generate_ai_answer(self, combined: str, *, roomid: str = "") -> tuple[str, object | None]:
        from config.settings import settings
        from src.agent.models import AgentAction
        from src.agent.orchestrator import TaskOrchestrator

        if not settings.rag_enabled:
            if settings.agent_silent_on_no_answer:
                return "", None
            return self.llm.answer_material_question(combined), None

        try:
            orchestrator = TaskOrchestrator()
            ctx = self._build_agent_context(combined, roomid)
            result = orchestrator.run_qa(ctx)
            self._save_agent_run(roomid, combined, result)
            if result.action == AgentAction.HUMAN and roomid:
                try:
                    self._transfer_human(roomid, "")
                except Exception:
                    pass
            return result.answer, result
        except Exception as e:
            logger.warning("QA Agent 失败，静默跳过: %s", e)
            return "", None

    def handle_kf_first_contact(
        self,
        external_userid: str,
        *,
        open_kfid: str = "",
    ) -> None:
        """微信客服首次私聊：欢迎语 + 可选资料清单"""
        kfid = open_kfid or settings.wework_kf_default_open_kfid
        roomid = build_kf_roomid(kfid, external_userid)
        existing = self.store.get_group(roomid)
        if existing and existing.get("welcomed_at"):
            return

        acc = settings.get_kf_account(kfid)
        display_name = acc.name if acc and acc.name else f"客服:{external_userid[:12]}"
        self._maybe_ensure_form_token(roomid)
        self.store.upsert_group(
            roomid,
            name=display_name,
            status=GROUP_STATUS_INIT,
            open_kfid=kfid,
        )
        try:
            welcome = build_welcome_message(
                settings.wework_welcome_advisor_phone, channel="kf",
            )
            self.external.send_kf_text(external_userid, welcome, open_kfid=kfid)
        except Exception as e:
            logger.exception("kf 客户 %s 欢迎语发送失败: %s", external_userid, e)
            return

        welcomed_at = datetime.now(timezone.utc).isoformat()
        self.store.upsert_group(
            roomid, status=GROUP_STATUS_WELCOMED, welcomed_at=welcomed_at, open_kfid=kfid,
        )
        logger.info("kf 客户 [%s] %s 欢迎语已发送 → WELCOMED", kfid, external_userid)

        if settings.wework_welcome_auto_checklist:
            try:
                self._send_checklist(roomid, to_external_userid=external_userid)
                logger.info("kf 客户 %s 已自动发送注册资料清单", external_userid)
            except Exception as e:
                logger.warning("kf 客户 %s 自动发清单失败: %s", external_userid, e)

    def handle_kf_incoming_text(
        self,
        external_userid: str,
        msgid: str,
        content: str,
        *,
        open_kfid: str = "",
    ) -> None:
        """微信客服私聊入站 → 统一状态机"""
        kfid = open_kfid or settings.wework_kf_default_open_kfid
        self.handle_incoming_text(
            roomid=build_kf_roomid(kfid, external_userid),
            msgid=msgid,
            from_id=external_userid,
            content=content,
        )

    def handle_incoming_text(
        self, roomid: str, msgid: str, from_id: str, content: str,
    ) -> None:
        text = content.strip()
        if not text:
            self.store.mark_message_processed(msgid)
            return

        if is_kf_session(roomid):
            parsed = parse_kf_roomid(roomid)
            if parsed:
                open_kfid, wm = parsed
                self.handle_kf_first_contact(wm, open_kfid=open_kfid)
            else:
                self.handle_kf_first_contact(roomid.removeprefix("kf:"))
        elif from_id.startswith("wm") and roomid.startswith("wr"):
            self.store.link_customer(from_id, roomid)

        if not self.store.get_group(roomid):
            self.store.upsert_group(roomid, status=GROUP_STATUS_WELCOMED)
            self._maybe_ensure_form_token(roomid)

        status = (self.store.get_group(roomid) or {}).get("status") or GROUP_STATUS_WELCOMED
        wm_target = self._resolve_external_userid(roomid, from_id)

        if status == GROUP_STATUS_HUMAN:
            self.store.mark_message_processed(msgid)
            return

        if text in ("/资料", "/docs"):
            self._send_checklist(roomid, to_external_userid=wm_target)
            self.store.mark_message_processed(msgid)
            return

        if text in ("/填表", "/form"):
            self._handle_fill_form_command(
                roomid,
                to_external_userid=wm_target,
            )
            self.store.mark_message_processed(msgid)
            return

        if text in ("/模板", "/template"):
            self._send_paste_template(
                roomid,
                to_external_userid=wm_target,
            )
            self.store.mark_message_processed(msgid)
            return

        if text in ("/进度", "/progress"):
            self._send_progress(roomid, to_external_userid=wm_target)
            self.store.mark_message_processed(msgid)
            return

        if text in ("转人工", "/转人工", "/human"):
            self._transfer_human(roomid, from_id)
            self.store.mark_message_processed(msgid)
            return

        if text in ("确认", "确认无误", "/确认") or re.match(r"^确认[。!！]?$", text):
            self._handle_confirm(roomid, from_id)
            self.store.mark_message_processed(msgid)
            return

        # 键=值 表单粘贴
        if "=" in text or "：" in text or ":" in text:
            result = parse_registration_form(text)
            if result.fields:
                for row in fields_to_material_rows(result.fields, source="chat_form"):
                    fk = row.pop("field_key")
                    self.store.upsert_material(roomid, fk, **row)
                self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
                self._safe_send(
                    roomid,
                    f"【材料更新】\n{format_progress_text(self.store.get_materials(roomid))}",
                    to_external_userid=wm_target,
                )
                if is_ready_for_confirm(self.store.get_materials(roomid)):
                    self._send_review_summary(roomid, to_external_userid=wm_target)
                self.store.mark_message_processed(msgid)
                return

        if status in (GROUP_STATUS_CONFIRMED, GROUP_STATUS_HANDOFF):
            self.store.mark_message_processed(msgid)
            return

        with self._lock:
            batch = self._pending.get(roomid)
            if batch is None:
                batch = PendingBatch(roomid=roomid, from_id=from_id)
                self._pending[roomid] = batch
            batch.msgids.append(msgid)
            batch.texts.append(text)
            if not batch.from_id:
                batch.from_id = from_id
            if batch.timer:
                batch.timer.cancel()
            batch.timer = threading.Timer(DEBOUNCE_SECONDS, self._flush_batch, args=(roomid,))
            batch.timer.daemon = True
            batch.timer.start()

    def handle_file_received(
        self,
        roomid: str,
        msgid: str,
        field_key: str,
        filename: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        from src.wework.material_handler import MaterialHandler

        handler = MaterialHandler(store=self.store)
        msg = handler.notify_classification(field_key, filename)
        self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
        wm = to_external_userid or self._resolve_external_userid(roomid, "")
        self._safe_send(
            roomid,
            f"{msg}\n{format_progress_text(self.store.get_materials(roomid))}",
            to_external_userid=wm,
        )
        if is_ready_for_confirm(self.store.get_materials(roomid)):
            self._send_review_summary(roomid, to_external_userid=wm)
        self.store.mark_message_processed(msgid)

    def _flush_batch(self, roomid: str) -> None:
        with self._lock:
            batch = self._pending.pop(roomid, None)
        if not batch or not batch.texts:
            return
        combined = "\n".join(batch.texts)
        trigger_msgid = batch.msgids[-1] if batch.msgids else ""
        wm = batch.from_id if batch.from_id.startswith("wm") else None
        try:
            answer, qa_result = self._generate_ai_answer(combined, roomid=roomid)
            from src.agent.models import AgentAction

            if qa_result and qa_result.action == AgentAction.SILENT:
                logger.info("会话 %s 问题静默跳过，待人工: %s", roomid, combined[:80])
                return
            if not (answer or "").strip():
                logger.info("会话 %s 无回答，跳过发送", roomid)
                return
            reply = f"【AI 助手】{answer}"
            self._safe_send(roomid, reply, to_external_userid=wm)
            self.store.insert_ai_reply(
                roomid, trigger_msgid, reply,
                model=settings.openai_model,
                run_id=qa_result.run_id if qa_result else "",
                confidence=qa_result.confidence if qa_result else 0.0,
            )
            self.store.set_group_status(roomid, GROUP_STATUS_QA)
        except Exception as e:
            logger.exception("会话 %s AI 回复失败: %s", roomid, e)
        finally:
            for mid in batch.msgids:
                self.store.mark_message_processed(mid)

    def _send_checklist(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        wm = to_external_userid or self._resolve_external_userid(roomid, "")
        owner = self._owner(roomid)
        self.external.send_material_checklist(
            roomid, sender_userid=owner, to_external_userid=wm,
        )
        paste_target = "本会话" if is_kf_session(roomid) else "本群"
        hint = (
            f"清单见上。在线填表：{self._form_url(roomid)}"
            if settings.collect_form_enabled
            else f"清单见上。发送 /填表 获取填写模板，粘贴到{paste_target}提交；证件请直接上传。"
        )
        self._safe_send(roomid, hint, to_external_userid=wm)

    def _send_progress(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        progress = format_progress_text(self.store.get_materials(roomid))
        linked_hint = ""
        if is_kf_session(roomid):
            wm = roomid.removeprefix("kf:")
            groups = self.store.get_linked_groups(wm)
            if groups:
                ids = ", ".join(g["roomid"][:16] for g in groups[:3])
                linked_hint = f"\n\n（您在群内也有会话：{ids}…，材料各自独立）"
        self._safe_send(
            roomid,
            progress + linked_hint,
            to_external_userid=to_external_userid,
        )

    def _send_review_summary(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        materials = self.store.get_materials(roomid)
        company_data = aggregate_company_data(materials)
        summary = self.llm.confirm_materials_summary(company_data)
        self.store.set_group_status(roomid, GROUP_STATUS_REVIEW)
        self._safe_send(
            roomid,
            f"【材料确认】\n{summary}\n\n请核对后回复「确认」启动打包与 ICRIS 注册（dry_run）。",
            to_external_userid=to_external_userid,
        )

    def _handle_confirm(self, roomid: str, from_id: str) -> None:
        wm = self._resolve_external_userid(roomid, from_id)
        materials = self.store.get_materials(roomid)
        if not is_ready_for_confirm(materials):
            self._safe_send(
                roomid,
                f"必填材料尚未齐全。\n{format_progress_text(materials)}",
                to_external_userid=wm,
            )
            return
        self.store.set_group_status(roomid, GROUP_STATUS_CONFIRMED)
        self._safe_send(
            roomid,
            "已收到确认，正在打包材料并启动 ICRIS 注册…",
            to_external_userid=wm,
        )
        threading.Thread(
            target=self._run_handoff,
            args=(roomid, from_id),
            daemon=True,
            name=f"handoff-{roomid[:8]}",
        ).start()

    def _run_handoff(self, roomid: str, from_id: str) -> None:
        try:
            self.ext_workflow.run_after_confirm(roomid, from_id)
        except Exception as e:
            logger.exception("handoff 失败 roomid=%s", roomid)
            self._safe_send(
                roomid,
                f"后续流程异常: {e}",
                to_external_userid=self._resolve_external_userid(roomid, from_id),
            )

    def _transfer_human(self, roomid: str, from_id: str) -> None:
        owner = str(self._owner(roomid) or "")
        wm = self._resolve_external_userid(roomid, from_id)
        self.store.set_group_status(roomid, GROUP_STATUS_HUMAN)
        self._safe_send(roomid, "已为您转接人工专员，请稍候。", to_external_userid=wm)
        channel = "客服私聊" if is_kf_session(roomid) else "外部群"
        if owner:
            self.external.send_text_to_user(
                owner,
                f"【{channel}转人工】会话 {roomid}\n客户 {from_id or wm or ''}",
            )

    def ensure_group_registered(self, roomid: str, owner_userid: str = "") -> None:
        if not self.store.get_group(roomid):
            self.store.upsert_group(
                roomid,
                owner_userid=owner_userid or self.external.default_owner_userid,
                status=GROUP_STATUS_WELCOMED,
            )
            self._maybe_ensure_form_token(roomid)
