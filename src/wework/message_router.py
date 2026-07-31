"""统一路由：存档消息 / 回调事件 / Mock 注入"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from src.storage.db import ExternalGroupStore
from src.wework.callback_handler import ExternalChatEvent, is_group_create_event
from src.wework.group_state_machine import GroupStateMachine
from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ArchiveTextMessage:
    msgid: str
    roomid: str
    from_id: str
    content: str


@dataclass
class ArchiveFileMessage:
    msgid: str
    roomid: str
    from_id: str
    msgtype: str
    sdkfileid: str
    filename: str = "upload.bin"


@dataclass
class MessageRouter:
    """消息与事件统一入口"""

    state_machine: GroupStateMachine = field(default_factory=GroupStateMachine)
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)

    def route_external_chat_event(self, evt: ExternalChatEvent) -> None:
        if not is_group_create_event(evt):
            logger.debug("忽略客户群变更: %s", evt.change_type)
            return
        logger.info("客户群事件 %s chat_id=%s", evt.change_type, evt.chat_id)
        self.state_machine.handle_group_created(evt.chat_id)

    def route_archive_text(self, msg: ArchiveTextMessage) -> None:
        if not msg.roomid or not msg.content.strip():
            return
        is_new = self.store.insert_message_if_new(
            msg.msgid,
            msg.roomid,
            msg.from_id,
            "text",
            msg.content,
        )
        if not is_new:
            logger.debug("重复消息 %s，跳过", msg.msgid)
            return
        self.state_machine.handle_incoming_text(
            msg.roomid,
            msg.msgid,
            msg.from_id,
            msg.content,
        )

    def route_archive_file(self, msg: ArchiveFileMessage, file_data: bytes) -> None:
        if not msg.roomid or not file_data:
            return
        is_new = self.store.insert_message_if_new(
            msg.msgid, msg.roomid, msg.from_id, msg.msgtype, msg.filename,
        )
        if not is_new:
            return
        from src.wework.material_handler import MaterialHandler

        handler = MaterialHandler(store=self.store)
        field_key = handler.save_file_message(
            msg.roomid, msg.msgid, msg.filename, file_data, use_llm=settings.openai_api_key != "",
        )
        self.state_machine.handle_file_received(msg.roomid, msg.msgid, field_key, msg.filename)

    def inject_mock_file(
        self, roomid: str, file_path: str, from_id: str = "mock_external_user",
    ) -> None:
        from pathlib import Path

        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(file_path)
        msgid = f"mockfile_{uuid.uuid4().hex[:12]}"
        msg = ArchiveFileMessage(
            msgid=msgid,
            roomid=roomid,
            from_id=from_id,
            msgtype="file",
            sdkfileid="",
            filename=p.name,
        )
        self.route_archive_file(msg, p.read_bytes())
        logger.info("[Mock] 已注入文件到群 %s: %s", roomid, p.name)

    def inject_mock_message(self, roomid: str, text: str, from_id: str = "mock_external_user") -> None:
        """CLI Mock 注入客户消息"""
        self.state_machine.ensure_group_registered(roomid)
        msgid = f"mock_{uuid.uuid4().hex[:16]}"
        self.route_archive_text(
            ArchiveTextMessage(
                msgid=msgid,
                roomid=roomid,
                from_id=from_id,
                content=text,
            )
        )
        logger.info("[Mock] 已注入消息到群 %s: %s", roomid, text[:80])

    def inject_kf_message(
        self,
        external_userid: str,
        text: str,
        *,
        msgid: str | None = None,
        open_kfid: str = "",
    ) -> None:
        """CLI Mock 注入微信客服私聊消息"""
        from src.wework.kf_session import build_kf_roomid

        if not external_userid.startswith("wm"):
            external_userid = f"wm{external_userid}"
        kfid = open_kfid or settings.wework_kf_default_open_kfid or "wkMockKf"
        roomid = build_kf_roomid(kfid, external_userid)
        mid = msgid or f"mock_kf_{uuid.uuid4().hex[:16]}"
        is_new = self.store.insert_message_if_new(
            mid, roomid, external_userid, "text", text,
        )
        if not is_new:
            logger.debug("[Mock] 重复 kf 消息 %s", mid)
            return
        self.state_machine.handle_incoming_text(
            roomid, mid, external_userid, text,
        )
        logger.info("[Mock] 已注入 kf 消息 [%s] %s: %s", kfid, external_userid, text[:80])

    def simulate_kf_first_contact(
        self, external_userid: str, *, open_kfid: str = "",
    ) -> None:
        if not external_userid.startswith("wm"):
            external_userid = f"wm{external_userid}"
        self.state_machine.handle_kf_first_contact(
            external_userid, open_kfid=open_kfid,
        )

    def simulate_kf_callback(
        self, open_kfid: str, token: str = "mock_token",
    ) -> None:
        """Mock 模拟 kf_msg_or_event 回调触发 sync"""
        from src.wework.kf_worker import KfSyncWorker

        worker = KfSyncWorker(
            store=self.store,
            external=self.state_machine.external,
            state_machine=self.state_machine,
        )
        worker.sync_for_account(open_kfid, token=token)

    def simulate_group_create(self, roomid: str, *, force: bool = False) -> None:
        """Mock 模拟建群事件"""
        evt = ExternalChatEvent(
            event="change_external_chat",
            change_type="create",
            chat_id=roomid,
            raw={},
        )
        if is_group_create_event(evt):
            self.state_machine.handle_group_created(evt.chat_id, force=force)
