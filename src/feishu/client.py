"""飞书群机器人客户端（支持 WebSocket 模式接收消息 + Mock 模式）"""

from __future__ import annotations

import logging
import json
import time
import threading
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
class FeishuMessage:
    """飞书收到的消息"""
    message_id: str        # 消息 ID
    sender_id: str         # 发送者 open_id
    sender_name: str       # 发送者昵称
    content: str           # 消息文本内容
    chat_id: str           # 群聊 ID
    chat_type: str         # group / p2p
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 飞书客户端
# ---------------------------------------------------------------------------

@dataclass
class FeishuClient:
    """飞书机器人客户端（发送消息 + WebSocket 接收消息）"""

    app_id: str = ""
    app_secret: str = ""
    _tenant_access_token: str = ""
    _token_expires: float = 0
    _mock_mode: bool = False
    _command_handlers: dict[str, Callable] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.app_id = self.app_id or settings.feishu_app_id
        self.app_secret = self.app_secret or settings.feishu_app_secret
        self._mock_mode = not settings.feishu_configured
        if self._mock_mode:
            logger.info("飞书未配置，使用 Mock 模式")

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _get_tenant_access_token(self) -> str:
        """获取飞书 tenant_access_token"""
        if self._mock_mode:
            return "mock_feishu_token"
        if self._tenant_access_token and time.time() < self._token_expires:
            return self._tenant_access_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = httpx.post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code", -1) != 0:
            raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data}")
        self._tenant_access_token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200) - 300
        return self._tenant_access_token

    # ------------------------------------------------------------------
    # 发送消息到群聊
    # ------------------------------------------------------------------

    def _send_message(self, chat_id: str, msg_type: str, content: str) -> dict[str, Any]:
        """发送消息到飞书群聊"""
        if self._mock_mode:
            logger.info("[Mock 飞书群] 群 %s 发送 %s: %s", chat_id, msg_type, content[:100])
            return {"code": 0, "msg": "ok (mock)"}

        token = self._get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": content,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_group_text(self, chat_id: str, content: str) -> dict[str, Any]:
        """发送文本消息到群聊"""
        return self._send_message(
            chat_id,
            "text",
            json.dumps({"text": content}),
        )

    def send_group_interactive(self, chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
        """发送卡片消息到群聊"""
        return self._send_message(
            chat_id,
            "interactive",
            json.dumps(card),
        )

    def send_material_checklist(self, chat_id: str) -> str:
        """发送材料清单到飞书群（卡片消息）"""
        checklist_path = PROJECT_ROOT / "templates" / "material_checklist.md"
        content = checklist_path.read_text(encoding="utf-8")

        # 构建飞书卡片消息
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "香港公司注册材料清单"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                }
            ],
        }
        self.send_group_interactive(chat_id, card)
        return content

    # ------------------------------------------------------------------
    # 命令注册
    # ------------------------------------------------------------------

    def register_command(self, command: str, handler: Callable[[FeishuMessage], None]) -> None:
        """注册命令处理器"""
        self._command_handlers[command] = handler

    def handle_message(self, msg: FeishuMessage) -> str | None:
        """处理收到的消息，匹配命令"""
        content = msg.content.strip()
        logger.info("[飞书] 收到消息: %s 来自 %s: %s", msg.chat_id, msg.sender_name, content)

        for cmd, handler in self._command_handlers.items():
            if content.startswith(cmd):
                logger.info("[飞书] 匹配命令: %s", cmd)
                handler(msg)
                return cmd

        logger.info("[飞书] 未匹配任何命令: %s", content)
        return None

    # ------------------------------------------------------------------
    # WebSocket 模式（接收消息）
    # ------------------------------------------------------------------

    def start_ws_listener(self, blocking: bool = True) -> None:
        """启动 WebSocket 监听，接收飞书群消息"""
        if self._mock_mode:
            logger.info("[Mock 飞书] WebSocket 模拟启动（不会真实连接）")
            if blocking:
                logger.info("[Mock 飞书] 模拟运行中，按 Ctrl+C 退出...")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    logger.info("[Mock 飞书] 模拟停止")
            return

        logger.info("[飞书] 启动 WebSocket 监听...")
        try:
            self._run_ws()
        except KeyboardInterrupt:
            logger.info("[飞书] WebSocket 已停止")

    def _run_ws(self) -> None:
        """运行 WebSocket 客户端"""
        try:
            from lark_oapi.event import EventDispatcherHandler
            from lark_oapi.ws import Client as WsClient
        except ImportError:
            logger.error(
                "未安装 lark-oapi，请执行: pip install lark-oapi"
            )
            return

        def on_message(dispatcher: EventDispatcherHandler) -> None:
            """处理收到的消息事件"""
            def handler(data: dict) -> None:
                event = data.get("event", {})
                msg_type = event.get("message_type", "")

                # 只处理文本消息
                if msg_type != "text":
                    return

                message = event.get("message", {})
                text_content = message.get("content", "")
                # 飞书消息的 content 是 JSON 字符串: {"text":"消息内容"}
                try:
                    content_obj = json.loads(text_content)
                    text_content = content_obj.get("text", text_content)
                except (json.JSONDecodeError, TypeError):
                    pass

                text_content = text_content.strip()

                # 去掉 @机器人 前缀（飞书 @ 是 <at user_id="xxx">name</at> 格式）
                import re
                text_content = re.sub(r"<at[^>]*>.*?</at>", "", text_content).strip()

                msg = FeishuMessage(
                    message_id=message.get("message_id", ""),
                    sender_id=event.get("sender", {}).get("sender_id", {}).get("open_id", ""),
                    sender_name=event.get("sender", {}).get("sender_id", {}).get("open_id", "未知"),
                    content=text_content,
                    chat_id=message.get("chat_id", ""),
                    chat_type=message.get("chat_type", "group"),
                    raw=data,
                )
                self.handle_message(msg)

            dispatcher.on("im.message.receive_v1", handler)

        # 创建 WebSocket 客户端
        ws_client = WsClient(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=on_message,
        )

        logger.info("[飞书] WebSocket 客户端已启动，等待消息...")
        ws_client.start()