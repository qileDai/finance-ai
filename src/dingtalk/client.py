"""钉钉群机器人客户端（支持 Stream Mode 接收消息 + Mock 模式）"""

from __future__ import annotations

import logging
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class DingTalkMessage:
    """钉钉收到的消息"""
    sender_id: str        # 发送者 openId
    sender_name: str      # 发送者昵称
    content: str          # 消息文本
    chat_id: str          # 群聊 ID
    conversation_type: str  # 1=单聊, 2=群聊
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 钉钉客户端
# ---------------------------------------------------------------------------

@dataclass
class DingTalkClient:
    """钉钉机器人客户端（发送消息 + Stream Mode 接收消息）"""

    app_key: str = ""
    app_secret: str = ""
    _access_token: str = ""
    _token_expires: float = 0
    _mock_mode: bool = False
    _command_handlers: dict[str, Callable] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.app_key = self.app_key or settings.dingtalk_app_key
        self.app_secret = self.app_secret or settings.dingtalk_app_secret
        self._mock_mode = not settings.dingtalk_configured
        if self._mock_mode:
            logger.info("钉钉未配置，使用 Mock 模式")

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """获取钉钉 access_token"""
        if self._mock_mode:
            return "mock_dingtalk_token"
        if self._access_token and time.time() < self._token_expires:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        resp = httpx.post(
            url,
            json={"appKey": self.app_key, "appSecret": self.app_secret},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["accessToken"]
        self._token_expires = time.time() + data.get("expireIn", 7200) - 300
        return self._access_token

    # ------------------------------------------------------------------
    # 发送消息
    # ------------------------------------------------------------------

    def send_group_markdown(self, chat_id: str, title: str, text: str) -> dict[str, Any]:
        """发送 Markdown 消息到群聊"""
        if self._mock_mode:
            logger.info("[Mock 钉钉群] 群 %s 发送 Markdown: %s", chat_id, title)
            return {"code": 0, "msg": "ok (mock)"}

        token = self._get_access_token()
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        payload = {
            "robotCode": self.app_key,
            "openConversationId": chat_id,
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "title": title,
                "text": text,
            }),
        }
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_group_text(self, chat_id: str, content: str) -> dict[str, Any]:
        """发送文本消息到群聊"""
        if self._mock_mode:
            logger.info("[Mock 钉钉群] 群 %s: %s", chat_id, content[:100])
            return {"code": 0, "msg": "ok (mock)"}

        token = self._get_access_token()
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        payload = {
            "robotCode": self.app_key,
            "openConversationId": chat_id,
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": content}),
        }
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_material_checklist(self, chat_id: str) -> str:
        """发送材料清单到钉钉群（Markdown 格式）"""
        checklist_path = PROJECT_ROOT / "templates" / "material_checklist.md"
        content = checklist_path.read_text(encoding="utf-8")
        self.send_group_markdown(chat_id, "香港公司注册材料清单", content)
        return content

    # ------------------------------------------------------------------
    # 命令注册
    # ------------------------------------------------------------------

    def register_command(self, command: str, handler: Callable[[DingTalkMessage], None]) -> None:
        """注册命令处理器"""
        self._command_handlers[command] = handler

    def handle_message(self, msg: DingTalkMessage) -> str | None:
        """处理收到的消息，匹配命令"""
        content = msg.content.strip()
        logger.info("[钉钉] 收到消息: %s 来自 %s: %s", msg.chat_id, msg.sender_name, content)

        # 匹配命令
        for cmd, handler in self._command_handlers.items():
            if content.startswith(cmd):
                logger.info("[钉钉] 匹配命令: %s", cmd)
                handler(msg)
                return cmd

        logger.info("[钉钉] 未匹配任何命令: %s", content)
        return None

    # ------------------------------------------------------------------
    # Stream Mode（接收消息）
    # ------------------------------------------------------------------

    def start_stream_listener(self, blocking: bool = True) -> None:
        """启动 Stream Mode 监听，接收钉钉群消息"""
        if self._mock_mode:
            logger.info("[Mock 钉钉] Stream Mode 模拟启动（不会真实连接）")
            if blocking:
                logger.info("[Mock 钉钉] 模拟运行中，按 Ctrl+C 退出...")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    logger.info("[Mock 钉钉] 模拟停止")
            return

        logger.info("[钉钉] 启动 Stream Mode 监听...")
        try:
            asyncio.run(self._run_stream())
        except KeyboardInterrupt:
            logger.info("[钉钉] Stream Mode 已停止")

    async def _run_stream(self) -> None:
        """异步运行 Stream Mode"""
        try:
            from dingtalk_stream import AckMessage
            import dingtalk_stream
        except ImportError:
            logger.error(
                "未安装 dingtalk-stream，请执行: pip install dingtalk-stream"
            )
            return

        def on_message_callback(headers: dict, body: dict) -> Any:
            """处理收到的消息回调"""
            logger.debug("[钉钉 Stream] 收到消息: %s", json.dumps(body, ensure_ascii=False)[:200])

            # 只处理文本消息
            msg_type = body.get("msgtype", "")
            if msg_type != "text":
                return AckMessage.STATUS_OK, "ok"

            text_content = body.get("text", {}).get("content", "").strip()
            sender_id = body.get("senderStaffId", body.get("senderId", ""))
            sender_name = body.get("senderNick", "未知")
            conversation_type = body.get("conversationType", "2")
            chat_id = body.get("conversationId", body.get("sessionWebhook", ""))

            # 去掉 @机器人 前缀
            if text_content.startswith("@"):
                # 格式: @机器人名 实际内容
                parts = text_content.split(" ", 1)
                if len(parts) > 1:
                    text_content = parts[1].strip()

            msg = DingTalkMessage(
                sender_id=sender_id,
                sender_name=sender_name,
                content=text_content,
                chat_id=chat_id,
                conversation_type=conversation_type,
                raw=body,
            )
            self.handle_message(msg)
            return AckMessage.STATUS_OK, "ok"

        # 创建 Stream 客户端
        credential = dingtalk_stream.Credential(self.app_key, self.app_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)
        client.register_all_callback(on_message_callback)

        logger.info("[钉钉] Stream 客户端已启动，等待消息...")
        await client.start()