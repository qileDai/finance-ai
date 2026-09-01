"""邮箱读取 - 获取 ICRIS 注册账号"""

from __future__ import annotations

import email
import html as html_lib
import imaplib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header
from email.message import Message
from urllib.parse import unquote

from config.settings import settings

logger = logging.getLogger(__name__)

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_ACTIVATION_URL_KEYWORDS = (
    "activate",
    "confirm",
    "verify",
    "activation",
    "啟用",
    "启用",
    "激活",
)
_SUBJECT_SEARCH_TERMS = (
    "ICRIS",
    "e-Services",
    "Companies Registry",
    "activate",
    "activation",
    "confirm",
    "verification",
)
_FROM_SEARCH_TERMS = ("cr.gov.hk", "e-services")


@dataclass
class IcrisAccount:
    username: str
    password: str
    raw_subject: str = ""


def _decode_part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "gbk", "gb18030"):
        try:
            return payload.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def collect_message_body(msg: Message) -> str:
    """拼接纯文本与 HTML 正文，便于抽激活链接。"""
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                chunks.append(_decode_part_payload(part))
    else:
        chunks.append(_decode_part_payload(msg))
    return "\n".join(chunks)


def is_activation_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw.lower().startswith("http"):
        return False
    lower = raw.lower()
    if any(k in lower for k in _ACTIVATION_URL_KEYWORDS):
        return True
    if "e-services.cr.gov.hk" in lower:
        path = lower.split("e-services.cr.gov.hk", 1)[-1]
        if "?" in raw or (path.startswith("/") and len(path) > 2):
            return True
    return False


def extract_activation_link_from_body(body: str) -> str | None:
    """从纯文本/HTML 正文提取 ICRIS 激活链接。"""
    if not body:
        return None
    text = html_lib.unescape(body)
    candidates: list[str] = []
    for match in _HREF_RE.finditer(text):
        candidates.append(match.group(1))
    for match in _URL_RE.finditer(text):
        candidates.append(match.group(0))
    for raw in candidates:
        url = unquote(raw.rstrip(".,;)"))
        if is_activation_url(url):
            return url
    return None


def imap_search_queries(since_str: str) -> list[tuple[str | None, str]]:
    """合法的 IMAP SEARCH 列表（每次一条 SUBJECT/FROM，避免非法 OR）。"""
    queries: list[tuple[str | None, str]] = []
    for term in _SUBJECT_SEARCH_TERMS:
        queries.append((None, f'(SINCE {since_str} SUBJECT "{term}")'))
    for term in _FROM_SEARCH_TERMS:
        queries.append((None, f'(SINCE {since_str} FROM "{term}")'))
    return queries


def _imap_response_text(data: object) -> str:
    if data is None:
        return ""
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    if isinstance(data, (list, tuple)):
        parts = [_imap_response_text(x) for x in data if x is not None]
        return " ".join(p for p in parts if p)
    return str(data)


def _send_imap_id(mail: imaplib.IMAP4, contact: str = "") -> None:
    """网易 163/QQ：登录前（NONAUTH）必须发 IMAP ID，否则 SELECT 常停在 AUTH。"""
    email_addr = (contact or "").replace("\\", "").replace('"', "")
    payload = (
        '("name" "finance-ai" "contact" "%s" "version" "1.0" "vendor" "finance-ai")'
        % (email_addr or "finance-ai@local")
    )
    try:
        imaplib.Commands["ID"] = ("AUTH", "SELECTED", "NONAUTH")
        typ, dat = mail._simple_command("ID", payload)
        try:
            mail._untagged_response(typ, dat, "ID")
        except TypeError:
            # 旧签名 _untagged_response(name)
            mail._untagged_response("ID")  # type: ignore[misc]
        except Exception:
            pass
    except Exception as e:
        logger.debug("IMAP ID 发送失败（可忽略）: %s", e)


def format_imap_connect_error(exc: BaseException) -> str:
    """把 IMAP 测试/登录失败译成可读原因。"""
    raw = str(exc) or exc.__class__.__name__
    lower = raw.lower()
    if "search illegal in state auth" in lower:
        return (
            "收件箱未打开（仍停在 AUTH）。163 需在登录前发送 IMAP ID；"
            "请确认已开启 IMAP 且使用授权码。"
            f" 原始错误: {raw}"
        )
    if "unsafe login" in lower:
        return (
            "163 拒绝打开收件箱（Unsafe Login）。请在网页邮箱开启 IMAP，"
            "使用授权码而非登录密码。"
            f" 原始错误: {raw}"
        )
    if any(
        k in lower
        for k in ("authenticationfailed", "invalid credentials", "login fail", "auth fail")
    ):
        return f"登录失败，请检查账号和授权码。原始错误: {raw}"
    if "timed out" in lower or "timeout" in lower:
        return f"连接超时，无法连上 IMAP 服务器。原始错误: {raw}"
    if raw.startswith("无法打开收件箱"):
        return raw
    return f"连接失败: {raw}"


