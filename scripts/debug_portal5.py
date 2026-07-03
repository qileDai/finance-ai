"""拦截 disable-devtool 脚本 + 阻止跳转"""
import asyncio
import re
from playwright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"
REG_BASE = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"

PRELOAD = """
window.DisableDevtool = function() { return { success: false, reason: 'blocked' }; };
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };

// disable-devtool type=6 使用 performance 检测，返回正常值
const _now = performance.now.bind(performance);
performance.now = function() { return _now(); };
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(locale="zh-HK")
        await ctx.add_init_script(PRELOAD)

        async def route_handler(route):
            url = route.request.url.lower()
            # 拦截 disable-devtool 相关脚本
            if any(k in url for k in ("disable-devtool", "devtool", "anti-debug", "anti_debug")):
                print(f"  BLOCK JS: {route.request.url[:80]}")
                await route.fulfill(status=200, content_type="application/javascript", body="// blocked")
                return
            if route.request.is_navigation_request() and "cr.gov.hk" in url and "e-services" not in url:
                print(f"  BLOCK NAV: {url[:80]}")
                await route.abort()
                return
            await route.continue_()

        await ctx.route("**/*", route_handler)
        page = await ctx.new_page()
        page.on("console", lambda m: print(f"  [{m.type}] {m.text[:90]}") if "devtool" in m.text.lower() or "DEVTOOL" in m.text else None)

        await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)
        print(f"url: {page.url}")

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
                await page.goto(
                    f"{REG_BASE}?systemclock={m.group(1)}&webEnv=PROD&isOnsite=false&inactiveTime=0",
                    wait_until="domcontentloaded",
                )

        print(f"FINAL: {page.url}")
        await page.wait_for_timeout(15000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
