"""测试 s02 页面「继续」按钮点击"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.browser.icris_registration import IcrisRegistrationBot
from src.materials.packager import load_mock_data

HTML = ROOT / "data" / "debug" / "s02_s02_before_fill_20260706_134056.html"


async def main() -> None:
    if not HTML.exists():
        print("缺少 HTML")
        return

    html = HTML.read_text(encoding="utf-8")
    bot = IcrisRegistrationBot()
    data = load_mock_data()

    from playwright.async_api import async_playwright
    from src.browser.launcher import launch_browser

    async with async_playwright() as p:
        browser = await launch_browser(p)
        page = await browser.new_page()
        await page.set_content(html, wait_until="domcontentloaded")

        await bot._fill_account_profile_native(page, data)
        btn = page.locator("button[type='submit'].primary")
        print("button count:", await btn.count())
        print("button text:", await btn.inner_text())
        clicked = await bot._click_continue(page)
        print("continue clicked (about:blank url):", clicked)

        # 模拟真实 registration URL 再测
        await page.evaluate(
            '() => { history.replaceState({}, "", '
            '"https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s02.do#"); }'
        )
        clicked2 = await bot._click_continue(page)
        print("continue clicked (mock s02 url):", clicked2)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
