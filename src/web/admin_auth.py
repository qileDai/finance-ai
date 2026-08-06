"""管理后台 Cookie 会话（HMAC 签名）。"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import parse_qs

from config.settings import settings

COOKIE_NAME = "admin_session"


def _session_secret() -> str:
    custom = (getattr(settings, "admin_session_secret", None) or "").strip()
    if custom:
        return custom
    pwd = (settings.admin_password or "").strip()
    if not pwd:
        return ""
    # 派生：避免把明文密码直接当 HMAC key 写进文档；仍依赖密码变更即失效
    return hashlib.sha256(f"finance-ai-admin|{pwd}".encode("utf-8")).hexdigest()


def session_hours() -> float:
    try:
        h = float(getattr(settings, "admin_session_hours", 12) or 12)
    except (TypeError, ValueError):
        h = 12.0
    return max(1.0, min(h, 24 * 30))


def expected_username() -> str:
    return (settings.admin_username or "admin").strip() or "admin"


def password_configured() -> bool:
    return bool((settings.admin_password or "").strip())


def verify_credentials(username: str, password: str) -> tuple[bool, str]:
    """返回 (ok, error_message)。"""
    if not password_configured():
        return False, "Admin disabled: set ADMIN_PASSWORD in .env"
    expect = expected_username()
    if (username or "").strip() != expect:
        return False, "用户名或密码错误"
    if (password or "") != (settings.admin_password or "").strip():
        return False, "用户名或密码错误"
    return True, ""


def issue_session_token(username: str | None = None) -> str:
    user = (username or expected_username()).strip() or expected_username()
    secret = _session_secret()
    if not secret:
        raise RuntimeError("ADMIN_PASSWORD not set")
    exp = int(time.time()) + int(session_hours() * 3600)
    payload = f"{user}|{exp}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}|{sig}"


def parse_session_token(token: str) -> str | None:
    """校验 token，成功返回 username。"""
    secret = _session_secret()
    if not secret or not token:
        return None
    parts = token.strip().split("|")
    if len(parts) != 3:
        return None
    user, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    if user != expected_username():
        return None
    payload = f"{user}|{exp_s}"
    expect_sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expect_sig, sig):
        return None
    return user


def parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def username_from_request(headers: Any) -> str | None:
    cookies = parse_cookies(headers.get("Cookie") or "")
    return parse_session_token(cookies.get(COOKIE_NAME, ""))


def session_cookie_header(token: str) -> str:
    max_age = int(session_hours() * 3600)
    return (
        f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
    )


def clear_session_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def read_json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        import json

        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # form fallback
        try:
            qs = parse_qs(raw.decode("utf-8", errors="replace"))
            return {k: (v[0] if v else "") for k, v in qs.items()}
        except Exception:
            return {}
