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
    kf_secret: str = ""
    kf_open_kfid: str = ""
    send_mode: str = ""
    group_webhook_url: str = ""
    _access_token: str = ""
    _token_expires: float = 0
    _kf_access_token: str = ""
    _kf_token_expires: float = 0
    _mock_mode: bool = False
    _mock_messages: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.corp_id = self.corp_id or settings.wework_corp_id
        self.corp_secret = self.corp_secret or settings.wework_corp_secret
        self.agent_id = self.agent_id or settings.wework_agent_id
        self.default_owner_userid = (
            self.default_owner_userid or settings.wework_default_group_owner_userid
        )
        self.kf_secret = self.kf_secret or settings.wework_kf_secret
        self.kf_open_kfid = self.kf_open_kfid or settings.wework_kf_open_kfid
        self.send_mode = self.send_mode or settings.wework_external_send_mode_resolved
        self.group_webhook_url = (
            self.group_webhook_url or settings.wework_external_group_webhook_url
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

    def _get_kf_access_token(self) -> str:
        if self._mock_mode:
            return "mock_kf_token"
        if self._kf_access_token and time.time() < self._kf_token_expires:
            return self._kf_access_token

        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        resp = httpx.get(
            url,
            params={"corpid": self.corp_id, "corpsecret": self.kf_secret},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"获取微信客服 access_token 失败: {data}")
        self._kf_access_token = data["access_token"]
        self._kf_token_expires = time.time() + data.get("expires_in", 7200) - 300
        return self._kf_access_token

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
        detail = self.get_group_chat(chat_id)
        if detail and detail.get("owner"):
            return str(detail["owner"])
        if self.default_owner_userid:
            return self.default_owner_userid
        raise RuntimeError(
            f"未配置 WEWORK_DEFAULT_GROUP_OWNER_USERID，且无法从群 {chat_id} 获取 owner"
        )

    def _group_owner(self, chat_id: str) -> str:
        detail = self.get_group_chat(chat_id) or {}
        owner = str(detail.get("owner") or "").strip()
        if owner:
            return owner
        if self.default_owner_userid:
            return self.default_owner_userid
        raise RuntimeError(f"无法确定群 {chat_id} 的群主 userid")

    def external_userids_in_group(self, chat_id: str) -> list[str]:
        """当前群内的外部联系人 userid（wm 开头），不会跨群"""
        detail = self.get_group_chat(chat_id)
        if not detail:
            return []
        members = detail.get("member_list") or []
        ids: list[str] = []
        for m in members:
            uid = str(m.get("userid") or m.get("user_id") or "").strip()
            mtype = m.get("type", 2)
            if uid and mtype == 2:
                ids.append(uid)
        return ids

    def describe_send_plan(self, chat_id: str, *, to_external_userid: str | None = None) -> str:
        mode = (self.send_mode or "mass").strip().lower()
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return "缺少 chat_id"
        if mode == "mass":
            return f"mass: 企业群发 chat_id_list=[{chat_id}]（需群主确认）"
        if to_external_userid and self.kf_secret and self.kf_open_kfid:
            return f"kf: 自动私聊 → {to_external_userid}（仅该客户，当前群 {chat_id}）"
        if mode in ("kf", "auto") and self.kf_secret and self.kf_open_kfid:
            members = self.external_userids_in_group(chat_id)
            if members:
                return (
                    f"kf: 自动私聊 → 群 {chat_id} 内 {len(members)} 位外部客户 "
                    f"（不波及其他群；消息在客服会话非群聊）"
                )
            return f"kf: 群 {chat_id} 无外部成员，回退 mass"
        if mode == "webhook" and self.group_webhook_url:
            return "webhook: 单 Webhook 即时推送"
        return f"mass: 企业群发 chat_id_list=[{chat_id}]（需群主确认）"

    def _send_via_kf_group(self, chat_id: str, content: str) -> dict[str, Any] | None:
        """仅向 chat_id 对应群内的外部成员自动私聊（不波及其他群）"""
        targets = self.external_userids_in_group(chat_id)
        if not targets:
            logger.warning("群 %s 无外部成员，无法 kf 自动发送", chat_id)
            return None

        ok = 0
        last: dict[str, Any] = {"errcode": -1, "errmsg": "no send attempt"}
        for uid in targets:
            data = self._send_via_kf(uid, content)
            last = data
            if data.get("errcode", 0) == 0:
                ok += 1
        logger.info("群 %s kf 自动发送: %s/%s 成功", chat_id, ok, len(targets))
        return last if ok else None

    def _send_via_webhook(self, content: str) -> dict[str, Any]:
        url = (self.group_webhook_url or "").strip()
        if not url:
            raise RuntimeError("未配置 WEWORK_EXTERNAL_GROUP_WEBHOOK_URL")
        resp = httpx.post(
            url,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("Webhook 发消息失败: %s", data)
        else:
            logger.info("已通过 Webhook 发送消息（即时入群）")
        return data

    def _send_via_kf(self, external_userid: str, content: str) -> dict[str, Any]:
        """微信客服私聊（无需群主确认，消息出现在客户微信客服会话中）"""
        if not self.kf_open_kfid or not self.kf_secret:
            raise RuntimeError("未配置 WEWORK_KF_OPEN_KFID / WEWORK_KF_SECRET")

        token = self._get_kf_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={token}"
        payload = {
            "touser": external_userid,
            "open_kfid": self.kf_open_kfid,
            "msgtype": "text",
            "text": {"content": content},
        }
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("微信客服发消息失败 touser=%s: %s", external_userid, data)
        else:
            logger.info("已通过微信客服自动发送给 %s", external_userid)
        return data

    def _send_via_mass(self, chat_id: str, content: str, sender: str) -> dict[str, Any]:
        """企业群发（需群主在企微里点确认，消息才会出现在群里）

        必须指定 chat_id_list，否则企微会把任务下发给 sender 的全部客户群。
        """
        chat_id = (chat_id or "").strip()
        if not chat_id:
            raise ValueError("mass 群发缺少 chat_id，无法限定目标群")

        owner = self._group_owner(chat_id)
        if sender != owner:
            logger.info("群发 sender %s → 使用群 %s 的群主 %s", sender, chat_id, owner)
            sender = owner

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/add_msg_template?access_token={token}"
        payload: dict[str, Any] = {
            "chat_type": "group",
            "sender": sender,
            "chat_id_list": [chat_id],
            "text": {"content": content},
        }
        logger.info("企业群发 → 仅群 %s (sender=%s)", chat_id, sender)
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("客户群发消息失败: %s", data)
        else:
            msgid = data.get("msgid", "")
            logger.warning(
                "已创建企业群发任务 msgid=%s，需群主 %s 在企微【服务通知】确认后才会出现在群里",
                msgid,
                sender,
            )
            if msgid:
                try:
                    remind_url = (
                        f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/"
                        f"remind_groupmsg_send?access_token={token}"
                    )
                    httpx.post(remind_url, json={"msgid": msgid}, timeout=30)
                except Exception as e:
                    logger.debug("提醒群主确认群发失败: %s", e)
        return data

    def _send_via_appchat(self, chat_id: str, content: str) -> dict[str, Any]:
        """内部群 appchat（即时入群，外部客户群 chat_id 通常不可用）"""
        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
        resp = httpx.post(
            url,
            json={"chatid": chat_id, "msgtype": "text", "text": {"content": content}},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error("appchat 发消息失败: %s", data)
        else:
            logger.info("已通过 appchat 即时发送到群 %s", chat_id)
        return data

    def send_kf_text(self, external_userid: str, content: str) -> dict[str, Any]:
        """向微信客服会话发送文本（客户私聊侧可见）"""
        if self._mock_mode:
            logger.info("[Mock 客服] → %s: %s", external_userid, content[:200])
            self._mock_messages.append(
                {"chat_id": f"kf:{external_userid}", "to": external_userid, "content": content}
            )
            return {"errcode": 0, "errmsg": "ok (mock)"}
        return self._send_via_kf(external_userid, content)

    def sync_kf_messages(
        self,
        *,
        cursor: str = "",
        token: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        """拉取微信客服消息（sync_msg）"""
        if self._mock_mode:
            return {
                "errcode": 0,
                "errmsg": "ok (mock)",
                "msg_list": [],
                "next_cursor": cursor,
                "has_more": 0,
            }
        if not self.kf_open_kfid or not self.kf_secret:
            raise RuntimeError("未配置 WEWORK_KF_OPEN_KFID / WEWORK_KF_SECRET")

        access_token = self._get_kf_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}"
        payload: dict[str, Any] = {
            "cursor": cursor or "",
            "token": token or "",
            "limit": limit,
            "voice_format": 0,
            "open_kfid": self.kf_open_kfid,
        }
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"kf/sync_msg 失败: {data}")
        return data

    def send_group_text(
        self,
        chat_id: str,
        content: str,
        *,
        sender_userid: str | None = None,
        to_external_userid: str | None = None,
    ) -> dict[str, Any]:
        """向客户群/客户发送文本。

        群消息（欢迎语、清单等）始终通过 chat_id_list 只发到当前群。
        回复某个客户时可用微信客服私聊（to_external_userid）。
        """
        if self._mock_mode:
            logger.info("[Mock 外部群] 群 %s → %s: %s", chat_id, to_external_userid, content[:200])
            self._mock_messages.append(
                {"chat_id": chat_id, "to": to_external_userid or "", "content": content}
            )
            return {"errcode": 0, "errmsg": "ok (mock)"}

        mode = (self.send_mode or "mass").strip().lower()
        chat_id = (chat_id or "").strip()
        if not chat_id:
            raise ValueError("send_group_text 缺少 chat_id")

        # 1. 回复指定外部联系人 → 仅该客户（kf 自动，无需群主确认）
        if to_external_userid and mode in ("kf", "auto") and self.kf_secret and self.kf_open_kfid:
            logger.info("发送计划: %s", self.describe_send_plan(chat_id, to_external_userid=to_external_userid))
            return self._send_via_kf(to_external_userid, content)

        # 2. mass 模式：仅当前群企业群发（chat_id_list 锁定单群）
        if mode == "mass":
            logger.info("发送计划: %s", self.describe_send_plan(chat_id))
            sender = self._resolve_sender(chat_id, sender_userid)
            return self._send_via_mass(chat_id, content, sender)

        # 3. kf/auto：优先对当前群内外部成员自动私聊（仅该 roomid 的成员，不波及其他群）
        if mode in ("kf", "auto") and self.kf_secret and self.kf_open_kfid:
            logger.info("发送计划: %s", self.describe_send_plan(chat_id))
            kf_result = self._send_via_kf_group(chat_id, content)
            if kf_result is not None:
                return kf_result
            logger.warning("群 %s kf 自动发送失败，回退企业群发（需群主确认）", chat_id)

        # 4. Webhook（单群）
        if mode in ("webhook", "auto") and (self.group_webhook_url or "").strip():
            return self._send_via_webhook(content)

        # 5. 内部群 appchat
        if not chat_id.startswith("wr") and mode in ("appchat", "auto"):
            data = self._send_via_appchat(chat_id, content)
            if data.get("errcode", 0) == 0:
                return data

        # 6. 回退：企业群发，仅 chat_id_list=[当前群]
        sender = self._resolve_sender(chat_id, sender_userid)
        return self._send_via_mass(chat_id, content, sender)

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

    def send_material_checklist(
        self,
        chat_id: str,
        *,
        sender_userid: str | None = None,
        to_external_userid: str | None = None,
    ) -> str:
        checklist_path = PROJECT_ROOT / "templates" / "material_checklist.md"
        content = checklist_path.read_text(encoding="utf-8")
        self.send_group_text(
            chat_id,
            content,
            sender_userid=sender_userid,
            to_external_userid=to_external_userid,
        )
        return content

    def get_mock_messages(self, chat_id: str | None = None) -> list[dict[str, str]]:
        if chat_id:
            return [m for m in self._mock_messages if m["chat_id"] == chat_id]
        return list(self._mock_messages)
