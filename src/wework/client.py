"""企业微信 API 客户端（支持 Mock 模式）"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


@dataclass
class MockMessage:
    chat_id: str
    sender: str
    content: str
    msg_type: str = "text"


@dataclass
class WeWorkClient:
    """企业微信消息收发客户端"""

    corp_id: str = ""
    corp_secret: str = ""
    agent_id: str = ""
    _access_token: str = ""
    _token_expires: float = 0
    _mock_messages: list[MockMessage] = field(default_factory=list)
    _mock_mode: bool = False

    def __post_init__(self) -> None:
        self.corp_id = self.corp_id or settings.wework_corp_id
        self.corp_secret = self.corp_secret or settings.wework_corp_secret
        self.agent_id = self.agent_id or settings.wework_agent_id
        self._mock_mode = not settings.wework_configured
        if self._mock_mode:
            logger.info("企业微信未配置，使用 Mock 模式")

    def _get_access_token(self) -> str:
        if self._mock_mode:
            return "mock_token"
        if self._access_token and time.time() < self._token_expires:
            return self._access_token
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        resp = httpx.get(
            url,
            params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        self._access_token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 7200) - 300
        return self._access_token

    def send_text(self, user_id: str, content: str) -> dict[str, Any]:
        """发送文本消息给指定用户"""
        if self._mock_mode:
            logger.info("[Mock 企微] 发送给用户 %s: %s", user_id, content[:100])
            self._mock_messages.append(MockMessage(user_id, "agent", content))
            return {"errcode": 0, "errmsg": "ok (mock)"}

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": content},
            "safe": 0,
        }
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_group_text(self, chat_id: str, content: str) -> dict[str, Any]:
        """发送消息到群聊（通过 appchat/send）"""
        if self._mock_mode:
            logger.info("[Mock 企微群] 群 %s: %s", chat_id, content[:100])
            self._mock_messages.append(MockMessage(chat_id, "agent", content))
            return {"errcode": 0, "errmsg": "ok (mock)"}

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
        payload = {"chatid": chat_id, "msgtype": "text", "text": {"content": content}}
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_material_checklist(self, chat_id: str) -> str:
        """发送材料清单到客户群"""
        checklist_path = PROJECT_ROOT / "templates" / "material_checklist.md"
        content = checklist_path.read_text(encoding="utf-8")
        self.send_group_text(chat_id, content)
        return content

    def push_mock_customer_message(self, chat_id: str, content: str) -> None:
        """Mock 模式下模拟客户消息"""
        self._mock_messages.append(MockMessage(chat_id, "customer", content))

    def get_mock_messages(self, chat_id: str | None = None) -> list[MockMessage]:
        if chat_id:
            return [m for m in self._mock_messages if m.chat_id == chat_id]
        return list(self._mock_messages)
