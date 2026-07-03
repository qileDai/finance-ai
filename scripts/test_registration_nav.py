"""验证 stealth + 注册导航集成"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)

from src.browser.icris_registration import IcrisRegistrationBot
from src.materials.packager import load_mock_data


async def main():
    bot = IcrisRegistrationBot()
    data = load_mock_data()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed")
        return

    from src.browser.launcher import create_browser_context, launch_browser

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()

        page = await bot._navigate_to_registration(page)
        print(f"navigate ok={page is not None}, url={page.url if page else 'N/A'}")

        if page:
            await bot._dismiss_portal_overlays(page)
            captcha_ok = await bot._fill_captcha(page)
            print(f"captcha ok={captcha_ok}")

        await page.wait_for_timeout(10000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
