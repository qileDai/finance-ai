"""微信客服 sync_msg：回调触发 + 轮询兜底，支持多 open_kfid"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from config.settings import settings
from src.storage.db import ExternalGroupStore
from src.wework.external_client import WeWorkExternalClient
from src.wework.group_state_machine import GroupStateMachine
from src.wework.kf_session import build_kf_roomid

logger = logging.getLogger(__name__)

# origin=3：微信用户（客户）发送
KF_ORIGIN_CUSTOMER = 3

UNSUPPORTED_MSG_TIP = "暂不支持该消息类型，请发送文字说明，或上传证件图片/PDF 文件。"


@dataclass
class KfSyncWorker:
    """kf/sync_msg 拉取客户私聊并交给统一状态机"""

    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    external: WeWorkExternalClient = field(default_factory=WeWorkExternalClient)
    state_machine: GroupStateMachine | None = None
    _running: bool = False
    _thread: threading.Thread | None = None
    _recover_thread: threading.Thread | None = None
    _sync_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.state_machine is None:
            self.state_machine = GroupStateMachine(store=self.store, external=self.external)
        if settings.wework_kf_configured:
            self.store.ensure_kf_cursors(settings.wework_kf_open_kfid_list)

    def on_kf_callback(self, open_kfid: str, token: str = "") -> None:
        """收到 kf_msg_or_event 后异步触发 sync（快速返回 200 后调用）"""
        if not settings.wework_kf_configured:
            return
        threading.Thread(
            target=self._safe_sync_for_account,
            args=(open_kfid, token),
            daemon=True,
            name=f"kf-callback-{open_kfid[:8]}",
        ).start()

    def _safe_sync_for_account(self, open_kfid: str, token: str = "") -> None:
        try:
            self.sync_for_account(open_kfid, token=token)
        except Exception as e:
            logger.exception("kf 回调 sync 失败 open_kfid=%s: %s", open_kfid, e)

    def sync_for_account(self, open_kfid: str, *, token: str = "") -> int:
        """对指定客服账号循环 sync_msg 直到 has_more=0，返回处理消息数"""
        if not open_kfid:
            return 0
        allowed = set(settings.wework_kf_open_kfid_list)
        if allowed and open_kfid not in allowed:
            logger.warning("忽略未配置的 open_kfid: %s", open_kfid)
            return 0

        handled = 0
        with self._sync_lock:
            while True:
                cursor, stored_token = self.store.get_kf_cursor(open_kfid)
                sync_token = token or stored_token
                data = self.external.sync_kf_messages(
                    open_kfid=open_kfid,
                    cursor=cursor,
                    token=sync_token,
                )
                next_cursor = str(data.get("next_cursor") or cursor)
                next_token = str(data.get("token") or sync_token)
                if next_cursor != cursor or next_token != sync_token:
                    self.store.set_kf_cursor(open_kfid, next_cursor, next_token)
                token = ""

                for msg in data.get("msg_list") or []:
                    if self._handle_message(msg, open_kfid):
                        handled += 1

                if not data.get("has_more"):
                    break
        if handled:
            logger.info("kf sync open_kfid=%s 处理 %d 条客户消息", open_kfid, handled)
        return handled

    def start_polling(
        self,
        *,
        interval: int | None = None,
        blocking: bool = False,
    ) -> None:
        if not settings.wework_kf_poll_enabled:
            logger.info("kf 轮询未启动（WEWORK_KF_MODE=%s）", settings.wework_kf_mode_resolved)
            return
        if not settings.wework_kf_configured:
            logger.warning("kf 未配置 Secret/账号，sync worker 未启动")
            return

        poll = interval or settings.wework_kf_poll_interval
        accounts = settings.wework_kf_open_kfid_list

        def _loop() -> None:
            self._running = True
            logger.info(
                "kf 轮询已启动，间隔 %ds，账号 %d 个",
                poll,
                len(accounts),
            )
            try:
                self.recover_stale_inbox()
            except Exception as e:
                logger.exception("kf 启动 inbox 恢复失败: %s", e)
            while self._running:
                for open_kfid in accounts:
                    try:
                        self.sync_for_account(open_kfid)
                    except Exception as e:
                        logger.exception("kf 轮询 sync 异常 %s: %s", open_kfid, e)
                try:
                    self.recover_stale_inbox()
                except Exception as e:
                    logger.exception("kf inbox 恢复失败: %s", e)
                time.sleep(poll)

        if blocking:
            _loop()
        else:
            self._thread = threading.Thread(target=_loop, daemon=True, name="kf-sync-worker")
            self._thread.start()

    def start_inbox_recover(self, *, interval: int | None = None) -> None:
        """push-only 模式也定时扫描未处理 inbox（崩溃恢复）。"""
        if self._recover_thread and self._recover_thread.is_alive():
            return
        poll = max(30, int(interval or settings.wework_kf_poll_interval or 120))

        def _loop() -> None:
            self._running = True
            logger.info("kf inbox 恢复扫描已启动，间隔 %ds", poll)
            try:
                self.recover_stale_inbox()
            except Exception as e:
                logger.exception("kf 启动 inbox 恢复失败: %s", e)
            while self._running:
                time.sleep(poll)
                if not self._running:
                    break
                try:
                    self.recover_stale_inbox()
                except Exception as e:
                    logger.exception("kf inbox 恢复失败: %s", e)

        self._recover_thread = threading.Thread(
            target=_loop, daemon=True, name="kf-inbox-recover"
        )
        self._recover_thread.start()

    def stop_polling(self) -> None:
        self._running = False

    def recover_stale_inbox(self) -> int:
        """重投超时未处理的文本消息（进程崩溃后 processed=0）。"""
        stale = int(getattr(settings, "wework_inbox_stale_seconds", 120) or 120)
        batch = int(getattr(settings, "wework_inbox_recover_batch", 20) or 20)
        rows = self.store.list_unprocessed_messages(
            older_than_seconds=stale,
            limit=batch,
            msgtype="text",
        )
        if not rows:
            return 0
        assert self.state_machine is not None
        recovered = 0
        for row in rows:
            msgid = str(row.get("msgid") or "")
            roomid = str(row.get("roomid") or "")
            from_id = str(row.get("from_id") or "")
            content = str(row.get("content") or "").strip()
            if not msgid or not roomid or not content:
                if msgid:
                    self.store.mark_message_processed(msgid)
                continue
            try:
                logger.warning(
                    "恢复未处理 inbox msgid=%s room=%s: %s",
                    msgid,
                    roomid,
                    content[:60],
                )
                self.state_machine.handle_incoming_text(
                    roomid, msgid, from_id, content,
                )
                recovered += 1
            except Exception as e:
                logger.exception(
                    "恢复 inbox 失败 msgid=%s: %s", msgid, e
                )
                # 避免死循环：失败也标记已处理并尽量告知客户
                try:
                    self.external.send_session_text(
                        roomid,
                        "刚才有一条消息处理异常，请再发一次；或回复「转人工」。",
                        to_external_userid=from_id if from_id.startswith("wm") else None,
                    )
                except Exception:
                    pass
                self.store.mark_message_processed(msgid)
        if recovered:
            logger.info("inbox 恢复重投 %d 条", recovered)
        return recovered

    def _handle_message(self, msg: dict, open_kfid: str) -> bool:
        origin = msg.get("origin")
        if origin != KF_ORIGIN_CUSTOMER:
            return False

        msgtype = msg.get("msgtype") or ""
        external_userid = str(msg.get("external_userid") or "").strip()
        if not external_userid:
            return False

        msg_open_kfid = str(msg.get("open_kfid") or open_kfid).strip() or open_kfid
        msgid = str(msg.get("msgid") or f"kf_{msg_open_kfid}_{external_userid}_{msg.get('send_time', '')}")
        roomid = build_kf_roomid(msg_open_kfid, external_userid)

        if msgtype == "text":
            text_obj = msg.get("text") or {}
            content = str(text_obj.get("content") or "").strip()
            if not content:
                return False
            is_new = self.store.insert_message_if_new(
                msgid, roomid, external_userid, "text", content,
            )
            if not is_new:
                return False
            logger.info(
                "kf 收到客户消息 [%s] %s: %s",
                msg_open_kfid,
                external_userid,
                content[:80],
            )
            assert self.state_machine is not None
            self.state_machine.handle_incoming_text(
                roomid, msgid, external_userid, content,
            )
            return True

        if msgtype in ("image", "file"):
            media_id = ""
            filename = "upload.bin"
            if msgtype == "image":
                media_id = str((msg.get("image") or {}).get("media_id") or "")
                filename = f"{msgid[:12] or 'image'}.jpg"
            else:
                file_obj = msg.get("file") or {}
                media_id = str(file_obj.get("media_id") or "")
                filename = str(file_obj.get("filename") or "upload.bin")
            if not media_id:
                return False
            is_new = self.store.insert_message_if_new(
                msgid, roomid, external_userid, msgtype, filename,
            )
            if not is_new:
                return False
            logger.info("kf 收到客户 [%s] %s 文件: %s", msg_open_kfid, external_userid, filename)
            self._handle_kf_file(
                roomid, msgid, external_userid, media_id, filename,
                open_kfid=msg_open_kfid,
            )
            return True

        # 语音/视频/链接等：明确提示，禁止静默丢弃
        is_new = self.store.insert_message_if_new(
            msgid, roomid, external_userid, msgtype or "unknown", msgtype or "",
        )
        if not is_new:
            return False
        logger.info(
            "kf 不支持的消息类型 [%s] %s msgtype=%s",
            msg_open_kfid,
            external_userid,
            msgtype,
        )
        self._reply_kf_file_error(
            roomid,
            external_userid,
            UNSUPPORTED_MSG_TIP,
            open_kfid=msg_open_kfid,
        )
        self.store.mark_message_processed(msgid)
        return True

    def _reply_kf_file_error(
        self,
        roomid: str,
        external_userid: str,
        text: str,
        *,
        open_kfid: str = "",
    ) -> None:
        """文件链路失败时直接回复客户，不进意图/QA 静默路径"""
        try:
            self.external.send_session_text(
                roomid,
                text,
                to_external_userid=external_userid,
            )
        except Exception as send_exc:
            logger.error(
                "kf 文件失败回复发送失败 room=%s user=%s kfid=%s: %s | %s",
                roomid,
                external_userid,
                open_kfid,
                send_exc,
                text[:80],
            )

    def _handle_kf_file(
        self,
        roomid: str,
        msgid: str,
        external_userid: str,
        media_id: str,
        filename: str,
        *,
        open_kfid: str = "",
    ) -> None:
        assert self.state_machine is not None
        try:
            data = self.external.download_kf_media(media_id)
            if not data:
                self._reply_kf_file_error(
                    roomid,
                    external_userid,
                    "图片/文件下载失败，请重新发送；若多次失败请回复「转人工」。",
                    open_kfid=open_kfid,
                )
                self.store.mark_message_processed(msgid)
                return
            from src.wework.material_handler import MaterialHandler

            handler = MaterialHandler(store=self.store)
            field_key = handler.save_file_message(
                roomid, msgid, filename, data,
                use_llm=bool(settings.openai_api_key),
            )
            self.state_machine.handle_file_received(
                roomid, msgid, field_key, filename,
                to_external_userid=external_userid,
            )
        except Exception as e:
            logger.exception("kf 文件处理失败 %s: %s", external_userid, e)
            self._reply_kf_file_error(
                roomid,
                external_userid,
                "文件处理失败，请重试一次；仍不行请回复「转人工」。",
                open_kfid=open_kfid,
            )
            self.store.mark_message_processed(msgid)
