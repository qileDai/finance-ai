"""企业微信外部群会话状态机（Phase 1–3）"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import PROJECT_ROOT, settings
from src.llm.openai_client import LLMClient
from src.materials.aggregator import aggregate_company_data, is_ready_for_confirm
from src.materials.checklist import (
    format_case_status_text,
    format_knowledge_checklist_text,
    format_progress_text,
    prioritized_missing,
    progress_summary,
    validate_cross_fields,
)
from src.materials.form_parser import fields_to_material_rows
from src.storage.db import ExternalGroupStore
from src.wework.external_client import WeWorkExternalClient
from src.wework.external_workflow import ExternalGroupWorkflow
from src.wework.intent_planner import (
    STEP_ENQUEUE_QA,
    STEP_QUEUED_TIP,
    STEP_REPLY_PROGRESS,
    STEP_SEND_GREETING,
    STEP_SEND_UNCLEAR,
    STEP_UPSERT_MATERIALS,
    ActionPlan,
)
from src.wework.kf_session import (
    build_kf_roomid,
    is_kf_session,
    parse_kf_roomid,
)
from src.wework.message_graph import MessageRouteResult, route_incoming_text
from src.wework.intent_router import (
    REPLY_FULL_PROGRESS,
    REPLY_KNOWLEDGE_CHECKLIST,
    _normalize_social,
    batch_has_mixed_session_and_biz,
    is_knowledge_checklist_query,
    is_short_ack,
    looks_like_session_state_query,
    resolve_reply_mode,
)

AGENT_MODE_NORMAL = "normal"
AGENT_MODE_SHADOW = "shadow"
AGENT_MODE_DISABLED = "disabled"
DISABLED_STATIC_REPLY = (
    "您好，智能助手暂不可用。专员将尽快回复您；"
    "紧急请回复「转人工」，或拨打欢迎语中的服务电话。"
)

logger = logging.getLogger(__name__)

GROUP_STATUS_INIT = "INIT"
GROUP_STATUS_WELCOMED = "WELCOMED"
GROUP_STATUS_QA = "QA"
GROUP_STATUS_COLLECTING = "COLLECTING"
GROUP_STATUS_REVIEW = "REVIEW"
GROUP_STATUS_CONFIRMED = "CONFIRMED"
GROUP_STATUS_QUEUED = "QUEUED"
GROUP_STATUS_HANDOFF = "HANDOFF"
GROUP_STATUS_FAILED = "FAILED"
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

def _qa_debounce_seconds(text: str) -> float:
    """普通合并等待；明确问句更快启动。"""
    base = float(getattr(settings, "wework_qa_debounce_seconds", 1.0) or 1.0)
    fast = float(getattr(settings, "wework_qa_debounce_fast_seconds", 0.4) or 0.4)
    base = max(0.0, base)
    fast = max(0.0, min(fast, base if base > 0 else fast))
    t = (text or "").strip()
    if not t:
        return base
    if "?" in t or "？" in t:
        return fast
    from src.wework.intent_router import _looks_like_qa_question

    if _looks_like_qa_question(t):
        return fast
    return base


def _format_customer_answer(answer: str, qa_result) -> str:
    """拼接对客回复，可选短引用。"""
    text = (answer or "").strip()
    body = f"【AI 助手】{text}" if text else "【AI 助手】"
    if not qa_result or not getattr(settings, "agent_show_citations", True):
        return body
    cites = [c for c in (getattr(qa_result, "citations", None) or []) if c]
    if not cites:
        return body
    # 缩短路径展示
    labels: list[str] = []
    for c in cites[:2]:
        label = str(c).replace("\\", "/")
        if "/" in label:
            label = label.rsplit("/", 1)[-1]
        if label and label not in labels:
            labels.append(label)
    if labels:
        body = f"{body}\n依据：{' / '.join(labels)}"
    return body


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
    _qa_inflight: set[str] = field(default_factory=set)
    _human_acked: set[str] = field(default_factory=set)
    _failed_hinted: set[str] = field(default_factory=set)
    _orchestrator: object | None = field(default=None, repr=False)
    # 优化 12：主动缺失材料提醒的计数与速率限制（内存态，重启重置）
    _message_counts: dict[str, int] = field(default_factory=dict)
    _last_reminder_at: dict[str, float] = field(default_factory=dict)

    def _get_orchestrator(self):
        if self._orchestrator is None:
            from src.agent.orchestrator import TaskOrchestrator

            self._orchestrator = TaskOrchestrator(store=self.store)
        return self._orchestrator

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

    def _kf_send_identity(
        self, roomid: str, to_external_userid: str | None = None
    ) -> tuple[str, str]:
        """返回 (open_kfid, external_userid) 供额度统计。"""
        if is_kf_session(roomid):
            parsed = parse_kf_roomid(roomid)
            if parsed:
                return parsed[0], parsed[1]
            wm = to_external_userid or roomid.removeprefix("kf:")
            return settings.wework_kf_default_open_kfid or "", wm or ""
        wm = to_external_userid or ""
        return "", wm

    def _kf_quota_remaining(
        self, roomid: str, to_external_userid: str | None = None
    ) -> int | None:
        """剩余可发条数；None 表示不限制。"""
        quota = int(getattr(settings, "wework_kf_send_quota_48h", 0) or 0)
        if quota <= 0 or not is_kf_session(roomid):
            return None
        kfid, wm = self._kf_send_identity(roomid, to_external_userid)
        if not wm:
            return None
        used = self.store.count_kf_sends_48h(open_kfid=kfid, external_userid=wm)
        return max(0, quota - used)

    def _maybe_truncate_for_quota(
        self, roomid: str, content: str, *, remaining: int | None
    ) -> str:
        """KF 额度紧时压缩长答为单条。"""
        if remaining is None or remaining > 1:
            max_b = int(getattr(settings, "wework_kf_long_reply_max_bytes", 0) or 0)
            if max_b > 0 and is_kf_session(roomid):
                raw = (content or "").encode("utf-8")
                if len(raw) > max_b:
                    cut = content.encode("utf-8")[:max_b].decode("utf-8", errors="ignore")
                    for sep in ("。", "！", "？", "\n", "；"):
                        idx = cut.rfind(sep)
                        if idx > len(cut) // 2:
                            cut = cut[: idx + 1]
                            break
                    return cut.rstrip() + "\n…（篇幅较长已摘要，详情可回复「转人工」）"
            return content
        # 仅剩 1 条：强制压缩
        text = (content or "").strip()
        if len(text.encode("utf-8")) <= 600:
            return text
        cut = text.encode("utf-8")[:600].decode("utf-8", errors="ignore")
        return cut.rstrip() + "\n…客服消息额度将尽，详情请回复「转人工」。"

    def _safe_send(
        self,
        roomid: str,
        content: str,
        *,
        to_external_userid: str | None = None,
        customer_fallback: bool = True,
        count_quota: bool = True,
        enforce_quota: bool = False,
    ) -> bool:
        """发送文本（自动切分）；失败时可选向客户发一句兜底。返回是否全部成功。

        enforce_quota：仅「无客户入站的主动触达」应为 True（硬拦 5/48h）。
        客户主动发消息后的被动回复必须 False，避免寒暄/答疑被额度 tip 顶替。
        """
        import time as _time

        remaining = self._kf_quota_remaining(roomid, to_external_userid)
        if enforce_quota and remaining is not None and remaining <= 0:
            logger.warning(
                "KF 主动发送额度已用尽 room=%s，跳过: %s",
                roomid,
                (content or "")[:60],
            )
            tip = "当前会话消息额度已用尽，请稍后再试或回复等待人工跟进。"
            if (content or "").strip() != tip:
                try:
                    self.external.send_session_text(
                        roomid,
                        tip,
                        sender_userid=self._owner(roomid),
                        to_external_userid=to_external_userid,
                    )
                except Exception:
                    pass
            return False

        # 被动回复：不按剩余额度截断；主动触达仍压缩/限 chunk
        if enforce_quota:
            body = self._maybe_truncate_for_quota(
                roomid, content, remaining=remaining
            )
            chunks = _split_utf8_chunks(body)
            if remaining is not None and remaining <= 1:
                chunks = chunks[:1]
            elif remaining is not None:
                chunks = chunks[: max(1, remaining)]
        else:
            body = content
            chunks = _split_utf8_chunks(body)

        ok = True
        sent_any = False
        delay = float(getattr(settings, "wework_send_chunk_delay_seconds", 0) or 0)
        try:
            for i, chunk in enumerate(chunks):
                if i > 0 and delay > 0:
                    _time.sleep(delay)
                data = self.external.send_session_text(
                    roomid,
                    chunk,
                    sender_userid=self._owner(roomid),
                    to_external_userid=to_external_userid,
                )
                if isinstance(data, dict) and int(data.get("errcode", 0) or 0) != 0:
                    raise RuntimeError(
                        f"send errcode={data.get('errcode')} {data.get('errmsg')}"
                    )
                sent_any = True
                # 仅主动触达计入 5/48h；被动回复不记，避免虚耗额度
                if (
                    enforce_quota
                    and count_quota
                    and is_kf_session(roomid)
                ):
                    kfid, wm = self._kf_send_identity(roomid, to_external_userid)
                    if wm:
                        try:
                            self.store.record_kf_send(
                                open_kfid=kfid, external_userid=wm, roomid=roomid
                            )
                        except Exception:
                            logger.debug("记录 kf 发送额度失败", exc_info=True)
            if (
                enforce_quota
                and len(chunks) < len(_split_utf8_chunks(body))
                and sent_any
            ):
                logger.warning("会话 %s 因额度未发完全文", roomid)
        except Exception as e:
            ok = False
            logger.error(
                "会话 %s 发消息失败: %s | content=%s",
                roomid,
                e,
                (content or "")[:80],
            )
            try:
                self.store.record_send_failure(roomid, reason=str(e))
            except Exception:
                logger.debug("记录发送失败指标失败", exc_info=True)
            if (
                customer_fallback
                and (content or "").strip() != SEND_FAIL_FALLBACK
            ):
                try:
                    self.external.send_session_text(
                        roomid,
                        SEND_FAIL_FALLBACK
                        if not sent_any
                        else "后半段消息可能未送达，请回复「转人工」或稍后再试。",
                        sender_userid=self._owner(roomid),
                        to_external_userid=to_external_userid,
                    )
                except Exception as e2:
                    logger.error("会话 %s 兜底发送也失败: %s", roomid, e2)
        return ok and sent_any

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
        self.ensure_default_material_contacts(roomid)

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

    def ensure_default_material_contacts(self, roomid: str) -> None:
        """客户未提供时回填配置中的默认邮箱/香港电话。"""
        if not roomid:
            return
        email = (getattr(settings, "materials_default_contact_email", "") or "").strip()
        phone = (getattr(settings, "materials_default_contact_phone", "") or "").strip()
        materials = self.store.get_materials(roomid)
        if email and not str((materials.get("contact_email") or {}).get("field_value") or "").strip():
            self.store.upsert_material(
                roomid,
                "contact_email",
                field_value=email,
                file_path="",
                source="default",
                status="received",
            )
        if phone and not str((materials.get("contact_phone") or {}).get("field_value") or "").strip():
            self.store.upsert_material(
                roomid,
                "contact_phone",
                field_value=phone,
                file_path="",
                source="default",
                status="received",
            )

    def _save_agent_run(self, roomid: str, combined: str, result) -> None:
        from config.settings import settings
        import json

        if not settings.agent_log_runs or not roomid:
            return
        trace_data = [
            {"step": t.step, "attempt": t.attempt, "data": t.data}
            for t in result.trace
        ]
        duration_ms = 0
        for t in result.trace:
            if t.step == "total":
                try:
                    duration_ms = int((t.data or {}).get("elapsed_ms") or 0)
                except (TypeError, ValueError):
                    duration_ms = 0
                break
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
            duration_ms=duration_ms,
        )

    def _materials_summary(self, roomid: str) -> str:
        """材料快照注入 QA SessionContext（已收+待收+证件关键值）。"""
        from src.materials.checklist import format_materials_snapshot

        materials = self.store.get_materials(roomid)
        if not materials:
            return "已收集: （尚未收到）"
        return format_materials_snapshot(materials, max_chars=1200)

    def _job_status_line(self, roomid: str) -> str:
        """办理任务 / 会话状态一行摘要（若有）。"""
        group = self.store.get_group(roomid) or {}
        gst = str(group.get("status") or "")
        try:
            job = self.store.get_active_registration_job(roomid)
        except Exception:
            job = None
        if job:
            st = str(job.get("status") or "")
            jid = job.get("id")
            if st in ("pending", "running"):
                return f"注册任务 #{jid} 状态={st}（办理中）"
            return f"注册任务 #{jid} 状态={st}"
        if gst == GROUP_STATUS_QUEUED or gst == GROUP_STATUS_HANDOFF:
            return f"会话状态={gst}（办理中）；可回复「转人工」"
        if gst == GROUP_STATUS_FAILED:
            return "会话状态=FAILED；可回复「重新办理」或继续咨询业务问题"
        if gst == GROUP_STATUS_HUMAN:
            return "会话状态=HUMAN（已转人工）"
        if gst:
            return f"会话状态={gst}"
        return ""

    def _build_agent_context(self, combined: str, roomid: str):
        from config.settings import settings
        from src.agent.models import AgentContext
        from src.agent.context_rewrite import rewrite_with_context

        history: list[str] = []
        group_meta: dict[str, str] = {}
        question = combined
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
                parsed = parse_kf_roomid(roomid)
                if parsed:
                    group_meta["open_kfid"] = parsed[0]
            elif roomid.startswith("wr"):
                group_meta["channel"] = "group"
            mat_summary = self._materials_summary(roomid)
            if mat_summary:
                group_meta["materials_summary"] = mat_summary
            job_line = self._job_status_line(roomid)
            if job_line:
                group_meta["job_status"] = job_line
            # L3：指代改写后再检索/生成
            question = rewrite_with_context(
                combined,
                history=history,
                materials_summary=mat_summary,
            )
        return AgentContext(
            question=question,
            roomid=roomid,
            scope=(settings.rag_scope or "hk").strip().lower() or "hk",
            history=history,
            group_meta=group_meta,
        )

    def _generate_ai_answer(self, combined: str, *, roomid: str = "") -> tuple[str, object | None]:
        from config.settings import settings
        from src.agent.models import AgentAction, ABSTAIN_MESSAGE

        if not settings.rag_enabled:
            if settings.agent_silent_on_no_answer:
                # 生产仍禁止裸静默：给客户可见兜底
                return (
                    (settings.agent_abstain_message or "").strip() or ABSTAIN_MESSAGE,
                    None,
                )
            return self.llm.answer_material_question(combined), None

        try:
            orchestrator = self._get_orchestrator()
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
            logger.warning("QA Agent 失败，将使用兜底文案: %s", e)
            raise

    def handle_kf_first_contact(
        self,
        external_userid: str,
        *,
        open_kfid: str = "",
    ) -> None:
        """微信客服首次私聊：欢迎语 + 可选资料清单（可合并为 1 条省额度）"""
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
        self.ensure_default_material_contacts(roomid)
        try:
            welcome = build_welcome_message(
                settings.wework_welcome_advisor_phone, channel="kf",
            )
            merge = bool(getattr(settings, "wework_kf_merge_welcome_checklist", True))
            if merge and settings.wework_welcome_auto_checklist:
                paste_hint = (
                    "发送 /填表 获取填写模板并粘贴提交；证件请直接上传身份证正反面。"
                    if not settings.collect_form_enabled
                    else f"也可在线填表：{self._form_url(roomid)}"
                )
                welcome = (
                    f"{welcome}\n\n"
                    "【注册资料】请准备：公司中英文名、注册资本、经营范围、"
                    "香港注册地址（中英文）、董事兼股东姓名与身份证、住址中英文；"
                    "邮箱与香港电话可不填（系统默认）。公司秘书由我司安排。"
                    f"{paste_hint}"
                    "随时可发 /进度 查询缺项。"
                )
                # 首触达欢迎按主动触达计 5/48h；客户后续入站回复不硬拦
                self._safe_send(
                    roomid,
                    welcome,
                    to_external_userid=external_userid,
                    enforce_quota=True,
                )
            else:
                self._safe_send(
                    roomid,
                    welcome,
                    to_external_userid=external_userid,
                    enforce_quota=True,
                )
        except Exception as e:
            logger.exception("kf 客户 %s 欢迎语发送失败: %s", external_userid, e)
            return

        welcomed_at = datetime.now(timezone.utc).isoformat()
        self.store.upsert_group(
            roomid, status=GROUP_STATUS_WELCOMED, welcomed_at=welcomed_at, open_kfid=kfid,
        )
        logger.info("kf 客户 [%s] %s 欢迎语已发送 → WELCOMED", kfid, external_userid)

        merge = bool(getattr(settings, "wework_kf_merge_welcome_checklist", True))
        if settings.wework_welcome_auto_checklist and not merge:
            try:
                self._send_checklist(
                    roomid,
                    to_external_userid=external_userid,
                    enforce_quota=True,
                )
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

        # 优化 12：累计每群消息计数（主动提醒触发依据）
        self._message_counts[roomid] = self._message_counts.get(roomid, 0) + 1

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
        materials_now = self.store.get_materials(roomid)
        has_materials = bool(materials_now)
        room_channel = "kf" if is_kf_session(roomid) else "group"

        if text in ("转人工", "/转人工", "/human"):
            self._transfer_human(roomid, from_id)
            self.store.mark_message_processed(msgid)
            return

        if status == GROUP_STATUS_HUMAN and self._is_resume_bot_command(text):
            self._resume_from_human(roomid, to_external_userid=wm_target)
            self.store.mark_message_processed(msgid)
            return

        # HUMAN = 已通知人工，不封锁 AI；转接提示仅一次（见 DB human_notified_at）
        if status == GROUP_STATUS_FAILED:
            # 允许继续 QA / 重新办理；首条闲聊给一句状态提示后走分流
            cmd_like = text in (
                "重新办理",
                "/重新办理",
                "重新注册",
                "转人工",
                "/转人工",
                "/human",
                "确认",
                "确认无误",
                "/确认",
                "开始注册",
                "/开始注册",
            )
            if not cmd_like and roomid not in self._failed_hinted:
                self._failed_hinted.add(roomid)
                self._safe_send(
                    roomid,
                    "当前自动办理未完成。您可继续咨询业务问题；"
                    "若需再次排队请回复「重新办理」；也可回复「转人工」。",
                    to_external_userid=wm_target,
                    customer_fallback=False,
                )

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
            self._reply_progress_mode(
                roomid,
                mode=REPLY_FULL_PROGRESS,
                to_external_userid=wm_target,
                status=status,
            )
            self.store.mark_message_processed(msgid)
            return

        if text in ("重新办理", "/重新办理", "重新注册"):
            self._handle_confirm(roomid, from_id, force_redo=True)
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

        agent_mode = self._agent_mode()
        if agent_mode == AGENT_MODE_DISABLED:
            self._safe_send(
                roomid,
                DISABLED_STATIC_REPLY,
                to_external_userid=wm_target,
                customer_fallback=False,
            )
            self.store.mark_message_processed(msgid)
            return

        # 短确认承接（上一轮助手在要字段时）
        if is_short_ack(text):
            if self._try_short_ack_followup(roomid, text, to_external_userid=wm_target):
                self.store.mark_message_processed(msgid)
                return

        # 规则+小模型+否决 → 确定性 ActionPlan → Executor
        route = route_incoming_text(
            text, status=status, has_materials=has_materials,
        )
        plan = route.plan
        logger.info(
            "意图分流 room=%s status=%s action=%s reply_mode=%s source=%s "
            "steps=%s veto=%s has_materials=%s channel=%s mode=%s fields=%s",
            roomid,
            status,
            route.action,
            route.reply_mode,
            route.source,
            plan.step_kinds if plan else [],
            route.veto_applied,
            has_materials,
            room_channel,
            agent_mode,
            list(route.fields.keys()),
        )
        self._audit_intent_route(
            roomid,
            status=status,
            channel=room_channel,
            text=text,
            route=route,
            agent_mode=agent_mode,
        )
        if plan is None:
            self._enqueue_qa_text(roomid, msgid, from_id, text)
            return
        self._execute_plan(
            roomid,
            msgid,
            from_id,
            text,
            plan=plan,
            route=route,
            status=status,
            to_external_userid=wm_target,
            agent_mode=agent_mode,
        )

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
        from src.wework.material_handler import (
            REJECTED_NON_ID,
            REJECTED_UPLOAD,
            MaterialHandler,
        )

        handler = MaterialHandler(store=self.store)
        msg = handler.notify_classification(field_key, filename, roomid=roomid) or (
            f"已收到文件「{filename}」。"
        )
        wm = to_external_userid or self._resolve_external_userid(roomid, "")

        if field_key in (REJECTED_NON_ID, REJECTED_UPLOAD):
            # 未入库：仅提示
            reply = msg
            self._safe_send(roomid, reply, to_external_userid=wm)
            try:
                self.store.insert_ai_reply(roomid, msgid, reply, model="material_file")
            except Exception:
                logger.debug("写入文件回复上下文失败 room=%s", roomid, exc_info=True)
            self.store.mark_message_processed(msgid)
            return

        self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
        reply = (
            f"{msg}\n"
            f"{format_progress_text(self.store.get_materials(roomid), channel=self._room_channel(roomid), linked_hint=self._dual_channel_hint(roomid))}"
        )
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
            if roomid in self._qa_inflight:
                # 已有 QA 在跑：保留 pending，稍后再 flush
                batch_wait = self._pending.get(roomid)
                if batch_wait and (
                    batch_wait.timer is None or not batch_wait.timer.is_alive()
                ):
                    last = batch_wait.texts[-1] if batch_wait.texts else ""
                    delay = _qa_debounce_seconds(last)
                    batch_wait.timer = threading.Timer(
                        delay, self._flush_batch, args=(roomid,)
                    )
                    batch_wait.timer.daemon = True
                    batch_wait.timer.start()
                logger.info("会话 %s QA 进行中，延后 flush（防双答）", roomid)
                return
            batch = self._pending.pop(roomid, None)
            if batch and batch.texts:
                self._qa_inflight.add(roomid)
        if not batch or not batch.texts:
            return
        # 混句兜底：会话态直答 + 业务句进 QA
        if batch_has_mixed_session_and_biz(batch.texts):
            status = (self.store.get_group(roomid) or {}).get("status") or ""
            has_m = bool(self.store.get_materials(roomid))
            wm0 = batch.from_id if batch.from_id.startswith("wm") else None
            session_parts = [
                t
                for t in batch.texts
                if looks_like_session_state_query(t, status, has_materials=has_m)
                or is_knowledge_checklist_query(t, status, has_materials=has_m)
            ]
            biz_parts = [t for t in batch.texts if t not in session_parts]
            if session_parts:
                mode = resolve_reply_mode(
                    session_parts[0], status, has_materials=has_m,
                )
                self._reply_progress_mode(
                    roomid, mode=mode, to_external_userid=wm0, status=status,
                )
            for mid in batch.msgids:
                try:
                    self.store.mark_message_processed(mid)
                except Exception:
                    pass
            if not biz_parts:
                with self._lock:
                    self._qa_inflight.discard(roomid)
                return
            # 仅业务句继续 QA
            batch.texts = biz_parts
        combined = "\n".join(batch.texts)
        trigger_msgid = batch.msgids[-1] if batch.msgids else ""
        wm = batch.from_id if batch.from_id.startswith("wm") else None
        from src.agent.models import AgentAction, ABSTAIN_MESSAGE

        abstain = (settings.agent_abstain_message or "").strip() or ABSTAIN_MESSAGE
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

            try:
                answer, qa_result = self._generate_ai_answer(combined, roomid=roomid)
            except Exception as e:
                logger.exception("会话 %s AI 生成失败: %s", roomid, e)
                self._safe_send(
                    roomid,
                    QA_ERROR_FALLBACK,
                    to_external_userid=wm,
                    customer_fallback=False,
                )
                return

            if qa_result and qa_result.action == AgentAction.SILENT:
                logger.info(
                    "会话 %s 原静默改为兜底可见: %s", roomid, combined[:80]
                )
                answer = abstain
            if qa_result and qa_result.action in (
                AgentAction.ABSTAIN,
                AgentAction.HUMAN,
            ):
                answer = (answer or "").strip() or (
                    abstain
                    if qa_result.action == AgentAction.ABSTAIN
                    else "已为您转接人工服务，专属服务老师将尽快回复您。"
                )
            if not (answer or "").strip():
                logger.info("会话 %s 无回答，发送兜底", roomid)
                answer = abstain
            grounded = self._ground_qa_answer(answer, roomid)
            if grounded:
                logger.info("会话 %s QA 发前护栏触发", roomid)
                answer = grounded
            reply = _format_customer_answer(answer, qa_result)
            self._safe_send(roomid, reply, to_external_userid=wm)
            self.store.insert_ai_reply(
                roomid, trigger_msgid, reply,
                model=settings.openai_model,
                run_id=qa_result.run_id if qa_result else "",
                confidence=qa_result.confidence if qa_result else 0.0,
            )
            cur = (self.store.get_group(roomid) or {}).get("status") or ""
            if cur not in (
                GROUP_STATUS_QUEUED,
                GROUP_STATUS_HANDOFF,
                GROUP_STATUS_HUMAN,
                GROUP_STATUS_FAILED,
            ):
                self.store.set_group_status(roomid, GROUP_STATUS_QA)
            # 优化 12：QA 回复后主动提醒缺失材料
            self._maybe_proactive_reminder(roomid, to_external_userid=wm)
        except Exception as e:
            logger.exception("会话 %s AI 回复失败: %s", roomid, e)
            self._safe_send(
                roomid,
                QA_ERROR_FALLBACK,
                to_external_userid=wm,
                customer_fallback=False,
            )
        finally:
            with self._lock:
                self._qa_inflight.discard(roomid)
            for mid in batch.msgids:
                self.store.mark_message_processed(mid)

    def _send_greeting_reply(
        self,
        roomid: str,
        text: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        """寒暄/致谢固定话术，不进 RAG。"""
        n = _normalize_social(text)
        if any(k in n for k in ("谢谢", "感谢", "多谢")):
            msg = (
                "不客气，很高兴为您服务。"
                "如需咨询香港公司注册/开户，或发送「/资料」查看清单，随时吩咐。"
            )
        elif any(k in n for k in ("再见", "拜拜", "回见")):
            msg = "好的，再见。后续有香港注册或开户问题随时找我。"
        elif n in (
            "好的", "好", "收到", "明白", "了解", "嗯", "嗯嗯",
            "哦", "喔", "噢", "ok", "okay", "kk",
        ):
            msg = (
                "好的，已收到。您可以继续发送资料或提问；"
                "也可回复「/资料」查看清单，需要人工请回复「转人工」。"
            )
        else:
            # 你好 / 在吗 / 极短引导：不重复整段建群欢迎语
            msg = (
                "您好，我是赢态香港业务助手，可协助公司注册、开户与资料收集。"
                "您可以直接提问（如「注册要多久」），或发送「/资料」查看清单；"
                "需要人工请回复「转人工」。"
            )
        wm = to_external_userid or self._resolve_external_userid(roomid, "")
        self._safe_send(roomid, msg, to_external_userid=wm, customer_fallback=False)
        try:
            self.store.insert_ai_reply(roomid, "", msg, model="greeting")
        except Exception:
            logger.debug("写入寒暄回复失败 room=%s", roomid, exc_info=True)

    def _agent_mode(self) -> str:
        m = (getattr(settings, "wework_agent_mode", "") or "normal").strip().lower()
        if m not in (AGENT_MODE_NORMAL, AGENT_MODE_SHADOW, AGENT_MODE_DISABLED):
            return AGENT_MODE_NORMAL
        return m

    def _audit_intent_route(
        self,
        roomid: str,
        *,
        status: str,
        channel: str,
        text: str,
        route: MessageRouteResult,
        agent_mode: str,
    ) -> None:
        try:
            signal = route.plan.signal if route.plan else None
            steps = route.plan.step_kinds if route.plan else []
            text_hash = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
            self.store.insert_intent_route(
                roomid=roomid,
                status=status,
                channel=channel,
                text_hash=text_hash,
                rule_intent=(signal.rule_intent if signal else "") or "",
                rule_mode=(signal.rule_mode if signal else "") or "",
                model_intent=(signal.model_intent if signal else "") or "",
                model_mode=(signal.model_mode if signal else "") or "",
                model_confidence=float(signal.model_confidence if signal else 0.0),
                veto_applied=",".join(route.veto_applied or []),
                plan_steps_json=json.dumps(steps, ensure_ascii=False),
                final_intent=route.action,
                final_mode=route.reply_mode or "",
                executed_ok=True,
                agent_mode=agent_mode,
            )
        except Exception:
            logger.debug("写入 intent_routes 失败 room=%s", roomid, exc_info=True)

    def _execute_plan(
        self,
        roomid: str,
        msgid: str,
        from_id: str,
        text: str,
        *,
        plan: ActionPlan,
        route: MessageRouteResult,
        status: str,
        to_external_userid: str | None,
        agent_mode: str,
    ) -> None:
        """按 ActionPlan 有序执行；shadow 模式只审计不发送/不入库。"""
        shadow = agent_mode == AGENT_MODE_SHADOW
        channel = self._room_channel(roomid)
        wm = to_external_userid
        enqueued_qa = False

        for step in plan.steps:
            if step.kind == STEP_UPSERT_MATERIALS:
                fields = dict(step.fields or route.fields or {})
                if not fields:
                    continue
                if shadow:
                    logger.info("shadow 跳过材料入库 room=%s fields=%s", roomid, list(fields))
                    continue
                for row in fields_to_material_rows(fields, source="chat_form"):
                    fk = row.pop("field_key")
                    self.store.upsert_material(roomid, fk, **row)
                if "company_name_cn" in fields or "company_name_en" in fields:
                    self._align_materials_folder(roomid)
                if status not in (
                    GROUP_STATUS_QUEUED,
                    GROUP_STATUS_HANDOFF,
                    GROUP_STATUS_CONFIRMED,
                    GROUP_STATUS_HUMAN,
                ):
                    self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
                prefix = "【材料已更新】" if (step.is_correction or route.is_correction) else "【材料更新】"
                body = format_progress_text(
                    self.store.get_materials(roomid),
                    mode=REPLY_FULL_PROGRESS,
                    channel=channel,
                    linked_hint=self._dual_channel_hint(roomid),
                    status=status,
                )
                self._safe_send(roomid, f"{prefix}\n{body}", to_external_userid=wm)
                if is_ready_for_confirm(self.store.get_materials(roomid)):
                    self._send_review_summary(roomid, to_external_userid=wm)
                continue

            if step.kind == STEP_REPLY_PROGRESS:
                if shadow:
                    logger.info(
                        "shadow 跳过进度直答 room=%s mode=%s", roomid, step.reply_mode
                    )
                    continue
                self._reply_progress_mode(
                    roomid,
                    mode=step.reply_mode or REPLY_FULL_PROGRESS,
                    to_external_userid=wm,
                    status=status,
                )
                continue

            if step.kind == STEP_SEND_UNCLEAR:
                if shadow:
                    logger.info("shadow 跳过 unclear 提示 room=%s", roomid)
                    continue
                progress = format_progress_text(
                    self.store.get_materials(roomid),
                    channel=channel,
                    linked_hint=self._dual_channel_hint(roomid),
                    status=status,
                )
                if status not in (
                    GROUP_STATUS_QUEUED,
                    GROUP_STATUS_HANDOFF,
                    GROUP_STATUS_CONFIRMED,
                    GROUP_STATUS_HUMAN,
                ):
                    self.store.set_group_status(roomid, GROUP_STATUS_COLLECTING)
                self._safe_send(
                    roomid,
                    f"{progress}\n\n"
                    "未识别到可入库字段。请按「键=值」发送，或发送 /填表 / /模板 获取填写指引。",
                    to_external_userid=wm,
                )
                continue

            if step.kind == STEP_SEND_GREETING:
                if shadow:
                    logger.info("shadow 跳过寒暄 room=%s", roomid)
                    continue
                self._send_greeting_reply(roomid, text, to_external_userid=wm)
                continue

            if step.kind == STEP_QUEUED_TIP:
                if shadow:
                    logger.info("shadow 跳过办理中提示 room=%s", roomid)
                    continue
                active = self.store.get_active_registration_job(roomid)
                job_hint = f"（任务 #{active['id']}）" if active else ""
                self._safe_send(
                    roomid,
                    f"您的注册正在办理中{job_hint}，请稍候。"
                    "可继续咨询业务问题（如开户时长）；"
                    "如需帮助请回复「转人工」；办结后如需再跑请回复「重新办理」。",
                    to_external_userid=wm,
                    customer_fallback=False,
                )
                continue

            if step.kind == STEP_ENQUEUE_QA:
                qa_text = (step.text or text).strip()
                if not qa_text:
                    continue
                if shadow:
                    logger.info("shadow 跳过 QA 入队 room=%s q=%s", roomid, qa_text[:80])
                    continue
                self._enqueue_qa_text(roomid, msgid, from_id, qa_text)
                enqueued_qa = True
                continue

        if not enqueued_qa:
            self.store.mark_message_processed(msgid)

        # 优化 12：材料收集阶段主动提醒缺失材料（shadow 模式不发）
        if not shadow:
            self._maybe_proactive_reminder(roomid, to_external_userid=wm)

    def _ground_qa_answer(self, answer: str, roomid: str) -> str | None:
        """发前轻量护栏：若答案编造办理/收齐状态且与 DB 不符 → 弃权。"""
        text = (answer or "").strip()
        if not text:
            return None
        try:
            materials = self.store.get_materials(roomid)
            p = progress_summary(materials)
            job = self.store.get_active_registration_job(roomid)
        except Exception:
            return None
        # 宣称已收齐但实际未齐
        if re.search(r"资料已齐|材料已齐|已收齐|必填.*齐全", text):
            if not p.get("complete") or p.get("needs_review_labels"):
                return (
                    "关于材料是否收齐，请以本会话「/进度」为准；"
                    "当前仍有待补或待复核项。需要人工请回复「转人工」。"
                )
        # 编造任务号
        m = re.search(r"任务\s*#?\s*(\d+)", text)
        if m and job:
            if str(job.get("id")) != m.group(1):
                return (
                    "办理进度请以系统登记为准。您可回复「办得怎么样」查询，"
                    "或回复「转人工」。"
                )
        if m and not job and re.search(r"正在办理|已排队|办理中", text):
            gst = (self.store.get_group(roomid) or {}).get("status") or ""
            if gst not in (
                GROUP_STATUS_QUEUED,
                GROUP_STATUS_HANDOFF,
                GROUP_STATUS_CONFIRMED,
            ):
                return (
                    "当前尚未进入注册办理队列。您可继续补充资料或回复「进度」查看。"
                )
        return None

    def _dual_channel_hint(self, roomid: str) -> str:
        """KF/群材料独立提示（仅当确实存在另一通道时）。"""
        if is_kf_session(roomid):
            parsed = parse_kf_roomid(roomid)
            wm = parsed[1] if parsed else roomid.removeprefix("kf:")
            groups = self.store.get_linked_groups(wm) if wm.startswith("wm") else []
            if groups:
                return (
                    "说明：另一侧（客户群）材料独立，需在对应会话查看；"
                    "当前仅显示微信客服私聊会话。"
                )
            return ""
        return ""

    def _room_channel(self, roomid: str) -> str:
        return "kf" if is_kf_session(roomid) else "group"

    def _reply_progress_mode(
        self,
        roomid: str,
        *,
        mode: str = REPLY_FULL_PROGRESS,
        to_external_userid: str | None = None,
        status: str = "",
    ) -> None:
        """按 reply_mode 发送材料/办理/知识清单直答。"""
        channel = self._room_channel(roomid)
        linked = self._dual_channel_hint(roomid)
        gst = status or (
            (self.store.get_group(roomid) or {}).get("status") or ""
        )
        materials = self.store.get_materials(roomid)

        if mode == REPLY_KNOWLEDGE_CHECKLIST:
            # 与 /资料 同源
            self._send_checklist(
                roomid, to_external_userid=to_external_userid,
            )
            return

        if mode == REPLY_CASE_STATUS:
            try:
                job = self.store.get_active_registration_job(roomid)
            except Exception:
                job = None
            text = format_case_status_text(
                group_status=gst,
                job=job,
                materials=materials,
                channel=channel,
                linked_hint=linked,
            )
            self._safe_send(roomid, text, to_external_userid=to_external_userid)
            return

        # 收齐了吗 + needs_review → 明确未齐
        p = progress_summary(materials)
        body = format_progress_text(
            materials,
            mode=mode or REPLY_FULL_PROGRESS,
            channel=channel,
            linked_hint=linked,
            status=gst,
        )
        if mode == REPLY_FULL_PROGRESS and p.get("needs_review_labels"):
            if "未齐" not in body and "待复核" in body:
                body = body + "\n结论：尚未收齐（存在待复核项）。"
        job_line = self._job_status_line(roomid)
        if mode == REPLY_FULL_PROGRESS and job_line and (
            "办理" in job_line
            or "FAILED" in job_line
            or "QUEUED" in job_line
            or "HANDOFF" in job_line
            or "任务" in job_line
        ):
            body = f"{job_line}\n\n{body}"
        self._safe_send(roomid, body, to_external_userid=to_external_userid)

    def _try_short_ack_followup(
        self,
        roomid: str,
        text: str,
        *,
        to_external_userid: str | None = None,
    ) -> bool:
        """上一轮助手在追问字段时，短确认「对/是的」→ 引导发送键=值。"""
        if not is_short_ack(text):
            return False
        try:
            recent = self.store.get_recent_messages(roomid, limit=6)
        except Exception:
            return False
        last_assistant = ""
        for line in reversed(recent):
            if line.startswith("助手:"):
                last_assistant = line[3:].strip()
                break
        if not last_assistant:
            return False
        if not re.search(r"请补充|还需要|请按|键\s*=\s*值|联络邮箱|请发送", last_assistant):
            return False
        # 引导复读待收第一项
        mats = self.store.get_materials(roomid)
        p = progress_summary(mats)
        missing = p.get("missing_labels") or []
        tip = missing[0] if missing else "对应资料"
        self._safe_send(
            roomid,
            f"收到。请直接发送「{tip}=您的内容」，或按「键=值」补充；"
            "也可发送 /填表 获取模板。",
            to_external_userid=to_external_userid,
        )
        return True

    def _enqueue_qa_text(
        self,
        roomid: str,
        msgid: str,
        from_id: str,
        text: str,
    ) -> None:
        """防抖合并业务 QA；若与已有 pending 混入会话态句则拆开。"""
        with self._lock:
            batch = self._pending.get(roomid)
            if batch is None:
                batch = PendingBatch(roomid=roomid, from_id=from_id)
                self._pending[roomid] = batch
            # 混句策略 A：pending 已有业务句，新来会话态 → 不合并，立即直答会话态
            trial = list(batch.texts) + [text]
            if batch.texts and batch_has_mixed_session_and_biz(trial):
                if looks_like_session_state_query(text) or is_knowledge_checklist_query(text):
                    # 保留原 QA batch，会话态当场答
                    status = (
                        (self.store.get_group(roomid) or {}).get("status") or ""
                    )
                    has_m = bool(self.store.get_materials(roomid))
                    mode = resolve_reply_mode(text, status, has_materials=has_m)
                    wm = from_id if from_id.startswith("wm") else None
                    # 先重启原 QA 计时
                    if batch.timer:
                        batch.timer.cancel()
                    delay = _qa_debounce_seconds(
                        batch.texts[-1] if batch.texts else text
                    )
                    batch.timer = threading.Timer(
                        delay, self._flush_batch, args=(roomid,)
                    )
                    batch.timer.daemon = True
                    batch.timer.start()
                    self._reply_progress_mode(
                        roomid,
                        mode=mode,
                        to_external_userid=wm,
                        status=status,
                    )
                    self.store.mark_message_processed(msgid)
                    return
            batch.msgids.append(msgid)
            batch.texts.append(text)
            if not batch.from_id:
                batch.from_id = from_id
            if batch.timer:
                batch.timer.cancel()
            delay = _qa_debounce_seconds(text)
            batch.timer = threading.Timer(delay, self._flush_batch, args=(roomid,))
            batch.timer.daemon = True
            batch.timer.start()

    def _send_checklist(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
        enforce_quota: bool = False,
    ) -> None:
        wm = to_external_userid or self._resolve_external_userid(roomid, "")
        owner = self._owner(roomid)
        # 与 knowledge_checklist / format_knowledge_checklist_text 同源
        content = format_knowledge_checklist_text()
        try:
            self.external.send_session_text(
                roomid,
                content,
                sender_userid=owner,
                to_external_userid=wm,
            )
        except Exception:
            # 回退旧路径
            self.external.send_material_checklist(
                roomid, sender_userid=owner, to_external_userid=wm,
            )
        paste_target = "本会话" if is_kf_session(roomid) else "本群"
        hint = (
            f"清单见上。在线填表：{self._form_url(roomid)}"
            if settings.collect_form_enabled
            else f"清单见上。发送 /填表 获取填写模板，粘贴到{paste_target}提交；证件请直接上传。"
        )
        linked = self._dual_channel_hint(roomid)
        if linked:
            hint = f"{hint}\n{linked}"
        self._safe_send(
            roomid,
            hint,
            to_external_userid=wm,
            enforce_quota=enforce_quota,
        )

    def _send_progress(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        status = (self.store.get_group(roomid) or {}).get("status") or ""
        self._reply_progress_mode(
            roomid,
            mode=REPLY_FULL_PROGRESS,
            to_external_userid=to_external_userid,
            status=status,
        )

    def _send_review_summary(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        materials = self.store.get_materials(roomid)
        # 跨字段校验（优化 11）：error 级问题阻止进入 REVIEW
        cross_issues = validate_cross_fields(materials)
        errors = [i for i in cross_issues if i.get("level") == "error"]
        if errors:
            err_lines = "\n".join(f"  ✗ {i.get('message')}" for i in errors)
            self._safe_send(
                roomid,
                "【材料校验】资料项已收齐，但存在以下校验错误，请修正后再确认：\n"
                f"{err_lines}\n\n"
                "修正后我会再次核对。如需帮助可回复「转人工」。",
                to_external_userid=to_external_userid,
            )
            return
        company_data = aggregate_company_data(materials)
        summary = self.llm.confirm_materials_summary(company_data)
        self.store.set_group_status(roomid, GROUP_STATUS_REVIEW)
        self._safe_send(
            roomid,
            f"【材料确认】资料已齐全，可以开始注册。\n{summary}\n\n"
            f"请核对无误后回复「确认」或「开始注册」。",
            to_external_userid=to_external_userid,
        )

    def _maybe_proactive_reminder(
        self, roomid: str, *, to_external_userid: str | None = None
    ) -> None:
        """COLLECTING 等状态下按消息计数/速率限制主动提醒缺失材料（优化 12）。"""
        if not getattr(settings, "materials_proactive_reminder_enabled", True):
            return
        status = (self.store.get_group(roomid) or {}).get("status") or ""
        if status not in (
            GROUP_STATUS_WELCOMED,
            GROUP_STATUS_QA,
            GROUP_STATUS_COLLECTING,
        ):
            return
        # 速率限制：距上次提醒不足间隔则跳过
        interval = float(
            getattr(settings, "materials_proactive_reminder_interval", 3600.0)
            or 3600.0
        )
        now = time.monotonic()
        if now - self._last_reminder_at.get(roomid, 0.0) < interval:
            return
        # 消息计数门槛：每 N 条消息触发一次检查
        every_n = int(
            getattr(settings, "materials_proactive_reminder_every_n_messages", 5) or 5
        )
        count = self._message_counts.get(roomid, 0)
        if count < every_n or count % every_n != 0:
            return
        max_items = int(
            getattr(settings, "materials_proactive_reminder_max_items", 3) or 3
        )
        materials = self.store.get_materials(roomid)
        missing = prioritized_missing(materials, limit=max_items)
        if not missing:
            return
        self._last_reminder_at[roomid] = now
        lines = ["📌 温馨提醒：以下材料尚未收到，补齐后即可开始注册："]
        for m in missing:
            mark = "（关键）" if m.get("critical") else ""
            lines.append(f"  - {m.get('label')}{mark}")
        if not all(m.get("critical") for m in missing) and any(
            m.get("critical") for m in missing
        ):
            lines.append("标「关键」为必填核心项，请优先补充。")
        lines.append("可发送「/资料」查看完整清单，或按「键=值」提交。")
        self._safe_send(
            roomid,
            "\n".join(lines),
            to_external_userid=to_external_userid,
            enforce_quota=True,
        )

    def _handle_confirm(
        self,
        roomid: str,
        from_id: str,
        *,
        force_redo: bool = False,
    ) -> None:
        wm = self._resolve_external_userid(roomid, from_id)
        self.ensure_default_material_contacts(roomid)
        materials = self.store.get_materials(roomid)
        if not is_ready_for_confirm(materials):
            self._safe_send(
                roomid,
                f"必填材料尚未齐全。\n{format_progress_text(materials)}",
                to_external_userid=wm,
            )
            return

        active = self.store.get_active_registration_job(roomid)
        if active:
            self._safe_send(
                roomid,
                f"注册任务 #{active.get('id')} 已在办理或排队中（{active.get('status')}），请勿重复确认。",
                to_external_userid=wm,
                customer_fallback=False,
            )
            return

        latest = self.store.get_latest_registration_job(roomid)
        if (
            not force_redo
            and latest
            and str(latest.get("status") or "") == "succeeded"
        ):
            self._safe_send(
                roomid,
                f"注册任务 #{latest.get('id')} 已办理完成。"
                "如需再次自动填表，请回复「重新办理」。",
                to_external_userid=wm,
                customer_fallback=False,
            )
            return

        dry_run = bool(settings.dry_run)
        allow_submit = (not dry_run) and bool(settings.icris_allow_submit)
        materials = self.store.get_materials(roomid)
        try:
            from src.materials.aggregator import aggregate_company_data

            company_data = aggregate_company_data(materials)
        except Exception as e:
            logger.exception("确认注册时聚合资料失败 room=%s", roomid)
            self._safe_send(
                roomid,
                f"资料聚合失败，无法入队：{e}",
                to_external_userid=wm,
                customer_fallback=False,
            )
            return
        company_name = str(
            company_data.get("company_name_en")
            or company_data.get("company_name_cn")
            or ""
        ).strip()
        job, created = self.store.enqueue_registration_job(
            roomid,
            customer_id=from_id or wm or "",
            dry_run=dry_run,
            allow_submit=allow_submit,
            max_attempts=settings.icris_job_max_attempts,
            payload=company_data,
            source="wework",
            company_name=company_name,
        )
        job_id = job.get("id")
        self.store.set_group_status(roomid, GROUP_STATUS_QUEUED)

        if not created:
            self._safe_send(
                roomid,
                f"注册任务 #{job_id} 已在队列中（状态 {job.get('status')}），请稍候。",
                to_external_userid=wm,
                customer_fallback=False,
            )
            return

        submit_note = (
            "排队办理中，将尝试自动提交 ICRIS。"
            if allow_submit
            else "排队办理中；当前为填表预览（不自动提交），完成后由专员复核。"
        )
        redo_note = "（重新办理）" if force_redo else ""
        self._safe_send(
            roomid,
            f"已受理{redo_note}，进入注册队列（任务 #{job_id}）。\n{submit_note}",
            to_external_userid=wm,
        )

    def _run_handoff(self, roomid: str, from_id: str) -> None:
        """兼容旧路径：同步执行（新路径走 registration_jobs 队列）。"""
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

    def _is_resume_bot_command(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if t in (
            "继续咨询",
            "取消转人工",
            "转回机器人",
            "转回助手",
            "/继续咨询",
            "/取消转人工",
        ):
            return True
        n = _normalize_social(t)
        return n in ("继续咨询", "取消转人工", "转回机器人", "转回助手")

    def _resume_from_human(
        self,
        roomid: str,
        *,
        to_external_userid: str | None = None,
    ) -> None:
        """客户显式恢复智能助手。"""
        materials = self.store.get_materials(roomid)
        new_status = (
            GROUP_STATUS_COLLECTING if materials else GROUP_STATUS_QA
        )
        self.store.set_group_status(roomid, new_status)
        try:
            self.store.clear_human_notified(roomid)
        except Exception:
            logger.debug("清除 human_notified_at 失败 room=%s", roomid, exc_info=True)
        self._human_acked.discard(roomid)
        self._safe_send(
            roomid,
            "已恢复智能助手。您可继续提问或发送资料；"
            "需要人工时请再次回复「转人工」。",
            to_external_userid=to_external_userid,
            customer_fallback=False,
        )

    def _transfer_human(self, roomid: str, from_id: str) -> None:
        owner = str(self._owner(roomid) or "")
        wm = self._resolve_external_userid(roomid, from_id)
        group = self.store.get_group(roomid) or {}
        already = bool(str(group.get("human_notified_at") or "").strip())
        self.store.set_group_status(roomid, GROUP_STATUS_HUMAN)
        # 全会话只提示一次（DB 持久化，防重启重复刷屏）
        if not already:
            self.store.mark_human_notified(roomid)
            self._human_acked.add(roomid)
            self._safe_send(
                roomid,
                "已为您转接人工专员，老师会尽快回复。"
                "您也可继续向我咨询业务问题或发送资料；"
                "回复「继续咨询」可明确恢复助手优先。",
                to_external_userid=wm,
            )
        else:
            self._safe_send(
                roomid,
                "已再次通知专员。您可继续提问或发送资料。",
                to_external_userid=wm,
            )
        channel = "客服私聊" if is_kf_session(roomid) else "外部群"
        # 通知专员非致命：corpsecret/应用未配或 40001 不得阻断客户转接与 inbox 完成
        if owner and settings.wework_configured:
            try:
                result = self.external.send_text_to_user(
                    owner,
                    f"【{channel}转人工】会话 {roomid}\n客户 {from_id or wm or ''}",
                )
                if isinstance(result, dict) and int(result.get("errcode") or 0) != 0:
                    logger.warning(
                        "转人工通知专员失败 owner=%s err=%s",
                        owner,
                        result,
                    )
            except Exception as exc:
                logger.warning(
                    "转人工通知专员异常 owner=%s: %s（客户侧转接已完成）",
                    owner,
                    exc,
                )
        elif owner and not settings.wework_configured:
            logger.warning(
                "跳过转人工通知专员：未配置 WEWORK_CORP_SECRET/AGENT_ID（需自建应用 Secret，非客服 Secret）"
            )

    def ensure_group_registered(self, roomid: str, owner_userid: str = "") -> None:
        if not self.store.get_group(roomid):
            self.store.upsert_group(
                roomid,
                owner_userid=owner_userid or self.external.default_owner_userid,
                status=GROUP_STATUS_WELCOMED,
            )
            self._maybe_ensure_form_token(roomid)
