"""企业微信外部群 Webhook 回调服务器"""

from __future__ import annotations

import logging
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from config.settings import settings
from src.wework.callback_handler import parse_external_callback_xml
from src.wework.client import WXBizMsgCrypt
from src.wework.message_router import MessageRouter

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/wework/external/callback"
WEBHOOK_PATH = "/webhook"


@dataclass
class ExternalWebhookServer:
    """HTTP 回调：URL 验证 + 客户联系事件"""

    router: MessageRouter
    host: str = "0.0.0.0"
    port: int = 8081
    _crypt: WXBizMsgCrypt | None = None

    def __post_init__(self) -> None:
        token = settings.wework_external_callback_token_resolved
        aes_key = settings.wework_external_callback_aes_key_resolved
        corp_id = settings.wework_corp_id
        if token and aes_key and corp_id:
            self._crypt = WXBizMsgCrypt(token, aes_key, corp_id)

    def start(self, *, blocking: bool = True) -> None:
        if not self._crypt:
            logger.warning(
                "外部群回调未配置 Token/AESKey，Webhook 仅 Mock 监听（无法验签解密）"
            )

        crypt = self._crypt
        router = self.router
        callback_path = CALLBACK_PATH

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug("HTTP %s", format % args)

            def _path_ok(self) -> bool:
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path == "/" or path == callback_path or path.startswith(callback_path + "/"):
                    return True
                if path == WEBHOOK_PATH or path.startswith(WEBHOOK_PATH + "/"):
                    return True
                return False

            def do_GET(self):
                if not self._path_ok():
                    self.send_response(404)
                    self.end_headers()
                    return
                if crypt is None:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"callback not configured")
                    return

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
                    logger.info("[外部群] URL 验证成功")
                except Exception as e:
                    logger.error("[外部群] URL 验证失败: %s", e)
                    self.send_response(403)
                    self.end_headers()

            def do_POST(self):
                if not self._path_ok():
                    self.send_response(404)
                    self.end_headers()
                    return

                query = parse_qs(urlparse(self.path).query)
                body_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(body_len)

                if crypt is None:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"success")
                    return

                try:
                    root = ET.fromstring(body)
                    encrypt = root.find("Encrypt")
                    if encrypt is None or encrypt.text is None:
                        raise ValueError("缺少 Encrypt")
                    xml_text = crypt.decrypt(encrypt.text)
                    evt = parse_external_callback_xml(xml_text)
                    if evt:
                        router.route_external_chat_event(evt)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"success")
                except Exception as e:
                    logger.error("[外部群] 回调处理失败: %s", e)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"success")

        server = HTTPServer((self.host, self.port), Handler)
        logger.info(
            "[外部群] Webhook 已启动 http://%s:%s%s",
            self.host,
            self.port,
            callback_path,
        )
        if blocking:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                logger.info("[外部群] Webhook 已停止")
                server.shutdown()
        else:
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
