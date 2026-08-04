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
from src.materials.form_parser import fields_to_material_rows
from src.storage.db import ExternalGroupStore
from src.wework.external_client import WeWorkExternalClient
from src.wework.external_workflow import ExternalGroupWorkflow
from src.wework.kf_session import (
    build_kf_roomid,
    is_kf_session,
    parse_kf_roomid,
)
from src.wework.message_graph import route_incoming_text
from src.wework.intent_router import (
    INTENT_ASK_PROGRESS,
    INTENT_SUBMIT_MATERIAL,
    INTENT_UNCLEAR_MATERIAL,
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
SEND_FAIL_FALLBACK = "系统繁忙，消息可能未送达，请稍后再试或回复「转人工」。"
QA_ERROR_FALLBACK = "系统繁忙，请稍后再试或回复「转人工」。"
# 企微客服文本约 2048 字节上限，预留余量
WEWORK_TEXT_MAX_BYTES = 2000


def _split_utf8_chunks(text: str, max_bytes: int = WEWORK_TEXT_MAX_BYTES) -> list[str]:
    """按 UTF-8 字节切分长文本，尽量在换行/句读处断开。"""
    raw = text or ""
    if not raw:
        return [""]
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [raw]
    chunks: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + max_bytes, len(encoded))
        # 避免截断多字节字符
        while end > start:
            try:
                piece = encoded[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            end = min(start + max_bytes, len(encoded))
            piece = encoded[start:end].decode("utf-8", errors="ignore")
        if end < len(encoded) and len(piece) > 40:
            for sep in ("\n", "。", "！", "？", ";", "；", ",", "，", " "):
                idx = piece.rfind(sep)
                if idx >= len(piece) // 3:
                    piece = piece[: idx + 1]
                    end = start + len(piece.encode("utf-8"))
                    break
        if piece:
            chunks.append(piece)
        if end <= start:
            break
        start = end
    return chunks or [raw[:200]]


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
    _handoff_inflight: set[str] = field(default_factory=set)

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
        customer_fallback: bool = True,
    ) -> bool:
        """发送文本（自动切分）；失败时可选向客户发一句兜底。返回是否全部成功。"""
        chunks = _split_utf8_chunks(content)
        ok = True
        try:
            for chunk in chunks:
                data = self.external.send_session_text(
                    roomid,
                    chunk,
                    sender_userid=self._owner(roomid),
                    to_external_userid=to_external_userid,
                )
                if isinstance(data, dict) and int(data.get("errcode", 0) or 0) != 0:
                    raise RuntimeError(f"send errcode={data.get('errcode')} {data.get('errmsg')}")
        except Exception as e:
            ok = False
            logger.error(
                "会话 %s 发消息失败: %s | content=%s",
                roomid,
                e,
                (content or "")[:80],
            )
            if (
                customer_fallback
                and (content or "").strip() != SEND_FAIL_FALLBACK
            ):
                try:
                    self.external.send_session_text(
                        roomid,
                        SEND_FAIL_FALLBACK,
                        sender_userid=self._owner(roomid),
                        to_external_userid=to_external_userid,
                    )
                except Exception as e2:
                    logger.error("会话 %s 兜底发送也失败: %s", roomid, e2)
        return ok

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

    def _materials_summary(self, roomid: str) -> str:
        """短摘要注入 QA 上下文（证件类型/号码/缺项）"""
        from src.materials.checklist import progress_summary

        materials = self.store.get_materials(roomid)
        if not materials:
            return ""
        parts: list[str] = []
        id_type = str((materials.get("id_type") or {}).get("field_value") or "").strip()
        id_number = str((materials.get("id_number") or {}).get("field_value") or "").strip()
        if id_type:
            parts.append(f"证件类型={id_type}")
        if id_number:
            parts.append("号码已填")
        elif id_type:
            parts.append("号码未识别")
        for key, label in (
            ("id_card_front", "身份证明正面"),
            ("passport", "护照"),
            ("address_proof", "地址证明"),
        ):
            row = materials.get(key) or {}
            if row.get("file_path") or row.get("field_value"):
                parts.append(f"{label}已上传")
        prog = progress_summary(materials)
        missing = prog.get("missing_labels") or []
        if missing:
            parts.append("缺: " + "、".join(missing[:6]))
            if len(missing) > 6:
                parts.append(f"等共{len(missing)}项")
        summary = "; ".join(parts)
        return summary[:400]

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
            mat_summary = self._materials_summary(roomid)
            if mat_summary:
                group_meta["materials_summary"] = mat_summary
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

        if (
            text in ("确认", "确认无误", "/确认", "开始注册", "/开始注册")
            or re.match(r"^确认[。!！]?$", text)
            or re.match(r"^开始注册[。!！]?$", text)
        ):
            self._handle_confirm(roomid, from_id)
            self.store.mark_message_processed(msgid)
            return

        # LangGraph 意图分流：材料/进度不进 RAG；业务问答才防抖进 QA
        route = route_incoming_text(text, status=status)
        logger.info(
            "意图分流 room=%s status=%s action=%s source=%s reply=%s fields=%s",
            roomid,
            status,
            route.action,
            route.source,
            route.reply_kind,
            list(route.fields.keys()),
        )

        if route.action == INTENT_ASK_PROGRESS:
            self._send_progress(roomid, to_external_userid=wm_target)
            self.store.mark_message_processed(msgid)
            return

        if route.action in (INTENT_SUBMIT_MATERIAL, INTENT_UNCLEAR_MATERIAL):
            fields = dict(route.fields or {})
            if fields:
                for row in fields_to_material_rows(fields, source="chat_form"):
                    fk = row.pop("field_key")
                    self.store.upsert_material(roomid, fk, **row)
                if "company_name_cn" in fields or "company_name_en" in fields:
                    self._align_materials_folder(roomid)
                self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
                self._safe_send(
                    roomid,
                    f"【材料更新】\n{format_progress_text(self.store.get_materials(roomid))}",
                    to_external_userid=wm_target,
                )
                if is_ready_for_confirm(self.store.get_materials(roomid)):
                    self._send_review_summary(roomid, to_external_userid=wm_target)
            else:
                # 明确是交资料但解析失败：缺项 + /填表，不走 RAG
                progress = format_progress_text(self.store.get_materials(roomid))
                self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
                self._safe_send(
                    roomid,
                    f"{progress}\n\n"
                    "未识别到可入库字段。请按「键=值」发送，或发送 /填表 / /模板 获取填写指引。",
                    to_external_userid=wm_target,
                )
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

    def _align_materials_folder(self, roomid: str) -> None:
        """公司名入库后，将材料目录从 roomid 名对齐到公司中文/英文名"""
        from src.storage.file_store import ensure_company_folder

        materials = self.store.get_materials(roomid)
        cn = str((materials.get("company_name_cn") or {}).get("field_value") or "").strip()
        en = str((materials.get("company_name_en") or {}).get("field_value") or "").strip()
        if not cn and not en:
            return
        try:
            _new_dir, old_name, new_name = ensure_company_folder(roomid, cn, en)
            if old_name and new_name and old_name != new_name:
                n = self.store.rewire_material_file_paths(roomid, old_name, new_name)
                logger.info(
                    "会话 %s 材料目录对齐 %s → %s，更新路径 %d 条",
                    roomid,
                    old_name,
                    new_name,
                    n,
                )
            if cn or en:
                self.store.upsert_group(roomid, company_name=cn or en)
        except Exception:
            logger.warning("会话 %s 材料目录对齐失败", roomid, exc_info=True)

    def handle_file_received(
        self,
        roomid: str,
        msgid: str,
        field_key: str,
        filename: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        from src.wework.material_handler import REJECTED_NON_ID, MaterialHandler

        handler = MaterialHandler(store=self.store)
        msg = handler.notify_classification(field_key, filename, roomid=roomid) or (
            f"已收到文件「{filename}」。"
        )
        wm = to_external_userid or self._resolve_external_userid(roomid, "")

        if field_key == REJECTED_NON_ID:
            # 非身份证明：不入库、不标为已收证件，仅提示
            reply = msg
            self._safe_send(roomid, reply, to_external_userid=wm)
            try:
                self.store.insert_ai_reply(roomid, msgid, reply, model="material_file")
            except Exception:
                logger.debug("写入文件回复上下文失败 room=%s", roomid, exc_info=True)
            self.store.mark_message_processed(msgid)
            return

        self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
        reply = f"{msg}\n{format_progress_text(self.store.get_materials(roomid))}"
        self._safe_send(roomid, reply, to_external_userid=wm)
        try:
            self.store.insert_ai_reply(roomid, msgid, reply, model="material_file")
        except Exception:
            logger.debug("写入文件回复上下文失败 room=%s", roomid, exc_info=True)
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
            if settings.wework_thinking_ack_enabled:
                ack = (settings.wework_thinking_ack_text or "正在为您查询，请稍候…").strip()
                if ack:
                    self._safe_send(
                        roomid,
                        ack,
                        to_external_userid=wm,
                        customer_fallback=False,
                    )

            answer, qa_result = self._generate_ai_answer(combined, roomid=roomid)
            from src.agent.models import AgentAction

            if qa_result and qa_result.action == AgentAction.SILENT:
                logger.info("会话 %s 问题静默跳过，待人工: %s", roomid, combined[:80])
                return
            if qa_result and qa_result.action in (
                AgentAction.ABSTAIN,
                AgentAction.HUMAN,
            ):
                answer = (answer or "").strip() or (
                    settings.agent_abstain_message
                    if qa_result.action == AgentAction.ABSTAIN
                    else "已为您转接人工服务，专属服务老师将尽快回复您。"
                )
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
            self._safe_send(
                roomid,
                QA_ERROR_FALLBACK,
                to_external_userid=wm,
                customer_fallback=False,
            )
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
            f"【材料确认】资料已齐全，可以开始注册。\n{summary}\n\n"
            f"请核对无误后回复「确认」或「开始注册」。",
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

        with self._lock:
            status = (self.store.get_group(roomid) or {}).get("status") or ""
            if roomid in self._handoff_inflight or status == GROUP_STATUS_HANDOFF:
                self._safe_send(
                    roomid,
                    "注册流程已在处理中或已完成交接，请稍候；勿重复发送「确认」。",
                    to_external_userid=wm,
                    customer_fallback=False,
                )
                return
            self._handoff_inflight.add(roomid)

        self.store.set_group_status(roomid, GROUP_STATUS_CONFIRMED)
        submit_note = (
            "将尝试自动提交 ICRIS。"
            if (not settings.dry_run and settings.icris_allow_submit)
            else "当前为填表预览（不自动提交），完成后由专员复核。"
        )
        self._safe_send(
            roomid,
            f"已收到，开始为您办理注册：正在打包材料并启动后续流程…\n{submit_note}",
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
        finally:
            with self._lock:
                self._handoff_inflight.discard(roomid)

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
