"""邮箱读取 - 获取 ICRIS 注册账号"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class IcrisAccount:
    username: str
    password: str
    raw_subject: str = ""


class EmailClient:
    """IMAP 邮箱客户端，用于读取 ICRIS 账号邮件"""

    def __init__(self) -> None:
        self.host = settings.email_imap_host
        self.port = settings.email_imap_port
        self.address = settings.email_address
        self.password = settings.email_password
        self._mock_mode = not settings.email_configured

    def _decode_header_value(self, value: str) -> str:
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return "".join(decoded)

    def _parse_icris_credentials(self, body: str, subject: str) -> IcrisAccount | None:
        """从邮件正文解析 ICRIS 账号密码"""
        patterns = [
            r"(?:User\s*(?:Name|ID|name)?|Username|Login\s*ID|用戶名稱|用户名)[:\s]+(\S+)",
            r"(?:Password|密碼|密码)[:\s]+(\S+)",
        ]
        username_match = re.search(patterns[0], body, re.IGNORECASE)
        password_match = re.search(patterns[1], body, re.IGNORECASE)

        if username_match and password_match:
            return IcrisAccount(
                username=username_match.group(1).strip(),
                password=password_match.group(1).strip(),
                raw_subject=subject,
            )

        # 备用：尝试 JSON 格式
        json_user = re.search(r'"username"\s*:\s*"([^"]+)"', body)
        json_pass = re.search(r'"password"\s*:\s*"([^"]+)"', body)
        if json_user and json_pass:
            return IcrisAccount(
                username=json_user.group(1),
                password=json_pass.group(1),
                raw_subject=subject,
            )
        return None

    def fetch_icris_account(self, mock_account: IcrisAccount | None = None) -> IcrisAccount:
        """读取最新 ICRIS 账号邮件"""
        if self._mock_mode:
            logger.info("邮箱未配置，使用 Mock 账号")
            return mock_account or IcrisAccount(
                username="MOCK_ICRIS_USER",
                password="MockPass123!",
                raw_subject="[Mock] ICRIS Account Registration",
            )

        mail = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            mail.login(self.address, self.password)
            mail.select("INBOX")

            _, message_numbers = mail.search(None, '(SUBJECT "ICRIS" OR SUBJECT "Companies Registry")')
            ids = message_numbers[0].split()
            if not ids:
                raise RuntimeError("未找到 ICRIS 相关邮件")

            latest_id = ids[-1]
            _, msg_data = mail.fetch(latest_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject = self._decode_header_value(msg.get("Subject", ""))
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode("utf-8", errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")

            account = self._parse_icris_credentials(body, subject)
            if not account:
                raise RuntimeError(f"无法从邮件解析账号: {subject}")
            logger.info("已读取 ICRIS 账号: %s", account.username)
            return account
        finally:
            mail.logout()