def _folder_names_from_list(folders: object) -> list[str]:
    names: list[str] = []
    if not folders:
        return names
    rows = folders if isinstance(folders, (list, tuple)) else [folders]
    for raw in rows:
        if not raw:
            continue
        line = (
            raw.decode("utf-8", errors="replace")
            if isinstance(raw, (bytes, bytearray))
            else str(raw)
        )
        quoted = re.findall(r'"([^"]+)"', line)
        if quoted:
            names.append(quoted[-1])
        else:
            token = line.split()[-1].strip() if line.split() else ""
            if token:
                names.append(token.strip('"'))
    return names


def _try_select(mail: imaplib.IMAP4, mailbox: str) -> tuple[bool, str]:
    try:
        typ, data = mail.select(mailbox)
    except Exception as e:
        return False, str(e)
    state = str(getattr(mail, "state", "") or "")
    if str(typ or "").upper() == "OK" and state == "SELECTED":
        return True, ""
    err = _imap_response_text(data) or str(typ)
    if state and state != "SELECTED":
        err = f"{err} (state={state})".strip()
    return False, err


def select_imap_inbox(mail: imaplib.IMAP4) -> None:
    """选中收件箱；失败则抛出，避免在 AUTH 状态下 SEARCH。"""
    last_err = ""
    for name in ("INBOX", '"INBOX"', "收件箱"):
        ok, err = _try_select(mail, name)
        if ok:
            return
        last_err = err or last_err
    try:
        typ, folders = mail.list()
    except Exception:
        typ, folders = "NO", None
    if str(typ or "").upper() == "OK":
        wanted = {"inbox", "收件箱"}
        for folder in _folder_names_from_list(folders):
            if folder.lower() in wanted or folder in ("INBOX", "收件箱"):
                ok, err = _try_select(mail, folder)
                if ok:
                    return
                last_err = err or last_err
    raise RuntimeError(f"无法打开收件箱: {last_err or 'SELECT failed'}")


def login_and_select_inbox(
    mail: imaplib.IMAP4, username: str, password: str
) -> None:
    _send_imap_id(mail, username)
    mail.login(username, password)
    _send_imap_id(mail, username)
    select_imap_inbox(mail)
    if str(getattr(mail, "state", "") or "") != "SELECTED":
        raise RuntimeError(
            f"无法打开收件箱: IMAP 仍停在 {getattr(mail, 'state', '') or 'AUTH'}"
        )


def open_imap_inbox(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    connect: Callable[[], imaplib.IMAP4] | None = None,
) -> imaplib.IMAP4:
    """SSL 登录并选中收件箱。connect 可注入假连接（单测用）。"""
    opener = connect or (
        lambda: imaplib.IMAP4_SSL(host, int(port), timeout=15)
    )
    mail = opener()
    try:
        login_and_select_inbox(mail, username, password)
    except Exception:
        try:
            mail.logout()
        except Exception:
            pass
        raise
    return mail


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

        mail = open_imap_inbox(self.host, self.port, self.address, self.password)
        try:
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

    def _search_message_ids(self, mail: imaplib.IMAP4_SSL, since_str: str) -> list[bytes]:
        seen: set[bytes] = set()
        ids: list[bytes] = []
        for charset, criteria in imap_search_queries(since_str):
            try:
                typ, data = mail.search(charset, criteria)
            except Exception as e:
                logger.debug("IMAP search 失败 %s: %s", criteria, e)
                continue
            if typ != "OK" or not data or not data[0]:
                continue
            for mid in data[0].split():
                if mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
        if ids:
            return ids
        try:
            typ, data = mail.search(None, f"(SINCE {since_str})")
            if typ == "OK" and data and data[0]:
                return data[0].split()[-50:]
        except Exception as e:
            logger.debug("IMAP SINCE 回退搜索失败: %s", e)
        return []

    def fetch_activation_link(
        self, account: dict, since_date: datetime | None = None
    ) -> str | None:
        """登录指定 IMAP 邮箱，搜索 ICRIS 确认邮件，提取激活链接。

        account: {email_address, imap_host, imap_port, username, password}
        since_date: 只搜索此日期之后的邮件（默认 7 天前）
        返回: 激活 URL 或 None
        """
        host = str(account.get("imap_host") or "")
        port = int(account.get("imap_port") or 993)
        username = str(account.get("username") or "")
        password = str(account.get("password") or "")
        if not host or not username or not password:
            logger.warning("邮箱账号配置不完整，跳过")
            return None

        since = since_date or (datetime.now() - timedelta(days=7))
        since_str = since.strftime("%d-%b-%Y")

        mail = None
        try:
            mail = open_imap_inbox(host, port, username, password)
            ids = self._search_message_ids(mail, since_str)
            if not ids:
                logger.info("未找到 ICRIS 激活邮件: %s", username)
                return None

            for mid in reversed(ids):
                _, msg_data = mail.fetch(mid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                body = collect_message_body(msg)
                link = extract_activation_link_from_body(body)
                if link:
                    logger.info("找到激活链接: %s", link[:80])
                    return link
            logger.info("邮件中未找到激活链接: %s", username)
            return None
        except Exception as e:
            logger.error("读取激活邮件失败 %s: %s", username, e)
            return None
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
