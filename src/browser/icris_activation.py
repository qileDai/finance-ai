"""ICRIS 账号激活 — 打开邮件激活链接，必要时用入库账号密码登录。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

_SUCCESS_KEYWORDS = [
    "activated",
    "已激活",
    "已啟用",
    "已启用",
    "success",
    "账户已激活",
    "帳戶已啟用",
    "registration complete",
    "activation successful",
    "activation complete",
    "啟動成功",
    "启动成功",
    "已啟動",
    "已启动",
]


async def _has_visible_password(page) -> bool:
    inp = page.locator("input[type='password']").first
    return await inp.count() > 0 and await inp.is_visible()


async def _page_looks_activated(page, *, after_login: bool = False) -> bool:
    try:
        body_text = (await page.inner_text("body")).lower()
    except Exception:
        body_text = ""
    if any(k in body_text for k in _SUCCESS_KEYWORDS):
        return True
    if not after_login:
        return False
    url_now = (page.url or "").lower()
    if "e-services.cr.gov.hk" in url_now and "s06.do" not in url_now:
        return True
    try:
        from src.browser.icris_nnc1_form import IcrisNnc1FormBot

        return await IcrisNnc1FormBot()._is_logged_in(page)
    except Exception:
        return False


async def activate_icris_account(
    activation_url: str,
    username: str = "",
    password: str = "",
) -> tuple[bool, str]:
    """用 Playwright 打开激活链接；若出现登录框则用入库账号密码登录。

    返回: (成功与否, 截图路径或错误消息)
    """
    try:
        from src.browser.launcher import import_async_playwright, launch_browser, create_browser_context
    except ImportError as e:
        raise RuntimeError(
            "请先安装 Playwright: pip install playwright && playwright install chromium"
        ) from e

    user = (username or "").strip()
    pwd = (password or "").strip()

    async_playwright = import_async_playwright()
    shot_dir = PROJECT_ROOT / "data" / "icris_activations"
    shot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shot_file = shot_dir / f"activation_{stamp}.png"

    async with async_playwright() as p:
        browser = await launch_browser(p, force_isolated=True)
        context = await create_browser_context(browser)
        page = await context.new_page()
        try:
            logger.info("打开激活链接: %s", activation_url[:80])
            await page.goto(activation_url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(2000)

            is_success = await _page_looks_activated(page)
            if not is_success and user and pwd and await _has_visible_password(page):
                from src.email.imap_client import IcrisAccount
                from src.browser.icris_nnc1_form import IcrisNnc1FormBot

                logger.info("激活页需登录，使用入库账号: %s", user)
                bot = IcrisNnc1FormBot()
                await bot._login(page, IcrisAccount(username=user, password=pwd))
                is_success = await _page_looks_activated(page, after_login=True)

            await page.screenshot(path=str(shot_file), full_page=True)
            logger.info("激活页面截图: %s (success=%s)", shot_file, is_success)
            return (is_success, str(shot_file))
        except Exception as e:
            logger.error("激活流程异常: %s", e)
            try:
                if not page.is_closed():
                    await page.screenshot(path=str(shot_file), full_page=True)
            except Exception:
                pass
            return (False, str(e))
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
