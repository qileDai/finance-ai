"""企业微信 API 客户端（消息收发 + Webhook 接收 + Mock 模式）"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx

try:
    from Crypto.Cipher import AES
    _CRYPTO_AVAILABLE = True
except ImportError:
    AES = None  # type: ignore
    _CRYPTO_AVAILABLE = False

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class MockMessage:
    chat_id: str
    sender: str
    content: str
    msg_type: str = "text"


@dataclass
class WeWorkMessage:
    """企业微信收到的消息"""
    msg_id: str
    sender_id: str       # 发送者 UserID
    sender_name: str     # 发送者名称
    content: str         # 消息文本
    chat_id: str         # 群聊 ID（群消息时有值）
    chat_type: str       # single / group
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 企微消息加解密
# ---------------------------------------------------------------------------

class WXBizMsgCrypt:
    """企业微信消息加解密"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self.corp_id = corp_id
        self.key = base64.b64decode(encoding_aes_key + "=")

    def _pkcs7_unpad(self, data: bytes) -> bytes:
        pad_len = data[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError("PKCS7 padding 错误")
        return data[:-pad_len]

    def decrypt(self, encrypt_text: str) -> str:
        """解密消息"""
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError(
                "解密需要 pycryptodome，请执行: pip install pycryptodome"
            )
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt_text))
        plain = self._pkcs7_unpad(plain)

        # 格式: random(16) + msg_len(4) + msg + corp_id
        content = plain[16:]
        msg_len = struct.unpack(">I", content[:4])[0]
        msg = content[4:4 + msg_len].decode("utf-8")

        # 验证 corp_id
        received_corp_id = content[4 + msg_len:].decode("utf-8")
        if received_corp_id != self.corp_id:
            raise ValueError(f"corp_id 不匹配: {received_corp_id} != {self.corp_id}")

        return msg

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """验证回调 URL"""
        sort_list = sorted([self.token, timestamp, nonce, echostr])
        sha1 = hashlib.sha1("".join(sort_list).encode()).hexdigest()
        if sha1 != msg_signature:
            raise ValueError("签名验证失败")
        return self.decrypt(echostr)


# ---------------------------------------------------------------------------
# 企业微信客户端
# ---------------------------------------------------------------------------

