"""指纹诊断脚本：验证 stealth 注入 + ICRIS 访问"""

import asyncio
import subprocess
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.browser.launcher import (
    USER_AGENT,
    create_browser_context,
    import_async_playwright,
    launch_browser,
    close_browser_session,
)


async def test_with_stealth():
    """测试 1：带 stealth route_handler（正常流程）"""
    print("=" * 60)
    print("测试 1: 带 stealth route_handler")
    print("=" * 60)

    pw = import_async_playwright()
    pw_instance = await pw().start()
    browser = await launch_browser(pw_instance)
    context = await create_browser_context(browser)
    page = await context.new_page()

    try:
        resp = await page.goto(
            "https://www.e-services.cr.gov.hk/",
            wait_until="commit",
            timeout=30000,
        )
        print(f"  HTTP 状态: {resp.status if resp else 'None'}")
        print(f"  最终 URL: {page.url}")
        if "chrome-error" in page.url:
            print("  ❌ chrome-error（网络层错误）")
        elif "www.cr.gov.hk" in page.url and "e-services" not in page.url:
            print("  ❌ 被重定向到公开站")
        else:
            print("  ✅ 访问成功")
    except Exception as e:
        print(f"  ❌ 失败: {e}")
        print(f"  最终 URL: {page.url}")

    await page.close()
    await close_browser_session(browser)
    await pw_instance.stop()


async def test_without_route_handler():
    """测试 2：CDP 连接但不带 route_handler（排除 route 干扰）"""
    print("\n" + "=" * 60)
    print("测试 2: CDP 连接但无 route_handler")
    print("=" * 60)

    pw = import_async_playwright()
    pw_instance = await pw().start()
    browser = await launch_browser(pw_instance)

    # 不调用 create_browser_context，直接用 browser.contexts[0]
    if browser.contexts:
        context = browser.contexts[0]
        print("  使用 CDP 默认 context（无 stealth）")
    else:
        context = await browser.new_context()
        print("  新建 context（无 stealth）")

    page = await context.new_page()

    try:
        resp = await page.goto(
            "https://www.e-services.cr.gov.hk/",
            wait_until="commit",
            timeout=30000,
        )
        print(f"  HTTP 状态: {resp.status if resp else 'None'}")
        print(f"  最终 URL: {page.url}")
        if "chrome-error" in page.url:
            print("  ❌ chrome-error（网络层错误）")
        elif "www.cr.gov.hk" in page.url and "e-services" not in page.url:
            print("  ❌ 被重定向到公开站")
        else:
            print("  ✅ 访问成功")
    except Exception as e:
        print(f"  ❌ 失败: {e}")
        print(f"  最终 URL: {page.url}")

    await page.close()
    await close_browser_session(browser)
    await pw_instance.stop()


async def test_raw_chrome():
    """测试 3：完全不用 Playwright，直接用 Chrome 命令行访问"""
    print("\n" + "=" * 60)
    print("测试 3: 完全不用 Playwright（curl + 浏览器 UA）")
    print("=" * 60)

    result = subprocess.run(
        [
            "curl.exe",
            "-sI",
            "-H",
            f"User-Agent: {USER_AGENT}",
            "-H",
            'sec-ch-ua: "Chromium";v="151", "Not_A Brand";v="99", "Google Chrome";v="151"',
            "-H",
            "sec-ch-ua-mobile: ?0",
            "-H",
            'sec-ch-ua-platform: "Windows"',
            "-H",
            "Accept-Language: zh-CN,zh;q=0.9,zh-HK;q=0.8,en;q=0.7",
            "https://www.e-services.cr.gov.hk/",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    lines = result.stdout.strip().split("\n")
    for line in lines[:10]:
        print(f"  {line}")

    # 跟踪重定向
    print("\n  跟踪重定向:")
    result2 = subprocess.run(
        [
            "curl.exe",
            "-s",
            "-o",
            "NUL",
            "-w",
            "%{http_code} -> %{url_effective}",
            "-L",
            "--max-redirs",
            "5",
            "-H",
            f"User-Agent: {USER_AGENT}",
            "https://www.e-services.cr.gov.hk/",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    print(f"  {result2.stdout.strip()}")


async def test_fingerprint():
    """指纹检查"""
    print("=" * 60)
    print("指纹检查")
    print("=" * 60)
    print(f"  UA = {USER_AGENT}")

    pw = import_async_playwright()
    pw_instance = await pw().start()
    browser = await launch_browser(pw_instance)
    context = await create_browser_context(browser)
    page = await context.new_page()

    await page.goto("about:blank")
    fp = await page.evaluate(
        """() => ({
            webdriver: navigator.webdriver,
            plugins_len: navigator.plugins.length,
            chrome_runtime: !!window.chrome?.runtime,
            languages: navigator.languages,
            ua: navigator.userAgent,
        })"""
    )
    for k, v in fp.items():
        print(f"  {k}: {v}")

    await page.close()
    await close_browser_session(browser)
    await pw_instance.stop()


async def main():
    await test_fingerprint()
    await test_with_stealth()
    await test_without_route_handler()
    await test_raw_chrome()
    print("\n诊断完成。")


if __name__ == "__main__":
    asyncio.run(main())
