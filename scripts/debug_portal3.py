"""拦截 ICRIS 反调试跳转 + 测试注册入口"""
import asyncio
import re
from playwright.async_api import async_playwright

PORTAL = "https://www.e-services.cr.gov.hk/"
REG_BASE = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"

# 在页面脚本执行前注入：阻止被检测后跳转到 cr.gov.hk 公开站
ANTI_REDIRECT_SCRIPT = """
(() => {
  const shouldBlock = (url) => {
    if (!url || typeof url !== 'string') return false;
    return url.includes('cr.gov.hk') && !url.includes('e-services.cr.gov.hk');
  };

  const wrap = (obj, key, factory) => {
    const orig = obj[key];
    if (!orig) return;
    obj[key] = factory(orig);
  };

  wrap(window.location, 'assign', (orig) => function(url) {
    if (shouldBlock(String(url))) { console.warn('[agent] blocked assign', url); return; }
    return orig.call(window.location, url);
  });
  wrap(window.location, 'replace', (orig) => function(url) {
    if (shouldBlock(String(url))) { console.warn('[agent] blocked replace', url); return; }
    return orig.call(window.location, url);
  });

  // 阻止 top.location 跳转
  try {
    const desc = Object.getOwnPropertyDescriptor(window.location.__proto__, 'href');
    if (desc && desc.set) {
      Object.defineProperty(window.location, 'href', {
        get: desc.get,
        set(v) {
          if (shouldBlock(String(v))) { console.warn('[agent] blocked href', v); return; }
          desc.set.call(window.location, v);
        },
        configurable: true,
      });
    }
  } catch (e) {}

  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  window.chrome = window.chrome || { runtime: {} };
})();
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-devtools",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        ctx = await browser.new_context(
            locale="zh-HK",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "zh-HK,zh;q=0.9"},
        )
        await ctx.add_init_script(ANTI_REDIRECT_SCRIPT)
        page = await ctx.new_page()

        page.on("console", lambda m: print(f"  [{m.type}] {m.text[:100]}") if "agent" in m.text or "devtool" in m.text.lower() else None)

        print("=== goto portal ===")
        await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        print(f"after 8s: {page.url}")

        if "e-services" not in page.url:
            print("FAIL still redirected")
            await page.wait_for_timeout(10000)
            await browser.close()
            return

        # 等 Vue 渲染完成
        btn = page.get_by_role("button", name="立即登记")
        for _ in range(20):
            if await btn.count() > 0:
                break
            await page.wait_for_timeout(500)

        print(f"register btn={await btn.count()}")
        if await btn.count() > 0:
            await btn.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=20000)
            except Exception as e:
                print(f"click wait: {e}")

        if "registration" not in page.url:
            m = re.search(r"systemclock=(\d+)", page.url)
            if m:
                url = f"{REG_BASE}?systemclock={m.group(1)}&webEnv=PROD&isOnsite=false&inactiveTime=0"
                print(f"goto reg url")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        print(f"FINAL: {page.url}")
        print(f"OK: {'registration' in page.url}")
        await page.wait_for_timeout(20000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
