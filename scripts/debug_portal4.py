"""测试：网络层拦截 cr.gov.hk 跳转"""
import asyncio
import re
from playwright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"
REG_BASE = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"

STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(locale="zh-HK")
        await ctx.add_init_script(STEALTH)

        async def block_cr_redirect(route):
            req = route.request
            url = req.url
            if req.is_navigation_request() and "cr.gov.hk" in url and "e-services" not in url:
                print(f"  BLOCKED NAV: {url}")
                await route.abort()
                return
            await route.continue_()

        await ctx.route("**/*", block_cr_redirect)
        page = await ctx.new_page()

        print("=== portal ===")
        await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)
        print(f"url after 10s: {page.url}")

        btn = page.get_by_role("button", name="立即登记")
        for _ in range(30):
            if await btn.count() > 0:
                break
            await page.wait_for_timeout(500)

        print(f"btn={await btn.count()}")
        if await btn.count() > 0:
            await btn.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=15000)
            except Exception as e:
                print(e)

        if "registration" not in page.url:
            m = re.search(r"systemclock=(\d+)", page.url)
            if m:
                url = f"{REG_BASE}?systemclock={m.group(1)}&webEnv=PROD&isOnsite=false&inactiveTime=0"
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        print(f"FINAL: {page.url}")
        await page.wait_for_timeout(20000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
