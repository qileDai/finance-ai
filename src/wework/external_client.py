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
        self.kf_open_kfid = self.kf_open_kfid or settings.wework_kf_default_open_kfid
        self.send_mode = self.send_mode or settings.wework_external_send_mode_resolved
        self.group_webhook_url = (
            self.group_webhook_url or settings.wework_external_group_webhook_url
        )
        self._mock_mode = not (
            settings.wework_configured or settings.wework_kf_configured
        )

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
        if to_external_userid and self.kf_secret and settings.wework_kf_configured:
            return f"kf: 自动私聊 → {to_external_userid}（仅该客户，当前群 {chat_id}）"
        if mode in ("kf", "auto") and self.kf_secret and settings.wework_kf_configured:
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

    def _send_via_kf_group(
        self,
        chat_id: str,
        content: str,
        *,
        open_kfid: str | None = None,
    ) -> dict[str, Any] | None:
        """仅向 chat_id 对应群内的外部成员自动私聊（不波及其他群）"""
        targets = self.external_userids_in_group(chat_id)
        if not targets:
            logger.warning("群 %s 无外部成员，无法 kf 自动发送", chat_id)
            return None

        ok = 0
        last: dict[str, Any] = {"errcode": -1, "errmsg": "no send attempt"}
        kfid = open_kfid or self.kf_open_kfid or settings.wework_kf_default_open_kfid
        for uid in targets:
            data = self._send_via_kf(uid, content, open_kfid=kfid)
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

    def _send_via_kf(
        self,
        external_userid: str,
        content: str,
        *,
        open_kfid: str | None = None,
    ) -> dict[str, Any]:
        """微信客服私聊（无需群主确认，消息出现在客户微信客服会话中）"""
        import time

        kfid = (open_kfid or self.kf_open_kfid or settings.wework_kf_default_open_kfid).strip()
        if not kfid or not self.kf_secret:
            raise RuntimeError("未配置 WEWORK_KF_OPEN_KFID / WEWORK_KF_SECRET")

        token = self._get_kf_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={token}"
        payload = {
            "touser": external_userid,
            "open_kfid": kfid,
            "msgtype": "text",
            "text": {"content": content},
        }
        data: dict[str, Any] = {}
        # 45009 API 频控：指数退避最多重试 2 次
        for attempt in range(3):
            resp = httpx.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            errcode = int(data.get("errcode", 0) or 0)
            if errcode == 0:
                logger.info("已通过微信客服 [%s] 自动发送给 %s", kfid, external_userid)
                return data
            if errcode == 45009 and attempt < 2:
                delay = 1.0 * (2 ** attempt)
                logger.warning(
                    "微信客服限流 45009，%.1fs 后重试 (%d/3) open_kfid=%s",
                    delay,
                    attempt + 1,
                    kfid,
                )
                time.sleep(delay)
                continue
            logger.error(
                "微信客服发消息失败 open_kfid=%s touser=%s: %s",
                kfid,
                external_userid,
                data,
            )
            return data
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

    def send_kf_text(
        self,
        external_userid: str,
        content: str,
        *,
        open_kfid: str | None = None,
    ) -> dict[str, Any]:
        """向微信客服会话发送文本（客户私聊侧可见）"""
        kfid = open_kfid or self.kf_open_kfid or settings.wework_kf_default_open_kfid
        if self._mock_mode:
            logger.info("[Mock 客服 %s] → %s: %s", kfid, external_userid, content[:200])
            from src.wework.kf_session import build_kf_roomid

            self._mock_messages.append(
                {
                    "chat_id": build_kf_roomid(kfid, external_userid),
                    "to": external_userid,
                    "open_kfid": kfid,
                    "content": content,
                }
            )
            return {"errcode": 0, "errmsg": "ok (mock)"}
        return self._send_via_kf(external_userid, content, open_kfid=kfid)

    def sync_kf_messages(
        self,
        *,
        open_kfid: str,
        cursor: str = "",
        token: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        """拉取微信客服消息（sync_msg）"""
        kfid = (open_kfid or self.kf_open_kfid or settings.wework_kf_default_open_kfid).strip()
        if self._mock_mode:
            return {
                "errcode": 0,
                "errmsg": "ok (mock)",
                "msg_list": [],
                "next_cursor": cursor,
                "has_more": 0,
            }
        if not kfid or not self.kf_secret:
            raise RuntimeError("未配置 WEWORK_KF_OPEN_KFID / WEWORK_KF_SECRET")

        access_token = self._get_kf_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}"
        payload: dict[str, Any] = {
            "cursor": cursor or "",
            "token": token or "",
            "limit": limit,
            "voice_format": 0,
            "open_kfid": kfid,
        }
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"kf/sync_msg 失败: {data}")
        return data

    def send_session_text(
        self,
        roomid: str,
        content: str,
        *,
        sender_userid: str | None = None,
        to_external_userid: str | None = None,
    ) -> dict[str, Any]:
        """按 roomid 通道发送：kf:* → 客服私聊，wr* → 群/私聊（依 send_mode）"""
        from src.wework.kf_session import parse_kf_roomid

        roomid = (roomid or "").strip()
        if roomid.startswith("kf:"):
            parsed = parse_kf_roomid(roomid)
            if parsed:
                open_kfid, wm_default = parsed
                wm = to_external_userid or wm_default
                return self.send_kf_text(wm, content, open_kfid=open_kfid)
            wm = to_external_userid or roomid.removeprefix("kf:")
            return self.send_kf_text(wm, content)
        return self.send_group_text(
            roomid,
            content,
            sender_userid=sender_userid,
            to_external_userid=to_external_userid,
        )

    def download_kf_media(self, media_id: str) -> bytes:
        """下载微信客服消息中的临时素材（GET /cgi-bin/media/get）"""
        # 最小合法 JPEG 头，供 mock / 本地联调，避免「下载失败」假阴性
        _mock_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        if self._mock_mode:
            return _mock_jpeg
        if not media_id:
            return b""
        access_token = self._get_kf_access_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/get"
        resp = httpx.get(
            url,
            params={"access_token": access_token, "media_id": media_id},
            timeout=60,
        )
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type or resp.content[:1] == b"{":
            try:
                data = resp.json()
            except Exception:
                data = {"errmsg": resp.text[:200]}
            err = data.get("errcode", 0) if isinstance(data, dict) else -1
            if err:
                raise RuntimeError(f"media/get 失败: {data}")
            logger.warning("media/get 返回 JSON 但无 errcode: %s", data)
            return b""
        if not resp.content:
            return b""
        return resp.content

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
        if to_external_userid and mode in ("kf", "auto") and self.kf_secret and settings.wework_kf_configured:
            logger.info("发送计划: %s", self.describe_send_plan(chat_id, to_external_userid=to_external_userid))
            return self._send_via_kf(to_external_userid, content)

        # 2. mass 模式：仅当前群企业群发（chat_id_list 锁定单群）
        if mode == "mass":
            logger.info("发送计划: %s", self.describe_send_plan(chat_id))
            sender = self._resolve_sender(chat_id, sender_userid)
            return self._send_via_mass(chat_id, content, sender)

        # 3. kf/auto：优先对当前群内外部成员自动私聊（仅该 roomid 的成员，不波及其他群）
        if mode in ("kf", "auto") and self.kf_secret and settings.wework_kf_configured:
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
        """通知企业成员（如转人工时通知群主）。

        使用自建应用 corpsecret + agentid，不能用微信客服 Secret。
        """
        if self._mock_mode:
            logger.info("[Mock 企微] 通知成员 %s: %s", userid, content[:200])
            return {"errcode": 0, "errmsg": "ok (mock)"}

        uid = (userid or "").strip()
        if not uid:
            logger.warning("send_text_to_user 跳过：userid 为空")
            return {"errcode": -1, "errmsg": "userid empty"}

        if not (
            (self.corp_id or "").strip()
            and (self.corp_secret or "").strip()
            and str(self.agent_id or "").strip()
        ):
            logger.warning(
                "send_text_to_user 跳过：缺少 WEWORK_CORP_ID / WEWORK_CORP_SECRET / WEWORK_AGENT_ID"
                "（须为自建应用 Secret，不能用 WEWORK_KF_SECRET）"
            )
            return {
                "errcode": -1,
                "errmsg": "app credentials not configured",
            }

        try:
            agentid = int(self.agent_id)
        except (TypeError, ValueError):
            logger.warning("send_text_to_user 跳过：WEWORK_AGENT_ID 无效: %s", self.agent_id)
            return {"errcode": -1, "errmsg": "invalid agentid"}

        try:
            token = self._get_access_token()
        except Exception as exc:
            logger.warning("send_text_to_user 获取应用 access_token 失败: %s", exc)
            return {"errcode": 40001, "errmsg": str(exc)}

        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": uid,
            "msgtype": "text",
            "agentid": agentid,
            "text": {"content": content},
            "safe": 0,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if int(data.get("errcode") or 0) != 0:
                logger.warning("send_text_to_user 发送失败 userid=%s: %s", uid, data)
            return data
        except Exception as exc:
            logger.warning("send_text_to_user 请求异常 userid=%s: %s", uid, exc)
            return {"errcode": -1, "errmsg": str(exc)}

    def send_material_checklist(
        self,
        chat_id: str,
        *,
        sender_userid: str | None = None,
        to_external_userid: str | None = None,
    ) -> str:
        checklist_path = PROJECT_ROOT / "templates" / "material_checklist.md"
        content = checklist_path.read_text(encoding="utf-8")
        self.send_session_text(
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