@dataclass
class WeWorkClient:
    """企业微信消息收发客户端"""

    corp_id: str = ""
    corp_secret: str = ""
    agent_id: str = ""
    token: str = ""
    encoding_aes_key: str = ""
    _access_token: str = ""
    _token_expires: float = 0
    _mock_messages: list[MockMessage] = field(default_factory=list)
    _mock_mode: bool = False
    _command_handlers: dict[str, Callable] = field(default_factory=dict)
    _crypt: WXBizMsgCrypt | None = None

    def __post_init__(self) -> None:
        self.corp_id = self.corp_id or settings.wework_corp_id
        self.corp_secret = self.corp_secret or settings.wework_corp_secret
        self.agent_id = self.agent_id or settings.wework_agent_id
        self.token = self.token or settings.wework_token
        self.encoding_aes_key = self.encoding_aes_key or settings.wework_encoding_aes_key
        self._mock_mode = not settings.wework_configured

        if self._mock_mode:
            logger.info("企业微信未配置，使用 Mock 模式")
        else:
            if settings.wework_webhook_configured:
                self._crypt = WXBizMsgCrypt(self.token, self.encoding_aes_key, self.corp_id)
                logger.info("企业微信已配置 (corp_id=%s, agent_id=%s), Webhook 已就绪", self.corp_id[:8] + "***", self.agent_id)
            else:
                logger.info("企业微信已配置 (corp_id=%s, agent_id=%s), Webhook 未配置(Token/EncodingAESKey)", self.corp_id[:8] + "***", self.agent_id)

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

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
        logger.info("企业微信 access_token 已获取")
        return self._access_token

    # ------------------------------------------------------------------
    # 发送消息
    # ------------------------------------------------------------------

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
        """发送消息到群聊"""
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

    # ------------------------------------------------------------------
    # 命令注册
    # ------------------------------------------------------------------

    def register_command(self, command: str, handler: Callable[[WeWorkMessage], None]) -> None:
        """注册命令处理器"""
        self._command_handlers[command] = handler

    def handle_message(self, msg: WeWorkMessage) -> str | None:
        """处理收到的消息，匹配命令"""
        content = msg.content.strip()
        logger.info("[企微] 收到消息: %s 来自 %s(%s): %s", msg.chat_id, msg.sender_name, msg.sender_id, content)

        for cmd, handler in self._command_handlers.items():
            if content.startswith(cmd):
                logger.info("[企微] 匹配命令: %s", cmd)
                handler(msg)
                return cmd

        logger.info("[企微] 未匹配任何命令: %s", content)
        return None

    # ------------------------------------------------------------------
    # Webhook 回调服务器（接收消息）
    # ------------------------------------------------------------------

    def start_webhook_server(self, host: str = "0.0.0.0", port: int = 8080, blocking: bool = True) -> None:
        """启动 HTTP 回调服务器，接收企业微信消息"""
        if self._mock_mode:
            logger.info("[Mock 企微] Webhook 模拟启动（不会真实监听）")
            if blocking:
                logger.info("[Mock 企微] 模拟运行中，按 Ctrl+C 退出...")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    logger.info("[Mock 企微] 模拟停止")
            return

        crypt = self._crypt
        handle_msg = self.handle_message

        class WeWorkHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug("HTTP %s", format % args)

            def do_GET(self):
                """URL 验证"""
                from urllib.parse import parse_qs, urlparse
                query = parse_qs(urlparse(self.path).query)
                msg_sig = query.get("msg_signature", [""])[0]
                timestamp = query.get("timestamp", [""])[0]
                nonce = query.get("nonce", [""])[0]
                echostr = query.get("echostr", [""])[0]

                try:
                    plain = crypt.verify_url(msg_sig, timestamp, nonce, echostr)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(plain.encode())
                    logger.info("[企微] URL 验证成功")
                except Exception as e:
                    logger.error("[企微] URL 验证失败: %s", e)
                    self.send_response(403)
                    self.end_headers()

            def do_POST(self):
                """接收消息"""
                from urllib.parse import parse_qs, urlparse
                query = parse_qs(urlparse(self.path).query)
                msg_sig = query.get("msg_signature", [""])[0]
                timestamp = query.get("timestamp", [""])[0]
                nonce = query.get("nonce", [""])[0]

                body_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(body_len)

                try:
                    # 解析加密 XML
                    root = ET.fromstring(body)
                    encrypt = root.find("Encrypt")
                    if encrypt is None or encrypt.text is None:
                        raise ValueError("消息中缺少 Encrypt 字段")

                    # 解密消息
                    xml_text = crypt.decrypt(encrypt.text)
                    msg_root = ET.fromstring(xml_text)

                    # 提取字段
                    msg_type = msg_root.find("MsgType")
                    msg_type = msg_type.text if msg_type is not None else ""

                    if msg_type != "text":
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"ok")
                        return

                    content = msg_root.find("Content")
                    content = content.text if content is not None else ""

                    msg_id = msg_root.find("MsgId")
                    msg_id = msg_id.text if msg_id is not None else ""

                    sender_id = msg_root.find("FromUserName")
                    sender_id = sender_id.text if sender_id is not None else ""

                    # 群聊 ID（ChatId）
                    chat_id = msg_root.find("ChatId")
                    chat_id = chat_id.text if chat_id is not None else sender_id

                    chat_type = msg_root.find("ChatType")
                    chat_type = chat_type.text if chat_type is not None else "single"

                    msg = WeWorkMessage(
                        msg_id=msg_id,
                        sender_id=sender_id,
                        sender_name=sender_id,
                        content=content,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        raw={"xml": xml_text},
                    )
                    handle_msg(msg)

                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception as e:
                    logger.error("[企微] 消息处理失败: %s", e)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")

        server = HTTPServer((host, port), WeWorkHandler)
        logger.info("[企微] Webhook 服务器已启动 http://%s:%s", host, port)
        if blocking:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                logger.info("[企微] Webhook 服务器已停止")
                server.shutdown()
        else:
            import threading
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            logger.info("[企微] Webhook 服务器后台运行中")