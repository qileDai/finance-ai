"""独立探测：拉邮箱找 ICRIS 激活信并打开 s06 启动帐户。不改任务状态、不挂钩 Worker。"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _result(
    *,
    ok: bool,
    found: bool,
    password_source: str,
    detail: str,
    url: str = "",
) -> dict[str, Any]:
    return {
        "ok": ok,
        "found": found,
        "password_source": password_source,
        "detail": detail,
        "url": url,
    }


def resolve_probe_password(
    store: Any,
    *,
    username: str,
    password: str = "",
) -> tuple[str, str]:
    """手填优先；否则只读查任务。返回 (密码, source=manual|job|none)。"""
    filled = (password or "").strip()
    if filled:
        return filled, "manual"
    looked = ""
    finder = getattr(store, "find_icris_password_by_username", None)
    if callable(finder):
        looked = str(finder(username) or "").strip()
    if looked:
        return looked, "job"
    return "", "none"


def _run_activate(url: str, username: str, password: str) -> tuple[bool, str]:
    from src.browser.icris_activation import activate_icris_account

    try:
        return asyncio.run(
            activate_icris_account(url, username=username, password=password)
        )
    except RuntimeError:
        result: tuple[bool, str] = (False, "unknown")

        def _run() -> None:
            nonlocal result
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    activate_icris_account(
                        url, username=username, password=password
                    )
                )
            finally:
                loop.close()

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=180)
        return result


def run_icris_activation_probe(
    store: Any,
    *,
    username: str,
    email: str,
    password: str = "",
    fetch_link: Callable[..., str | None] | None = None,
    activate: Callable[[str, str, str], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """拉「用戶登記及啟動」信，打开 s06 启动帐户并填表确认。不写 job 表。"""
    from src.browser.icris_activation import (
        normalize_s06_activation_url,
        require_s06_activation_url,
    )
    from src.email.imap_client import format_imap_connect_error

    user = (username or "").strip()
    addr = (email or "").strip()
    if not user or not addr:
        return _result(
            ok=False,
            found=False,
            password_source="none",
            detail="账号和邮箱必填",
        )

    account = store.get_email_account_by_address(addr)
    if not account:
        src = "manual" if (password or "").strip() else "none"
        return _result(
            ok=False,
            found=False,
            password_source=src,
            detail=f"邮箱未配置 IMAP: {addr}",
        )

    pwd, pwd_source = resolve_probe_password(
        store, username=user, password=password
    )

    found_subject = False
    if fetch_link is None:
        from src.email.imap_client import EmailClient

        imap_user = str(account.get("username") or "")
        imap_pwd = str(account.get("password") or "")
        host = str(account.get("imap_host") or "")
        if not host or not imap_user or not imap_pwd:
            return _result(
                ok=False,
                found=False,
                password_source=pwd_source,
                detail="邮箱账号配置不完整",
            )
        try:
            info = EmailClient().fetch_activation_result(
                account, expected_username=user
            )
        except Exception as e:
            return _result(
                ok=False,
                found=False,
                password_source=pwd_source,
                detail=format_imap_connect_error(e),
            )
        link = str(info.get("url") or "").strip() or None
        found_subject = bool(info.get("found_subject"))
        if not link:
            return _result(
                ok=False,
                found=found_subject,
                password_source=pwd_source,
                detail=str(info.get("detail") or f"暂无匹配 {user} 的激活邮件"),
            )
    else:
        link = fetch_link(account, expected_username=user)

    if not link:
        return _result(
            ok=False,
            found=found_subject,
            password_source=pwd_source,
            detail=f"暂无匹配 {user} 的激活邮件",
        )

    open_url, url_err = require_s06_activation_url(str(link))
    if url_err:
        return _result(
            ok=False,
            found=True,
            password_source=pwd_source,
            detail=url_err,
            url=open_url or str(link),
        )
    open_url = open_url or normalize_s06_activation_url(str(link))
    logger.info(
        "探测即将打开 邮箱=%s 期望用户名=%s url=%s",
        addr,
        user,
        open_url,
    )

    do_activate = activate or (
        lambda url, u, p: _run_activate(url, u, p)
    )
    try:
        ok, detail = do_activate(open_url, user, pwd)
    except Exception as e:
        logger.exception("探测激活异常")
        return _result(
            ok=False,
            found=True,
            password_source=pwd_source,
            detail=str(e),
            url=open_url,
        )
    note = str(detail or ("激活成功" if ok else "激活失败"))
    if open_url and open_url not in note:
        note = f"{note} | {open_url}"
    if ok:
        return _result(
            ok=True,
            found=True,
            password_source=pwd_source,
            detail=note,
            url=open_url,
        )
    return _result(
        ok=False,
        found=True,
        password_source=pwd_source,
        detail=note,
        url=open_url,
    )
