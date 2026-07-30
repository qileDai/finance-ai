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


def build_welcome_message(phone: str = "") -> str:
    phone_display = phone.strip() if phone and phone.strip() else "待补充"
    return (
        f"大家好 / 您好，我是赢态财务集团 - 香港业务专属服务老师：{WELCOME_ADVISOR_NAME}老师。"
        f"很荣幸能为您服务，接下来由我全程一对一跟进贵司的香港公司注册、开户及年审等香港公司服务事宜。"
        f"为确保服务高效顺畅，我会在每个关键节点主动向您汇报进度。"
        f"我的电话：【{phone_display}】，有任何疑问随时找我哈"
    )


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

    def _send_paste_template(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        template = self._load_paste_template()
        msg = (
            "【填写模板】请按下方格式填写，完成后整段粘贴到本群发送：\n\n"
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
            self.external.send_group_text(
                roomid,
                content,
                sender_userid=self._owner(roomid),
                to_external_userid=to_external_userid,
            )
        except Exception as e:
            logger.warning("群 %s 发消息失败（材料已入库）: %s", roomid, e)

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
            welcome_bundle = build_welcome_message(settings.wework_welcome_advisor_phone)
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

    def _generate_ai_answer(self, combined: str) -> str:
        context = ""
        if settings.rag_enabled:
            try:
                from src.rag.hybrid_retriever import HybridRetriever
                from src.rag.prompt import format_hits_for_prompt

                hits = HybridRetriever().retrieve(
                    combined, top_k=settings.rag_top_k, scope="hk",
                )
                context = format_hits_for_prompt(hits)
            except Exception as e:
                logger.warning("RAG 检索失败，回退纯 LLM: %s", e)
        return self.llm.answer_material_question(combined, context=context)

    def handle_kf_incoming_text(
        self, external_userid: str, msgid: str, content: str,
    ) -> None:
        """微信客服私聊入站（debounce 后 RAG+LLM，经 kf/send_msg 回复）"""
        text = content.strip()
        if not text:
            self.store.mark_message_processed(msgid)
            return

        key = f"kf:{external_userid}"
        with self._lock:
            batch = self._pending.get(key)
            if batch is None:
                batch = PendingBatch(roomid=key, from_id=external_userid)
                self._pending[key] = batch
            batch.msgids.append(msgid)
            batch.texts.append(text)
            if batch.timer:
                batch.timer.cancel()
            batch.timer = threading.Timer(
                DEBOUNCE_SECONDS, self._flush_kf_batch, args=(external_userid,),
            )
            batch.timer.daemon = True
            batch.timer.start()

    def _flush_kf_batch(self, external_userid: str) -> None:
        key = f"kf:{external_userid}"
        with self._lock:
            batch = self._pending.pop(key, None)
        if not batch or not batch.texts:
            return
        combined = "\n".join(batch.texts)
        trigger_msgid = batch.msgids[-1] if batch.msgids else ""
        try:
            answer = self._generate_ai_answer(combined)
            reply = f"【AI 助手】{answer}"
            self.external.send_kf_text(external_userid, reply)
            self.store.insert_ai_reply(key, trigger_msgid, reply, model=settings.openai_model)
        except Exception as e:
            logger.exception("kf 客户 %s AI 回复失败: %s", external_userid, e)
        finally:
            for mid in batch.msgids:
                self.store.mark_message_processed(mid)

    def handle_incoming_text(
        self, roomid: str, msgid: str, from_id: str, content: str,
    ) -> None:
        text = content.strip()
        if not text:
            self.store.mark_message_processed(msgid)
            return

        if not self.store.get_group(roomid):
            self.store.upsert_group(roomid, status=GROUP_STATUS_WELCOMED)
            self._maybe_ensure_form_token(roomid)

        status = (self.store.get_group(roomid) or {}).get("status") or GROUP_STATUS_WELCOMED
        owner = self._owner(roomid)

        if status == GROUP_STATUS_HUMAN:
            self.store.mark_message_processed(msgid)
            return

        if text in ("/资料", "/docs"):
            self._send_checklist(roomid)
            self.store.mark_message_processed(msgid)
            return

        if text in ("/填表", "/form"):
            self._handle_fill_form_command(
                roomid,
                to_external_userid=from_id if from_id.startswith("wm") else None,
            )
            self.store.mark_message_processed(msgid)
            return

        if text in ("/模板", "/template"):
            self._send_paste_template(
                roomid,
                to_external_userid=from_id if from_id.startswith("wm") else None,
            )
            self.store.mark_message_processed(msgid)
            return

        if text in ("/进度", "/progress"):
            self._send_progress(roomid)
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
                )
                if is_ready_for_confirm(self.store.get_materials(roomid)):
                    self._send_review_summary(roomid)
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

    def handle_file_received(self, roomid: str, msgid: str, field_key: str, filename: str) -> None:
        owner = self._owner(roomid)
        from src.wework.material_handler import MaterialHandler

        handler = MaterialHandler(store=self.store)
        msg = handler.notify_classification(field_key, filename)
        self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
        self._safe_send(
            roomid,
            f"{msg}\n{format_progress_text(self.store.get_materials(roomid))}",
        )
        if is_ready_for_confirm(self.store.get_materials(roomid)):
            self._send_review_summary(roomid)
        self.store.mark_message_processed(msgid)

    def _flush_batch(self, roomid: str) -> None:
        with self._lock:
            batch = self._pending.pop(roomid, None)
        if not batch or not batch.texts:
            return
        combined = "\n".join(batch.texts)
        trigger_msgid = batch.msgids[-1] if batch.msgids else ""
        owner = self._owner(roomid)
        try:
            answer = self._generate_ai_answer(combined)
            reply = f"【AI 助手】{answer}"
            self.external.send_group_text(
                roomid,
                reply,
                sender_userid=owner,
                to_external_userid=batch.from_id if batch.from_id.startswith("wm") else None,
            )
            self.store.insert_ai_reply(roomid, trigger_msgid, reply, model=settings.openai_model)
            self.store.set_group_status(roomid, GROUP_STATUS_QA)
        except Exception as e:
            logger.exception("群 %s AI 回复失败: %s", roomid, e)
        finally:
            for mid in batch.msgids:
                self.store.mark_message_processed(mid)

    def _send_checklist(self, roomid: str) -> None:
        owner = self._owner(roomid)
        self.external.send_material_checklist(roomid, sender_userid=owner)
        hint = (
            f"清单见上。在线填表：{self._form_url(roomid)}"
            if settings.collect_form_enabled
            else "清单见上。发送 /填表 获取填写模板，粘贴到本群提交；证件请直接上传。"
        )
        self.external.send_group_text(roomid, hint, sender_userid=owner)

    def _send_progress(self, roomid: str) -> None:
        self._safe_send(roomid, format_progress_text(self.store.get_materials(roomid)))

    def _send_review_summary(self, roomid: str) -> None:
        owner = self._owner(roomid)
        materials = self.store.get_materials(roomid)
        company_data = aggregate_company_data(materials)
        summary = self.llm.confirm_materials_summary(company_data)
        self.store.set_group_status(roomid, GROUP_STATUS_REVIEW)
        self.external.send_group_text(
            roomid,
            f"【材料确认】\n{summary}\n\n请核对后回复「确认」启动打包与 ICRIS 注册（dry_run）。",
            sender_userid=owner,
        )

    def _handle_confirm(self, roomid: str, from_id: str) -> None:
        owner = self._owner(roomid)
        materials = self.store.get_materials(roomid)
        if not is_ready_for_confirm(materials):
            self.external.send_group_text(
                roomid,
                f"必填材料尚未齐全。\n{format_progress_text(materials)}",
                sender_userid=owner,
            )
            return
        self.store.set_group_status(roomid, GROUP_STATUS_CONFIRMED)
        self.external.send_group_text(
            roomid, "已收到确认，正在打包材料并启动 ICRIS 注册…", sender_userid=owner,
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
            self.external.send_group_text(
                roomid, f"后续流程异常: {e}", sender_userid=self._owner(roomid),
            )

    def _transfer_human(self, roomid: str, from_id: str) -> None:
        owner = str(self._owner(roomid) or "")
        self.store.set_group_status(roomid, GROUP_STATUS_HUMAN)
        self.external.send_group_text(
            roomid, "已为您转接人工专员，请稍候。", sender_userid=owner or None,
        )
        if owner:
            self.external.send_text_to_user(
                owner,
                f"【外部群转人工】群 {roomid}\n客户 {from_id}",
            )

    def ensure_group_registered(self, roomid: str, owner_userid: str = "") -> None:
        if not self.store.get_group(roomid):
            self.store.upsert_group(
                roomid,
                owner_userid=owner_userid or self.external.default_owner_userid,
                status=GROUP_STATUS_WELCOMED,
            )
            self._maybe_ensure_form_token(roomid)
