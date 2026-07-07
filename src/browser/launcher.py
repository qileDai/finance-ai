"""Playwright 浏览器启动（支持系统 Chrome/Edge，无需下载 Chromium）"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from src.browser.stealth import setup_stealth_context

logger = logging.getLogger(__name__)

FALLBACK_CHANNELS = ("chrome", "msedge", "chromium")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def launch_browser(playwright: Any) -> Any:
    """启动浏览器，降低被识别为自动化脚本的概率"""
    configured = getattr(settings, "browser_channel", "") or ""
    channels: list[str | None] = []

    if configured:
        channels.append(configured if configured != "chromium" else None)
    for ch in FALLBACK_CHANNELS:
        if ch == "chromium":
            if None not in channels:
                channels.append(None)
        elif ch not in channels:
            channels.append(ch)

    last_error: Exception | None = None
    for channel in channels:
        label = channel or "chromium (bundled)"
        launch_kwargs: dict[str, Any] = {
            "headless": settings.browser_headless,
            "slow_mo": settings.browser_slow_mo,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if settings.browser_no_proxy:
            launch_kwargs["args"].append("--no-proxy-server")
            launch_kwargs["args"].append("--proxy-server=direct://")
        if channel and channel != "chromium":
            launch_kwargs["channel"] = channel

        try:
            browser = await playwright.chromium.launch(**launch_kwargs)
            logger.info("浏览器已启动: %s", label)
            return browser
        except Exception as e:
            last_error = e
            logger.warning("无法使用 %s: %s", label, e)

    hint = (
        "请安装 Google Chrome / Microsoft Edge，或执行: python -m playwright install chromium"
    )
    raise RuntimeError(f"无法启动浏览器。{hint}") from last_error


async def create_browser_context(browser: Any) -> Any:
    """创建带反检测保护的浏览器上下文"""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        user_agent=USER_AGENT,
        extra_http_headers={
            "Accept-Language": "zh-CN,zh;q=0.9,zh-HK;q=0.8,en;q=0.7",
        },
    )
    await setup_stealth_context(context)
    try:
        await context.add_cookies(
            [
                {
                    "name": "locale",
                    "value": "zh_CN",
                    "domain": "www.e-services.cr.gov.hk",
                    "path": "/",
                },
                {
                    "name": "lang",
                    "value": "zh_CN",
                    "domain": ".e-services.cr.gov.hk",
                    "path": "/",
                },
            ]
        )
    except Exception:
        pass
    return context
