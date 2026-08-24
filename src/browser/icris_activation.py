"""ICRIS 账号激活 — 用浏览器打开邮件激活链接。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


async def activate_icris_account(activation_url: str) -> tuple[bool, str]:
    """用 Playwright 打开激活链接，等待激活成功页面。

    返回: (成功与否, 截图路径或错误消息)
    """
    try:
        from src.browser.launcher import import_async_playwright, launch_browser, create_browser_context
    except ImportError as e:
        raise RuntimeError(
            "请先安装 Playwright: pip install playwright && playwright install chromium"
        ) from e

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

            # 检测激活成功页面
            body_text = (await page.inner_text("body")).lower()
            success_keywords = [
                "activated", "已激活", "已啟用", "success",
                "账户已激活", "帳戶已啟用", "registration complete",
                "activation successful", "activation complete",
            ]
            is_success = any(k in body_text for k in success_keywords)
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
