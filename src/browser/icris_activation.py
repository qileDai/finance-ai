"""ICRIS 账号激活 — 打开邮件 s06 链接，在「启动帐户」页填用户名/密码并点确认。"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

# 官方成功页正文（简报第 17 页）；不用短词，以免命中填表页「啟動帳戶」。
_SUCCESS_KEYWORDS = [
    "successfully activated your user account",
    "你已成功啟動你的用戶帳戶",
    "您已成功啟動您的用戶帳戶",
    "你已成功启动你的用户帐户",
    "您已成功启动您的用户帐户",
    "please click 'continue' to use the functions",
    "請按「繼續」以使用電子服務網站",
    "请按「继续」以使用电子服务网站",
]

_CREDENTIAL_ERROR_RE = re.compile(
    r"incorrect user\s*id or password|"
    r"用户名称或密码不正确|用戶名稱或密碼不正確|"
    r"用户名或密码不正确|用戶名或密碼不正確|"
    r"帐号或密码不正确|帳號或密碼不正確",
    re.I,
)

_CANCELLED_USER_RE = re.compile(r"用戶名稱已取消|用户名称已取消")
_BAD_LINK_RE = re.compile(
    r"超連結|超链接|連結不正確|连结不正确|链接不正确|連結不正确"
)

_CONFIRM_NAME_RE = re.compile(r"^(Confirm|确认|確認)$", re.I)


def normalize_s06_activation_url(url: str) -> str:
    """邮件里的 ICRIS3EP s06 会落到 404；启动帐户页在 ICRIS3EF。"""
    raw = (url or "").strip()
    if not raw:
        return raw
    return re.sub(r"(?i)/ICRIS3EP/", "/ICRIS3EF/", raw)


def s06_credential_error(body: str) -> str:
    """启动帐户页凭证错误文案；无则空串。"""
    text = body or ""
    if not _CREDENTIAL_ERROR_RE.search(text):
        return ""
    if re.search(r"locked after\s*5|5\s*次.*锁定|5\s*次.*鎖定|5 unsuccessful", text, re.I):
        return "用户名或密码不正确（连续 5 次错误会锁帐户）"
    return "用户名或密码不正确"


def s06_fail_banner(body: str) -> str:
    """启动帐户红条失败（用户已取消、链接失效）；无则空串。"""
    text = body or ""
    if _CANCELLED_USER_RE.search(text):
        return "用户名称已取消"
    if _BAD_LINK_RE.search(text):
        return "激活链接不正确"
    return ""


async def _page_body(page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _has_visible_password(page) -> bool:
    try:
        inp = page.locator("input[type='password']").first
        return await inp.count() > 0 and await inp.is_visible()
    except Exception:
        return False


async def _is_s06_activation_form(page) -> bool:
    if not await _has_visible_password(page):
        return False
    user = page.locator("#userId").first
    if await user.count() > 0 and await user.is_visible():
        return True
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    return any(
        k in body
        for k in ("account activation", "启动帐户", "啟動帳戶", "启动账户", "啟動賬戶")
    )


async def _page_looks_activated(page, *, after_submit: bool = False) -> bool:
    """仅官方成功页文案。填表页、失败红条不算。"""
    del after_submit
    try:
        body_raw = await page.inner_text("body")
    except Exception:
        body_raw = ""
    body_text = (body_raw or "").lower()
    if s06_credential_error(body_raw) or s06_fail_banner(body_raw):
        return False
    return any(k.lower() in body_text for k in _SUCCESS_KEYWORDS)


async def _wait_activation_result(page, timeout_ms: int = 45000) -> tuple[str, str]:
    """点确认后等到成功、凭证错误或超时。返回 (success|credential_error|timeout, detail)。"""
    from src.browser.icris_ui_common import wait_spin_clear

    deadline = time.monotonic() + max(0.05, timeout_ms / 1000.0)
    while True:
        try:
            await wait_spin_clear(page, timeout_ms=5000)
        except Exception:
            pass
        url_now = str(getattr(page, "url", "") or "")
        logger.info("等待激活结果 url=%s", url_now[:160])
        body = await _page_body(page)
        cred_err = s06_credential_error(body)
        if cred_err:
            logger.error("启动帐户失败: %s", cred_err)
            return "credential_error", cred_err
        banner = s06_fail_banner(body)
        if banner:
            logger.error("启动帐户失败: %s", banner)
            return "page_error", banner
        if await _page_looks_activated(page, after_submit=True):
            logger.info("启动帐户已成功 url=%s", url_now[:160])
            return "success", ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait_ms = min(1000, max(50, int(remaining * 1000)))
        try:
            await page.wait_for_timeout(wait_ms)
        except Exception:
            import asyncio

            await asyncio.sleep(wait_ms / 1000.0)
    url_now = str(getattr(page, "url", "") or "")
    detail = f"等待激活超时，仍停在: {url_now[:120] or '(未知)'}"
    logger.warning("%s", detail)
    return "timeout", detail


async def _fill_input(page, selectors: list[str], value: str) -> bool:
    if not value:
        return False
    for sel in selectors:
        inp = page.locator(sel).first
        if await inp.count() == 0:
            continue
        try:
            if not await inp.is_visible():
                continue
        except Exception:
            pass
        try:
            await inp.scroll_into_view_if_needed()
            await inp.click()
            await inp.fill(value)
            await inp.dispatch_event("input")
            await inp.dispatch_event("change")
            actual = await inp.input_value()
            if actual == value:
                logger.info("已填写 %s", sel)
                return True
        except Exception:
            continue
    return False


async def _click_s06_confirm(page) -> bool:
    """点启动帐户页红色「确认 / Confirm」，不是门户登入。"""
    btn = page.get_by_role("button", name=_CONFIRM_NAME_RE).first
    if await btn.count() > 0:
        try:
            await btn.scroll_into_view_if_needed()
            await btn.click(timeout=15000)
            logger.info("已点击启动帐户确认")
            return True
        except Exception:
            try:
                await btn.click(timeout=8000, force=True)
                logger.info("已强制点击启动帐户确认")
                return True
            except Exception:
                pass

    clicked = await page.evaluate(
        """() => {
            const compact = s => (s || '').replace(/\\s+/g, '');
            const labelOf = el => compact(
                el.innerText || el.value || el.getAttribute('aria-label')
                || el.getAttribute('title') || ''
            );
            const isConfirm = t => /^(確認|确认|Confirm)$/i.test(t);
            const all = [...document.querySelectorAll(
                'button, input[type=submit], input[type=button], a[role=button]'
            )];
            for (const el of all) {
                if (!isConfirm(labelOf(el))) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                el.scrollIntoView({ block: 'center' });
                el.click();
                return true;
            }
            return false;
        }"""
    )
    if clicked:
        logger.info("已点击启动帐户确认（evaluate）")
        return True
    logger.warning("未找到启动帐户确认按钮")
    return False


async def fill_s06_activation_form(page, username: str, password: str) -> None:
    """按启动帐户页：#userId、#password，再点确认。"""
    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user or not pwd:
        raise ValueError("启动帐户缺少用户名或密码")

    ok_user = await _fill_input(
        page,
        [
            "#userId",
            "input[placeholder*='User ID' i]",
            "input[placeholder*='用户名称']",
            "input[placeholder*='用戶名稱']",
        ],
        user,
    )
    ok_pwd = await _fill_input(
        page,
        ["#password", "input[type='password']"],
        pwd,
    )
    if not ok_user or not ok_pwd:
        raise RuntimeError(
            f"启动帐户字段未填上 user={ok_user} password={ok_pwd}"
        )
    if not await _click_s06_confirm(page):
        raise RuntimeError("未点到启动帐户确认按钮")


