"""企业微信客户联系事件回调解析"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExternalChatEvent:
    """客户群变更事件"""

    event: str
    change_type: str
    chat_id: str
    raw: dict[str, str]


def _text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def parse_external_callback_xml(xml_text: str) -> ExternalChatEvent | None:
    """解析解密后的客户联系回调 XML"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error("回调 XML 解析失败: %s", e)
        return None

    msg_type = _text(root, "MsgType")
    if msg_type != "event":
        return None

    event = _text(root, "Event")
    if event != "change_external_chat":
        logger.debug("忽略非客户群事件: %s", event)
        return None

    chat_id = _text(root, "ChatId")
    change_type = _text(root, "ChangeType")
    if not chat_id:
        logger.warning("change_external_chat 缺少 ChatId")
        return None

    raw = {child.tag: (child.text or "") for child in root}
    return ExternalChatEvent(
        event=event,
        change_type=change_type,
        chat_id=chat_id,
        raw=raw,
    )


def is_group_create_event(evt: ExternalChatEvent) -> bool:
    return evt.change_type in ("create", "update") and bool(evt.chat_id)
