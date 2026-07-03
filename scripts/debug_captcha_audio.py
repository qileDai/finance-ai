"""测试语音验证码识别"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO)

from src.browser.icris_captcha import fill_captcha
from src.browser.icris_registration import IcrisRegistrationBot
from src.browser.launcher import create_browser_context, launch_browser


async def main():
    bot = IcrisRegistrationBot()
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()
        page = await bot._navigate_to_registration(page)
        print(f"nav ok={page is not None}")
        if not page:
            await browser.close()
            return

        ok = await fill_captcha(page, bot.llm)
        val = await page.locator("#checkCode").input_value()
        print(f"fill_captcha ok={ok} value={val!r}")
        await page.wait_for_timeout(8000)
        await browser.close()


asyncio.run(main())
