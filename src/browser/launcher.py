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


def _try_import_patchright():
    """尝试导入 patchright（反 CDP 检测的 Playwright fork）。"""
    try:
        from patchright.async_api import async_playwright

        logger.info("使用 patchright（反 CDP 检测模式）")
        return async_playwright
    except ImportError:
        return None


def import_async_playwright():
    """导入 Playwright；优先使用 patchright（如安装），否则用普通 Playwright。"""
    # patchright 可选：环境变量 PATCHRIGHT=1 时启用
    import os
    if os.environ.get("PATCHRIGHT", "").lower() in ("1", "true", "yes"):
        pw = _try_import_patchright()
        if pw:
            return pw

    try:
        from playwright.async_api import async_playwright

        return async_playwright
    except ImportError as e:
        msg = str(e).lower()
        if "greenlet" in msg or "dll load failed" in msg:
            import sys

            hint = (
                "Playwright 依赖 greenlet 原生模块加载失败。"
                "Windows 请执行: pip install msvc-runtime"
            )
            if sys.version_info >= (3, 14):
                hint += "（Python 3.14 需 msvc-runtime；仍失败请改用 Python 3.11–3.12）"
            raise RuntimeError(hint) from e
        raise RuntimeError(
            "请先安装 Playwright: pip install playwright && playwright install chromium"
        ) from e


def _try_launch_cdp_chrome() -> bool:
    """尝试启动带 remote-debugging-port 的 Chrome（Windows/Linux/macOS）"""
    import os
    import platform
    import subprocess
    import time
    from pathlib import Path

    from urllib.parse import urlparse

    parsed = urlparse(settings.chrome_cdp_url)
    port = parsed.port or 9222
    # 跨平台临时目录：Windows 用 TEMP，Linux/macOS 用 /tmp
    tmp_base = os.environ.get("TEMP") or "/tmp"
    profile = Path(tmp_base) / "icris-chrome-cdp-profile"
    profile.mkdir(parents=True, exist_ok=True)

    candidates = [
        # Windows
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        # Linux（容器/宝塔/服务器）
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium-browser"),
        Path("/usr/bin/chromium"),
        # macOS
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    chrome = next((p for p in candidates if p.is_file()), None)
    if not chrome:
        return False

    is_linux = platform.system() == "Linux"
    launch_args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
    ]
    # Linux/容器环境：无法开 sandbox；Xvfb 提供虚拟 DISPLAY，不需要 headless
    if is_linux:
        launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    launch_args.append("about:blank")

    try:
        subprocess.Popen(
            launch_args,
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


def _is_patchright() -> bool:
    """是否正在使用 patchright（而非普通 Playwright）"""
    import sys

    return "patchright" in sys.modules or any(
        "patchright" in str(m) for m in sys.modules.values() if m
    )


async def launch_browser(
    playwright: Any,
    *,
    force_isolated: bool = False,
    headless: bool | None = None,
) -> Any:
    """启动浏览器，降低被识别为自动化脚本的概率。

    force_isolated=True 时忽略 CHROME_USE_EXISTING，供队列 Worker 使用，避免抢 CDP。
    patchright 优先：用 launch(channel=chrome) 替代 CDP 连接，绕过 F5 bot 检测。
    """
    # patchright 模式：直接 launch(channel=chrome)，不用 CDP（避免 F5 检测）
    if _is_patchright():
        headless_flag = settings.browser_headless if headless is None else bool(headless)
        launch_kwargs: dict[str, Any] = {
            "headless": headless_flag,
            "channel": "chrome",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
        }
        if settings.browser_no_proxy:
            launch_kwargs["args"].append("--no-proxy-server")
            launch_kwargs["args"].append("--proxy-server=direct://")
        try:
            browser = await playwright.chromium.launch(**launch_kwargs)
            logger.info("patchright 启动 Chrome (channel=chrome) headless=%s", headless_flag)
            return browser
        except Exception as e:
            logger.warning("patchright 启动失败: %s，回退到 CDP 模式", e)

    use_existing = bool(settings.chrome_use_existing) and not force_isolated
    if use_existing:
        try:
            browser = await playwright.chromium.connect_over_cdp(settings.chrome_cdp_url)
            logger.info("已连接已有 Chrome: %s", settings.chrome_cdp_url)
            return browser
        except Exception as e:
            logger.warning(
                "无法连接 CDP %s (%s)，尝试自动启动 Chrome CDP",
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
                    logger.warning("自动 CDP 连接仍失败: %s，回退到 Playwright 启动", e2)

    # 非 CDP 模式：尝试自动启动 Chrome CDP（ICRIS 门户会检测 TLS 指纹）
    if not force_isolated and not use_existing:
        if _try_launch_cdp_chrome():
            try:
                browser = await playwright.chromium.connect_over_cdp(settings.chrome_cdp_url)
                logger.info("自动启动 Chrome CDP 成功（绕过 TLS 指纹检测）: %s", settings.chrome_cdp_url)
                return browser
            except Exception as e:
                logger.debug("自动 CDP 启动后连接失败: %s，回退到 Playwright 启动", e)

    if force_isolated:
        logger.info("强制独立浏览器（忽略 CHROME_USE_EXISTING）")

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

    headless_flag = settings.browser_headless if headless is None else bool(headless)
    last_error: Exception | None = None
    for channel in channels:
        label = channel or "chromium (bundled)"
        launch_kwargs: dict[str, Any] = {
            "headless": headless_flag,
            "slow_mo": settings.browser_slow_mo,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
        }
        if settings.browser_no_proxy:
            launch_kwargs["args"].append("--no-proxy-server")
            launch_kwargs["args"].append("--proxy-server=direct://")
        if channel and channel != "chromium":
            launch_kwargs["channel"] = channel
        try:
            browser = await playwright.chromium.launch(**launch_kwargs)
            logger.info("已启动浏览器: %s headless=%s", label, headless_flag)
            return browser
        except Exception as e:
            last_error = e
            logger.warning("启动浏览器失败 (%s): %s", label, e)

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
            logger.info("CDP 上下文已注入 stealth 脚本")
        except Exception as e:
            logger.debug("CDP 上下文 stealth 注入跳过: %s", e)
        return context

    # 检测是否为自动启动的 CDP 浏览器
    if browser.contexts:
        context = browser.contexts[0]
        logger.info("复用 CDP 浏览器上下文")
        try:
            await setup_stealth_context(context)
            logger.info("CDP 上下文已注入 stealth 脚本")
        except Exception as e:
            logger.debug("CDP 上下文 stealth 注入跳过: %s", e)
    else:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            user_agent=USER_AGENT,
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,zh-HK;q=0.8,en;q=0.7",
                "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="99", "Google Chrome";v="131"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
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
