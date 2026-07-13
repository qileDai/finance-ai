"""Playwright 浏览器启动（支持系统 Chrome/Edge，无需下载 Chromium）"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


def _try_launch_cdp_chrome() -> bool:
    """尝试启动带 remote-debugging-port 的 Chrome（Windows）"""
    import os
    import subprocess
    import time
    from pathlib import Path

    from urllib.parse import urlparse

    parsed = urlparse(settings.chrome_cdp_url)
    port = parsed.port or 9222
    profile = Path(os.environ.get("TEMP", ".")) / "icris-chrome-cdp-profile"
    profile.mkdir(parents=True, exist_ok=True)

    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    chrome = next((p for p in candidates if p.is_file()), None)
    if not chrome:
        return False
    try:
        subprocess.Popen(
            [
                str(chrome),
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(4)
        return True
    except Exception as e:
        logger.debug("自动启动 Chrome CDP 失败: %s", e)
        return False


@dataclass
class BrowserSession:
    browser: Any
    context: Any
    external_cdp: bool = False


async def launch_browser(playwright: Any) -> Any:
    """启动浏览器，降低被识别为自动化脚本的概率"""
    if settings.chrome_use_existing:
        try:
            browser = await playwright.chromium.connect_over_cdp(settings.chrome_cdp_url)
            logger.info("已连接已有 Chrome: %s", settings.chrome_cdp_url)
            return browser
        except Exception as e:
            logger.warning(
                "无法连接 CDP %s (%s)，回退到启动新浏览器",
                settings.chrome_cdp_url,
                e,
            )
            if _try_launch_cdp_chrome():
                try:
                    browser = await playwright.chromium.connect_over_cdp(
                        settings.chrome_cdp_url
                    )
                    logger.info("已自动启动并连接 Chrome CDP: %s", settings.chrome_cdp_url)
                    return browser
                except Exception as e2:
                    logger.warning("自动 CDP 连接仍失败: %s", e2)

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
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
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
    external_cdp = bool(settings.chrome_use_existing and browser.contexts)
    if external_cdp and browser.contexts:
        context = browser.contexts[0]
        logger.info("复用已有 Chrome 上下文 (CDP)")
        try:
            await setup_stealth_context(context)
        except Exception as e:
            logger.debug("CDP 上下文 stealth 注入跳过: %s", e)
        return context

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


def is_external_cdp_browser(browser: Any) -> bool:
    """是否通过 connect_over_cdp 连接（不应 close browser）"""
    return settings.chrome_use_existing and bool(getattr(browser, "contexts", None))


async def close_browser_session(browser: Any, *, external_cdp: bool | None = None) -> None:
    """关闭浏览器；CDP 外部连接时仅断开 Playwright"""
    if external_cdp if external_cdp is not None else is_external_cdp_browser(browser):
        logger.info("CDP 模式：保留用户 Chrome 窗口，仅断开 Playwright")
        return
    await browser.close()
