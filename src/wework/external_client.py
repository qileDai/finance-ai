"""企业微信外部群 API 客户端"""

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
class WeWorkExternalClient:
    """客户群消息收发（外部群）"""

    corp_id: str = ""
    corp_secret: str = ""
    agent_id: str = ""
    default_owner_userid: str = ""
    _access_token: str = ""
    _token_expires: float = 0
    _mock_mode: bool = False
    _mock_messages: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.corp_id = self.corp_id or settings.wework_corp_id
        self.corp_secret = self.corp_secret or settings.wework_corp_secret
        self.agent_id = self.agent_id or settings.wework_agent_id
        self.default_owner_userid = (
            self.default_owner_userid or settings.wework_default_group_owner_userid
        )
        self._mock_mode = not settings.wework_configured

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

    def get_group_chat(self, chat_id: str) -> dict[str, Any] | None:
        """获取客户群详情"""
        if self._mock_mode:
            return {
                "chat_id": chat_id,
                "name": "Mock 客户群",
                "owner": self.default_owner_userid or "mock_owner",
                "member_list": [],
            }

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/groupchat/get?access_token={token}"
        resp = httpx.post(url, json={"chat_id": chat_id, "need_name": 1}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("获取客户群详情失败: %s", data)
            return None
        return data.get("group_chat") or data.get("groupChat")

    def _resolve_sender(self, chat_id: str, sender_userid: str | None) -> str:
        if sender_userid:
            return sender_userid
        if self.default_owner_userid:
            return self.default_owner_userid
        detail = self.get_group_chat(chat_id)
        if detail and detail.get("owner"):
            return str(detail["owner"])
        raise RuntimeError(
            f"未配置 WEWORK_DEFAULT_GROUP_OWNER_USERID，且无法从群 {chat_id} 获取 owner"
        )

    def _external_userids_from_group(self, chat_id: str) -> list[str]:
        detail = self.get_group_chat(chat_id)
        if not detail:
            return []
        members = detail.get("member_list") or []
        ids: list[str] = []
        for m in members:
            uid = m.get("userid") or m.get("user_id") or ""
            mtype = m.get("type", 2)
            if uid and mtype == 2:
                ids.append(uid)
        return ids

    def send_group_text(
        self,
        chat_id: str,
        content: str,
        *,
        sender_userid: str | None = None,
    ) -> dict[str, Any]:
        """向客户群发送文本（企业群发 add_msg_template）"""
        if self._mock_mode:
            logger.info("[Mock 外部群] 群 %s: %s", chat_id, content[:200])
            self._mock_messages.append({"chat_id": chat_id, "content": content})
            return {"errcode": 0, "errmsg": "ok (mock)"}

        sender = self._resolve_sender(chat_id, sender_userid)
        external_userids = self._external_userids_from_group(chat_id)
        if not external_userids:
            logger.warning("群 %s 无外部成员，尝试仅按 chat_id 发送", chat_id)

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/add_msg_template?access_token={token}"
        payload: dict[str, Any] = {
            "chat_type": "group",
            "sender": sender,
            "text": {"content": content},
        }
        if external_userids:
            payload["external_userid"] = external_userids
        else:
            payload["chat_id_list"] = [chat_id]

        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("客户群发消息失败: %s", data)
        else:
            logger.info("已向客户群 %s 发送消息 (sender=%s)", chat_id, sender)
        return data

    def send_text_to_user(self, userid: str, content: str) -> dict[str, Any]:
        """通知企业成员（如转人工时通知群主）"""
        if self._mock_mode:
            logger.info("[Mock 企微] 通知成员 %s: %s", userid, content[:200])
            return {"errcode": 0, "errmsg": "ok (mock)"}

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": userid,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": content},
            "safe": 0,
        }
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_material_checklist(self, chat_id: str, *, sender_userid: str | None = None) -> str:
        checklist_path = PROJECT_ROOT / "templates" / "material_checklist.md"
        content = checklist_path.read_text(encoding="utf-8")
        self.send_group_text(chat_id, content, sender_userid=sender_userid)
        return content

    def get_mock_messages(self, chat_id: str | None = None) -> list[dict[str, str]]:
        if chat_id:
            return [m for m in self._mock_messages if m["chat_id"] == chat_id]
        return list(self._mock_messages)
