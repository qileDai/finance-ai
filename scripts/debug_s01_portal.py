"""Compare portal-click vs direct URL for s01 load; log failed requests."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from src.browser.launcher import create_browser_context, launch_browser
from src.browser.icris_registration import IcrisRegistrationBot, PORTAL_URL


async def probe_page(page, bot, label: str) -> None:
    p = await bot._registration_page_probe(page)
    extra = await page.evaluate(
        """() => ({
            title: document.title,
            htmlLen: document.documentElement?.outerHTML?.length || 0,
            bodyLen: document.body?.innerHTML?.length || 0,
            scriptCount: document.querySelectorAll('script[src]').length,
            allScripts: [...document.querySelectorAll('script[src]')].map(s => s.src),
        })"""
    )
    p.update(extra)
    print(f"\n=== {label} ===")
    print(json.dumps(p, ensure_ascii=False, indent=2))


async def main() -> None:
    bot = IcrisRegistrationBot()
    failed: list[str] = []

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()

        def on_fail(req):
            failed.append(f"{req.failure} {req.url[:120]}")

        page.on("requestfailed", on_fail)
        page.on("console", lambda msg: print(f"CONSOLE {msg.type}: {msg.text[:200]}") if msg.type == "error" else None)

        # A: portal click
        await page.goto(PORTAL_URL, wait_until="load", timeout=60000)
        await bot._wait_portal_session(page)
        await bot._dismiss_portal_overlays(page)
        btn = await bot._wait_portal_register_control(page, timeout=20000)
        if btn:
            try:
                async with context.expect_page(timeout=10000) as pi:
                    await btn.click()
                page = await pi.value
            except Exception:
                await btn.click()
            await page.wait_for_load_state("load", timeout=60000)
            await page.wait_for_timeout(10000)
            await probe_page(page, bot, "portal click + 10s")
        else:
            print("no register button")

        print("\n--- failed requests (portal) ---")
        for f in failed[:20]:
            print(f)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
