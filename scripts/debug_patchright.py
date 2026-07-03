"""用 patchright 绕过 ICRIS 反调试检测"""
import asyncio
import re
from patchright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"
REG_BASE = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
        )
        ctx = await browser.new_context(locale="zh-HK")
        page = await ctx.new_page()

        print("=== patchright portal ===")
        await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        print(f"after 8s: {page.url}")

        if "cr.gov.hk/tc/home" in page.url:
            print("FAIL: still redirected")
            await browser.close()
            return

        btn = page.get_by_role("button", name="立即登记")
        for _ in range(20):
            if await btn.count() > 0:
                break
            await page.wait_for_timeout(500)

        print(f"btn={await btn.count()}")
        if await btn.count() > 0:
            await btn.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=20000)
            except Exception as e:
                print(f"click: {e}")

        if "registration" not in page.url:
            m = re.search(r"systemclock=(\d+)", page.url)
            if m:
                url = f"{REG_BASE}?systemclock={m.group(1)}&webEnv=PROD&isOnsite=false&inactiveTime=0"
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        print(f"FINAL: {page.url}")
        print(f"OK: {'registration' in page.url}")
        await page.wait_for_timeout(15000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
