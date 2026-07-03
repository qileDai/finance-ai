"""调试 ICRIS 门户跳转问题"""
import asyncio
from playwright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"
REG_BASE = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            locale="zh-HK",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()
        navs = []

        def on_nav(frame):
            if frame == page.main_frame:
                navs.append(frame.url)
                print(f"  NAV -> {frame.url}")

        page.on("framenavigated", on_nav)

        print("=== 1. 访问门户 ===")
        r1 = await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        print(f"status={r1.status if r1 else None}, url={page.url}")
        await page.wait_for_timeout(5000)

        if "cr.gov.hk/tc/home" in page.url or "cr.gov.hk/en/home" in page.url:
            print("FAIL: 门户被重定向到 cr.gov.hk 公开站")
            await page.wait_for_timeout(10000)
            await browser.close()
            return

        import re
        m = re.search(r"systemclock=(\d+)", page.url)
        clock = m.group(1) if m else None
        print(f"systemclock={clock}")

        # cookie
        cookie = page.locator("button:has-text('接受')").last
        if await cookie.count() > 0 and await cookie.is_visible():
            await cookie.click()
            print("clicked cookie accept")

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1000)

        print("=== 2. 点击立即登记 ===")
        btn = page.get_by_role("button", name="立即登记")
        if await btn.count() > 0:
            await btn.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=20000)
            except Exception as e:
                print(f"wait registration timeout: {e}")
            print(f"after click url={page.url}")

        if "registration" not in page.url and clock:
            print("=== 3. 用 systemclock 直接打开 ===")
            url = f"{REG_BASE}?systemclock={clock}&webEnv=PROD&isOnsite=false&inactiveTime=0"
            r2 = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print(f"status={r2.status if r2 else None}, url={page.url}")

        print(f"\nFINAL URL: {page.url}")
        print(f"SUCCESS: {'registration' in page.url}")
        await page.wait_for_timeout(15000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
