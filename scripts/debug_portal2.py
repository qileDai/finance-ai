"""调试：在重定向前快速点击注册"""
import asyncio
import re
from playwright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"


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
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()

        page.on("console", lambda m: print(f"  CONSOLE [{m.type}]: {m.text[:120]}"))

        redirects_to_cr = []

        async def on_response(resp):
            url = resp.url
            if "cr.gov.hk" in url and "e-services" not in url:
                if resp.request.is_navigation_request():
                    print(f"  REDIRECT NAV {resp.status} {url}")

        page.on("response", on_response)

        print("=== 快速流程：门户加载后立即点注册 ===")
        await page.goto(PORTAL, wait_until="commit", timeout=60000)

        # 等待 e-services home 出现（不等完整 load，抢在重定向前）
        for i in range(30):
            if "e-services.cr.gov.hk" in page.url and "home.do" in page.url:
                print(f"  portal ready at {i*200}ms: {page.url}")
                break
            await page.wait_for_timeout(200)
        else:
            print(f"  portal not ready, url={page.url}")

        # 立即关弹窗 + cookie + 点注册
        await page.keyboard.press("Escape")
        cookie = page.locator("button:has-text('接受')").last
        if await cookie.count() > 0:
            try:
                await cookie.click(timeout=2000)
            except Exception:
                pass

        btn = page.get_by_role("button", name="立即登记")
        print(f"  register btn count={await btn.count()}, url={page.url}")
        if await btn.count() > 0:
            await btn.first.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=15000)
            except Exception as e:
                print(f"  wait fail: {e}")

        print(f"FINAL: {page.url}")

        if "registration" in page.url:
            print("SUCCESS")
        else:
            # 尝试阻止离开 e-services 的方式：直接用当前 url 的 clock
            m = re.search(r"systemclock=(\d+)", page.url)
            if not m:
                # 重新获取 - 可能被 redirect 了
                print("  已离开 e-services，尝试从 cr.gov.hk 找入口链接")
                link = page.locator("a[href*='e-services.cr.gov.hk']").first
                if await link.count() > 0:
                    href = await link.get_attribute("href")
                    print(f"  found link: {href}")

        await page.wait_for_timeout(20000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
