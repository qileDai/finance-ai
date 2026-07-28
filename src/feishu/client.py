"""飞书群机器人客户端（支持 WebSocket 模式接收消息 + Mock 模式）"""

from __future__ import annotations

import logging
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


@dataclass
class FeishuMessage:
    """飞书收到的消息"""

    message_id: str
    sender_id: str
    sender_name: str
    content: str
    chat_id: str
    chat_type: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeishuClient:
    """飞书机器人客户端（发送消息 + WebSocket 接收消息）"""

    app_id: str = ""
    app_secret: str = ""
    _tenant_access_token: str = ""
    _token_expires: float = 0
    _mock_mode: bool = False
    _command_handlers: dict[str, Callable] = field(default_factory=dict)
    _cached_chat_id: str = ""

    def __post_init__(self) -> None:
        self.app_id = self.app_id or settings.feishu_app_id
        self.app_secret = self.app_secret or settings.feishu_app_secret
        self._mock_mode = not settings.feishu_configured
        if self._mock_mode:
            logger.info("飞书未配置，使用 Mock 模式")

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _http(self) -> httpx.Client:
        """绕过系统代理，避免 SSL/代理导致飞书 API 失败"""
        return httpx.Client(timeout=30, trust_env=False)

    def _get_tenant_access_token(self) -> str:
        if self._mock_mode:
            return "mock_feishu_token"
        if self._tenant_access_token and time.time() < self._token_expires:
            return self._tenant_access_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        with self._http() as client:
            resp = client.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("code", -1) != 0:
            raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data}")
        self._tenant_access_token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200) - 300
        return self._tenant_access_token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_tenant_access_token()}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # 群查找
    # ------------------------------------------------------------------

    def resolve_target_chat_id(self) -> str | None:
        """优先 FEISHU_CHAT_ID，否则按群名查找；仅有 Webhook 时返回 webhook 哨兵"""
        if self._cached_chat_id:
            return self._cached_chat_id
        chat_id = (settings.feishu_chat_id or "").strip()
        if chat_id:
            self._cached_chat_id = chat_id
            return chat_id
        name = (settings.feishu_chat_name or "").strip()
        if name:
            found = self.find_chat_by_name(name)
            if found:
                self._cached_chat_id = found
                return found
        if settings.feishu_webhook_configured:
            self._cached_chat_id = "webhook"
            logger.info("使用群自定义机器人 Webhook 发消息（无 chat_id）")
            return "webhook"
        return None

    def find_chat_by_name(self, name: str) -> str | None:
        """在机器人所在群列表中按名称匹配 chat_id"""
        if self._mock_mode:
            logger.info("[Mock 飞书] find_chat_by_name(%s) → mock_chat_icris", name)
            return "mock_chat_icris"

        target = name.strip()
        page_token = ""
        try:
            while True:
                params: dict[str, Any] = {"page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                url = "https://open.feishu.cn/open-apis/im/v1/chats"
                with self._http() as client:
                    resp = client.get(url, headers=self._auth_headers(), params=params)
                    resp.raise_for_status()
                    data = resp.json()
                if data.get("code", -1) != 0:
                    logger.error("获取群列表失败: %s", data)
                    return None
                items = (data.get("data") or {}).get("items") or []
                for item in items:
                    chat_name = (item.get("name") or "").strip()
                    if chat_name == target or target in chat_name or chat_name in target:
                        chat_id = item.get("chat_id") or ""
                        logger.info("找到飞书群: %s → %s", chat_name, chat_id)
                        return chat_id
                if not (data.get("data") or {}).get("has_more"):
                    break
                page_token = (data.get("data") or {}).get("page_token") or ""
                if not page_token:
                    break
        except Exception as e:
            logger.error("查找飞书群失败: %s", e)
            return None

        logger.warning("未找到飞书群: %s", target)
        return None

    def _webhook_url(self) -> str:
        return (settings.feishu_webhook_url or "").strip()

    def _send_via_webhook(self, msg_type: str, content: str | dict[str, Any]) -> dict[str, Any]:
        """通过群自定义机器人 Webhook 发消息"""
        url = self._webhook_url()
        if not url:
            raise RuntimeError("未配置 FEISHU_WEBHOOK_URL")

        if self._mock_mode and not url:
            logger.info("[Mock 飞书 Webhook] %s: %s", msg_type, str(content)[:200])
            return {"code": 0, "msg": "ok (mock)"}

        if msg_type == "text":
            if isinstance(content, str):
                try:
                    body_content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    body_content = {"text": content}
            else:
                body_content = content
            payload: dict[str, Any] = {"msg_type": "text", "content": body_content}
        elif msg_type == "interactive":
            card = content
            if isinstance(content, str):
                card = json.loads(content)
            payload = {"msg_type": "interactive", "card": card}
        else:
            payload = {"msg_type": msg_type, "content": content}

        with self._http() as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        # 自定义机器人成功时通常 StatusCode=0 或 code=0
        if data.get("code", 0) not in (0, None) and data.get("StatusCode", 0) not in (0, None):
            logger.warning("Webhook 发送失败: %s", data)
        else:
            logger.info("Webhook 已发送 %s", msg_type)
        return data

    def _should_use_webhook(self, chat_id: str) -> bool:
        if not self._webhook_url():
            return False
        cid = (chat_id or "").strip()
        return cid in ("", "webhook") or cid.startswith("http")

    # ------------------------------------------------------------------
    # 发送消息
    # ------------------------------------------------------------------

    def _send_message(self, chat_id: str, msg_type: str, content: str) -> dict[str, Any]:
        if self._mock_mode and not self._webhook_url():
            logger.info("[Mock 飞书群] 群 %s 发送 %s: %s", chat_id, msg_type, content[:200])
            return {"code": 0, "msg": "ok (mock)"}

        if self._should_use_webhook(chat_id):
            return self._send_via_webhook(msg_type, content)

        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": content,
        }
        try:
            with self._http() as client:
                resp = client.post(url, json=payload, headers=self._auth_headers())
                resp.raise_for_status()
                data = resp.json()
            if data.get("code", 0) == 0:
                return data
            logger.warning("开放平台发消息失败，回退 Webhook: %s", data)
        except Exception as e:
            logger.warning("开放平台发消息异常，回退 Webhook: %s", e)

        if self._webhook_url():
            return self._send_via_webhook(msg_type, content)
        raise RuntimeError("飞书发消息失败，且未配置 FEISHU_WEBHOOK_URL")

    def send_group_text(self, chat_id: str, content: str) -> dict[str, Any]:
        # 有 Webhook 时，即使 chat_id 是真实 oc_，回复也优先走 Webhook（群自定义机器人）
        # 这样应用机器人未入群也能回消息；若需按 chat_id 精确回复可关掉 webhook
        if self._webhook_url() and not (settings.feishu_chat_id or "").strip():
            return self._send_via_webhook("text", {"text": content})
        return self._send_message(chat_id, "text", json.dumps({"text": content}))

    def send_group_interactive(self, chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
        if self._webhook_url() and (
            self._should_use_webhook(chat_id) or not (settings.feishu_chat_id or "").strip()
        ):
            return self._send_via_webhook("interactive", card)
        return self._send_message(chat_id, "interactive", json.dumps(card))

    def load_icris_register_form_text(self) -> str:
        path = PROJECT_ROOT / "templates" / "icris_register_form.md"
        return path.read_text(encoding="utf-8")

    def send_icris_register_form(self, chat_id: str) -> str:
        """发送 ICRIS 账号注册填写模板到群"""
        content = self.load_icris_register_form_text()
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "ICRIS 账号注册 — 填写资料"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "请复制下方模板填写后，**@机器人** 发送：\n"
                            "`/开始注册` + 整段已填写内容"
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": content[:2800],
                    },
                },
            ],
        }
        result = self.send_group_interactive(chat_id, card)
        # 卡片可能截断，再发一份纯文本便于复制
        self.send_group_text(chat_id, content)
        if result.get("code", 0) != 0:
            logger.warning("发送 ICRIS 模板卡片失败: %s", result)
        return content

    def send_material_checklist(self, chat_id: str) -> str:
        """兼容旧调用：改为发送 ICRIS 填写模板"""
        return self.send_icris_register_form(chat_id)

    def send_startup_notice(self, chat_id: str | None = None) -> None:
        """启动时向目标群发欢迎说明"""
        cid = chat_id or self.resolve_target_chat_id()
        if not cid:
            logger.warning("未配置/找到目标群，跳过启动通知")
            return
        self.send_group_text(
            cid,
            "ICRIS 账号注册机器人已上线。\n"
            "发送 /资料 获取填写模板；填完后发送 /开始注册 + 资料开始注册。\n"
            "发送 /help 查看帮助。",
        )

    # ------------------------------------------------------------------
    # 命令注册
    # ------------------------------------------------------------------

    def register_command(self, command: str, handler: Callable[[FeishuMessage], None]) -> None:
        self._command_handlers[command] = handler

    @staticmethod
    def strip_at_mentions(text: str) -> str:
        text = re.sub(r"<at[^>]*>.*?</at>", "", text or "", flags=re.I | re.S)
        text = re.sub(r"@_user_\d+", "", text)
        # 飞书偶发残留的全角空格 / 零宽字符
        text = text.replace("\u200b", "").replace("\ufeff", "").replace("\u3000", " ")
        return text.strip()

    def handle_message(self, msg: FeishuMessage) -> str | None:
        content = self.strip_at_mentions(msg.content).strip()
        # 兼容「@机器人/资料」无空格、或命令前后有多余空白
        content = re.sub(r"^[/\s]*", "/", content) if content.lstrip().startswith("/") else content
        content = content.strip()
        msg.content = content
        logger.info("[飞书] 收到消息: %s 来自 %s: %s", msg.chat_id, msg.sender_name, content[:200])

        # 优先最长命令匹配（避免 /start 抢在带正文的 /开始注册 前）
        for cmd in sorted(self._command_handlers.keys(), key=len, reverse=True):
            if content == cmd or content.startswith(cmd + " ") or content.startswith(cmd + "\n"):
                logger.info("[飞书] 匹配命令: %s", cmd)
                self._command_handlers[cmd](msg)
                return cmd
            # 正文可能在命令前（粘贴模板后写 /开始注册）— 也支持末行命令
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
            if lines and lines[-1] == cmd:
                msg.content = "\n".join(lines[:-1] + [cmd])
                logger.info("[飞书] 匹配末行命令: %s", cmd)
                self._command_handlers[cmd](msg)
                return cmd
            # 命令出现在首行（后面跟模板）
            if lines and lines[0] == cmd:
                logger.info("[飞书] 匹配首行命令: %s", cmd)
                self._command_handlers[cmd](msg)
                return cmd

        logger.info("[飞书] 未匹配任何命令: %s", content[:80])
        return None

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def start_ws_listener(self, blocking: bool = True) -> None:
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

    def list_joined_chats(self) -> list[dict[str, Any]]:
        """列出应用机器人已加入的群（用于诊断能否收到消息）"""
        if self._mock_mode:
            return [{"chat_id": "mock_chat_icris", "name": "mock"}]
        chats: list[dict[str, Any]] = []
        page_token = ""
        try:
            while True:
                params: dict[str, Any] = {"page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                with self._http() as client:
                    resp = client.get(
                        "https://open.feishu.cn/open-apis/im/v1/chats",
                        headers=self._auth_headers(),
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                if data.get("code", -1) != 0:
                    logger.warning("列出飞书群失败: %s", data)
                    break
                items = (data.get("data") or {}).get("items") or []
                chats.extend(items)
                page_token = (data.get("data") or {}).get("page_token") or ""
                if not page_token:
                    break
        except Exception:
            logger.exception("列出飞书群异常")
        return chats

    def _parse_incoming_event(self, data: Any) -> FeishuMessage | None:
        """兼容 dict / lark_oapi 事件对象"""
        event: dict[str, Any]
        if isinstance(data, dict):
            event = data.get("event") or data
        else:
            # lark_oapi P2ImMessageReceiveV1 等
            event = {}
            message = getattr(data, "message", None) or getattr(
                getattr(data, "event", None), "message", None
            )
            sender = getattr(data, "sender", None) or getattr(
                getattr(data, "event", None), "sender", None
            )
            if message is None:
                # 尝试 model 转 dict
                try:
                    if hasattr(data, "event") and data.event is not None:
                        raw = data.event
                        message = getattr(raw, "message", None)
                        sender = getattr(raw, "sender", None)
                except Exception:
                    pass
            if message is None:
                logger.debug("无法解析飞书事件结构: %s", type(data))
                return None

            msg_type = getattr(message, "message_type", "") or ""
            if msg_type and msg_type != "text":
                return None
            content_raw = getattr(message, "content", "") or ""
            try:
                content_obj = json.loads(content_raw)
                text_content = content_obj.get("text", content_raw)
            except (json.JSONDecodeError, TypeError):
                text_content = str(content_raw)

            sender_id_obj = getattr(sender, "sender_id", None) if sender else None
            open_id = ""
            if sender_id_obj is not None:
                open_id = getattr(sender_id_obj, "open_id", "") or ""
            elif isinstance(sender, dict):
                open_id = ((sender.get("sender_id") or {}).get("open_id") or "")

            return FeishuMessage(
                message_id=getattr(message, "message_id", "") or "",
                sender_id=open_id,
                sender_name=open_id or "未知",
                content=self.strip_at_mentions(text_content),
                chat_id=getattr(message, "chat_id", "") or "",
                chat_type=getattr(message, "chat_type", "group") or "group",
                raw={"type": str(type(data))},
            )

        # dict 路径
        message = event.get("message") or {}
        msg_type = message.get("message_type") or event.get("message_type") or ""
        if msg_type and msg_type != "text":
            return None
        text_content = message.get("content", "")
        try:
            content_obj = json.loads(text_content)
            text_content = content_obj.get("text", text_content)
        except (json.JSONDecodeError, TypeError):
            pass
        text_content = self.strip_at_mentions(str(text_content))
        sender = event.get("sender") or {}
        open_id = ((sender.get("sender_id") or {}).get("open_id") or "")
        return FeishuMessage(
            message_id=message.get("message_id", ""),
            sender_id=open_id,
            sender_name=open_id or "未知",
            content=text_content,
            chat_id=message.get("chat_id", ""),
            chat_type=message.get("chat_type", "group"),
            raw=data if isinstance(data, dict) else {},
        )

    def _run_ws(self) -> None:
        try:
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as WsClient
        except ImportError as e:
            raise SystemExit(
                "未安装 lark-oapi，无法接收群消息。\n"
                "请用当前解释器安装后重试:\n"
                "  python -m pip install lark-oapi\n"
                "推荐使用项目虚拟环境:\n"
                "  .\\.venv\\Scripts\\python.exe -m pip install lark-oapi\n"
                "  .\\.venv\\Scripts\\python.exe main.py feishu-bot"
            ) from e

        client_ref = self

        def _on_p2_message(data: Any) -> None:
            try:
                logger.info("[飞书] 收到原始事件: %s", type(data).__name__)
                msg = client_ref._parse_incoming_event(data)
                if msg and msg.content:
                    client_ref.handle_message(msg)
                elif msg is not None:
                    logger.info("[飞书] 事件已解析但正文为空，已忽略")
                else:
                    logger.info("[飞书] 事件无法解析为文本消息，已忽略")
            except Exception:
                logger.exception("处理飞书消息失败")

        builder = EventDispatcherHandler.builder("", "")
        if hasattr(builder, "register_p2_im_message_receive_v1"):
            event_handler = builder.register_p2_im_message_receive_v1(_on_p2_message).build()
        else:
            raise SystemExit(
                "当前 lark-oapi 版本不支持 register_p2_im_message_receive_v1，请升级:\n"
                "  python -m pip install -U lark-oapi"
            )

        ws_client = WsClient(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=event_handler,
        )
        logger.info("[飞书] WebSocket 客户端已启动，等待消息...")
        ws_client.start()
