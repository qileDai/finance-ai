"""测试验证码识别准确率"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.browser.icris_registration import IcrisRegistrationBot
from src.browser.icris_captcha import find_captcha_image, _get_captcha_bytes
from src.browser.launcher import create_browser_context, launch_browser
from src.browser.captcha_solver import solve_captcha, preprocess_captcha_image


async def main():
    bot = IcrisRegistrationBot()
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()
        page = await bot._navigate_to_registration(page)
        if not page:
            print("nav failed")
            await browser.close()
            return

        await page.wait_for_timeout(2000)
        img = await find_captcha_image(page)
        raw = await _get_captcha_bytes(img)
        Path("output").mkdir(exist_ok=True)
        Path("output/captcha_raw.gif").write_bytes(raw)

        from src.browser.icris_captcha import ICRIS_CAPTCHA_LENGTH
        print(f"expected_length={ICRIS_CAPTCHA_LENGTH}")

        code = solve_captcha(raw, bot.llm, max_length=ICRIS_CAPTCHA_LENGTH)
        print(f"result={code!r} len={len(code)}")

        await browser.close()


asyncio.run(main())