def require_s06_activation_url(activation_url: str) -> tuple[str, str]:
    """规范化并校验 s06 链接。返回 (url, error)；error 非空则不要打开。"""
    from src.email.imap_client import is_activation_url

    raw = (activation_url or "").strip()
    open_url = normalize_s06_activation_url(raw)
    if is_activation_url(open_url):
        return open_url, ""
    shown = open_url or raw or "(空)"
    return open_url, f"不是启动帐户链接: {shown[:120]}"


async def activate_icris_account(
    activation_url: str,
    username: str = "",
    password: str = "",
) -> tuple[bool, str]:
    """打开邮件 s06 启动帐户页，填账号密码并点确认。探测与自动激活共用。

    命中官方成功页后只截图返回，不点 Continue / 繼續。
    返回: (成功与否, 截图路径或错误消息)
    """
    open_url, url_err = require_s06_activation_url(activation_url)
    if url_err:
        logger.error("%s", url_err)
        return (False, url_err)

    try:
        from src.browser.launcher import (
            import_async_playwright,
            launch_browser,
            create_browser_context,
        )
    except ImportError as e:
        raise RuntimeError(
            "请先安装 Playwright: pip install playwright && playwright install chromium"
        ) from e

    from src.browser.cdp_lock import cdp_lock_held_here
    from src.browser.cdp_session import hold_cdp_lock
    from src.browser.icris_ui_common import wait_spin_clear
    from contextlib import nullcontext

    user = (username or "").strip()
    pwd = (password or "").strip()

    async_playwright = import_async_playwright()
    shot_dir = PROJECT_ROOT / "data" / "icris_activations"
    shot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shot_file = shot_dir / f"activation_{stamp}.png"

    lock_ctx = (
        nullcontext() if cdp_lock_held_here() else hold_cdp_lock("activation")
    )
    with lock_ctx:
        async with async_playwright() as p:
            browser = await launch_browser(p, force_isolated=False)
            context = await create_browser_context(browser)
            page = await context.new_page()
            try:
                logger.info("打开激活链接: %s", open_url[:80])
                await page.goto(open_url, wait_until="domcontentloaded", timeout=60000)
                await wait_spin_clear(page, timeout_ms=20000)
                await page.wait_for_timeout(1000)

                is_success = await _page_looks_activated(page)
                if not is_success and await _is_s06_activation_form(page):
                    if not user or not pwd:
                        await page.screenshot(path=str(shot_file), full_page=True)
                        return (False, f"启动帐户页需要用户名和密码 | {open_url}")
                    logger.info("启动帐户页填表，账号: %s", user)
                    await fill_s06_activation_form(page, user, pwd)
                    status, wait_detail = await _wait_activation_result(
                        page, timeout_ms=45000
                    )
                    if status != "success":
                        await page.screenshot(path=str(shot_file), full_page=True)
                        logger.warning("%s", wait_detail)
                        return (False, f"{wait_detail} | {open_url}")
                    is_success = True

                await page.screenshot(path=str(shot_file), full_page=True)
                logger.info("激活页面截图: %s (success=%s)", shot_file, is_success)
                if is_success:
                    logger.info("激活成功，停在成功页，不点繼續")
                    return (True, f"{shot_file} | {open_url}")
                if not await _is_s06_activation_form(page):
                    return (
                        False,
                        f"打开后不是启动帐户页: {page.url[:120]}",
                    )
                return (False, f"{shot_file} | {open_url}")
            except Exception as e:
                logger.error("激活流程异常: %s", e)
                try:
                    if not page.is_closed():
                        await page.screenshot(path=str(shot_file), full_page=True)
                except Exception:
                    pass
                return (False, f"{e} | {open_url}")
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
