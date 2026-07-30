"""微信客服 sync_msg 轮询：接收客户私聊并智能回复"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from config.settings import settings
from src.storage.db import ExternalGroupStore
from src.wework.external_client import WeWorkExternalClient
from src.wework.group_state_machine import GroupStateMachine

logger = logging.getLogger(__name__)

# origin=3：微信用户（客户）发送
KF_ORIGIN_CUSTOMER = 3


@dataclass
class KfSyncWorker:
    """轮询 kf/sync_msg，将客户私聊文本交给状态机 debounce + RAG 回复"""

    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    external: WeWorkExternalClient = field(default_factory=WeWorkExternalClient)
    state_machine: GroupStateMachine | None = None
    _running: bool = False
    _thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        if self.state_machine is None:
            self.state_machine = GroupStateMachine(store=self.store, external=self.external)

    def start_polling(
        self,
        *,
        interval: int | None = None,
        blocking: bool = False,
    ) -> None:
        if not settings.wework_kf_sync_enabled:
            logger.info("kf sync worker 未启动（WEWORK_KF_SYNC_ENABLED=false）")
            return
        if not settings.wework_kf_configured:
            logger.warning("kf 未配置 Secret/OpenKfId，sync worker 未启动")
            return

        poll = interval or settings.wework_kf_poll_interval

        def _loop() -> None:
            self._running = True
            logger.info("kf sync worker 已启动，轮询间隔 %ds", poll)
            while self._running:
                try:
                    self._poll_once()
                except Exception as e:
                    logger.exception("kf sync 轮询异常: %s", e)
                time.sleep(poll)

        if blocking:
            _loop()
        else:
            self._thread = threading.Thread(target=_loop, daemon=True, name="kf-sync-worker")
            self._thread.start()

    def stop_polling(self) -> None:
        self._running = False

    def _poll_once(self) -> None:
        while True:
            cursor, token = self.store.get_kf_cursor()
            data = self.external.sync_kf_messages(cursor=cursor, token=token)
            next_cursor = str(data.get("next_cursor") or cursor)
            next_token = str(data.get("token") or token)
            if next_cursor != cursor or next_token != token:
                self.store.set_kf_cursor(next_cursor, next_token)

            msg_list = data.get("msg_list") or []
            for msg in msg_list:
                self._handle_message(msg)

            if not data.get("has_more"):
                break

    def _handle_message(self, msg: dict) -> None:
        origin = msg.get("origin")
        if origin != KF_ORIGIN_CUSTOMER:
            return

        msgtype = msg.get("msgtype") or ""
        if msgtype != "text":
            return

        external_userid = str(msg.get("external_userid") or "").strip()
        if not external_userid:
            return

        text_obj = msg.get("text") or {}
        content = str(text_obj.get("content") or "").strip()
        if not content:
            return

        msgid = str(msg.get("msgid") or f"kf_{external_userid}_{msg.get('send_time', '')}")
        roomid = f"kf:{external_userid}"

        is_new = self.store.insert_message_if_new(
            msgid, roomid, external_userid, "text", content,
        )
        if not is_new:
            return

        logger.info("kf 收到客户消息 %s: %s", external_userid, content[:80])
        assert self.state_machine is not None
        self.state_machine.handle_kf_incoming_text(external_userid, msgid, content)
