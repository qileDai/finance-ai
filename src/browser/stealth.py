"""ICRIS 反自动化绕过：拦截 disable-devtool 脚本"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 站点加载 disable-devtool.min.js，检测到 Playwright/CDP 后会跳转到 cr.gov.hk
DISABLE_DEVTOOL_MARKER = "disable-devtool"

PRELOAD_SCRIPT = """
window.DisableDevtool = function() { return { success: false, reason: 'blocked' }; };
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
"""


async def setup_stealth_context(context: Any) -> None:
    """为 BrowserContext 注入反检测脚本与路由拦截"""
    await context.add_init_script(PRELOAD_SCRIPT)

    async def route_handler(route):
        url = route.request.url.lower()
        if DISABLE_DEVTOOL_MARKER in url:
            logger.debug("已拦截反调试脚本: %s", route.request.url)
            await route.fulfill(
                status=200,
                content_type="application/javascript",
                body="// blocked by register-ai",
            )
            return
        # 阻止被踢到公司注册处公开站（非 e-services 子域）
        if route.request.is_navigation_request():
            if "cr.gov.hk" in url and "e-services.cr.gov.hk" not in url:
                logger.warning("已拦截外站跳转: %s", route.request.url)
                await route.abort()
                return
        await route.continue_()

    await context.route("**/*", route_handler)
    logger.info("已启用 ICRIS 反跳转保护（拦截 disable-devtool）")
