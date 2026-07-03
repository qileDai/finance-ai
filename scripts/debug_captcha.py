"""调试 ICRIS 注册页验证码 DOM 结构"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO)

from src.browser.icris_captcha import fill_captcha as fill_icris_captcha, find_captcha_image
from src.browser.icris_registration import IcrisRegistrationBot
from src.browser.launcher import create_browser_context, launch_browser


async def dump_captcha_dom(page):
    info = await page.evaluate("""() => {
        const imgs = [...document.querySelectorAll('img')].map(i => ({
            src: i.src?.slice(0, 120),
            id: i.id, name: i.name, alt: i.alt,
            w: i.width, h: i.height, visible: i.offsetParent !== null
        }));
        const inputs = [...document.querySelectorAll('input')].map(i => ({
            type: i.type, id: i.id, name: i.name,
            placeholder: i.placeholder, ariaLabel: i.getAttribute('aria-label'),
            visible: i.offsetParent !== null
        }));
        const canvas = [...document.querySelectorAll('canvas')].length;
        return { imgs, inputs, canvas, url: location.href };
    }""")
    print("=== DOM dump ===")
    print(f"url: {info['url']}")
    print(f"canvas count: {info['canvas']}")
    print("images:")
    for img in info["imgs"]:
        print(f"  {img}")
    print("inputs:")
    for inp in info["inputs"]:
        print(f"  {inp}")


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

        await page.wait_for_timeout(3000)
        await dump_captcha_dom(page)

        # 尝试 LLM 识别：找任意小尺寸 img
        captcha_img = await find_captcha_image(page)
        print(f"find_captcha_image: {captcha_img is not None}")

        ok = await fill_icris_captcha(page, bot.llm)
        print(f"fill_captcha ok={ok}")
        val = await page.locator("#checkCode").input_value()
        print(f"checkCode value={val!r}")

        await page.wait_for_timeout(15000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
