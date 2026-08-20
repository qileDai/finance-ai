"""用 patchright 不带任何 stealth，直连 ICRIS 注册页"""

import asyncio
from patchright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"
REG_BASE = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome")
        # 纯净 context，不注入任何 stealth 脚本，不挂 route_handler
        ctx = await browser.new_context(locale="zh-HK")
        page = await ctx.new_page()

        print("=== 1. 门户 ===")
        await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        print(f"URL: {page.url}")
        if "cr.gov.hk/tc/home" in page.url:
            print("FAIL: redirected to public site")
            await browser.close()
            return

        # 点击「立即登记」
        btn = page.get_by_role("button", name="立即登記")
        for _ in range(20):
            if await btn.count() > 0:
                break
            await page.wait_for_timeout(500)

        print(f"btn count: {await btn.count()}")
        if await btn.count() > 0:
            await btn.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=20000)
            except Exception as e:
                print(f"click wait: {e}")

        import re
        if "registration" not in page.url:
            m = re.search(r"systemclock=(\d+)", page.url)
            if m:
                url = f"{REG_BASE}?systemclock={m.group(1)}&webEnv=PROD&isOnsite=false&inactiveTime=0"
                print(f"=== 2. 打开注册页: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        print(f"FINAL URL: {page.url}")
        print(f"registration in url: {'registration' in page.url}")

        # 等 Vue 挂载
        await page.wait_for_timeout(15000)

        probe = await page.evaluate("""() => ({
            url: window.location.href,
            title: document.title,
            bodyLen: document.body ? document.body.innerHTML.length : 0,
            vue: !!(window.__VUE__ || window.Vue || document.querySelector('[data-v-app], #app, [id*=app]')),
            forms: document.querySelectorAll('form').length,
            inputs: document.querySelectorAll('input').length,
            checkbox: document.querySelectorAll('input[type=checkbox]').length,
            scripts: Array.from(document.querySelectorAll('script[src]')).map(s => s.src).slice(0, 8),
        })""")
        print(f"\nProbe: {probe}")

        if probe['bodyLen'] > 100:
            print("✅ 注册页加载成功！")
        else:
            print("❌ 注册页 body 为空（F5 bot 检测阻止渲染）")

        await page.wait_for_timeout(10000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
