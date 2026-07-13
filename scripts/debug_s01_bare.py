"""Probe s01 with no stealth routing."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from src.browser.icris_registration import PORTAL_URL, build_registration_url, IcrisRegistrationBot


async def main() -> None:
    bot = IcrisRegistrationBot()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()
        failed = []
        page.on("requestfailed", lambda r: failed.append(f"{r.url[:100]} {r.failure}"))

        await page.goto(PORTAL_URL, wait_until="commit", timeout=45000)
        await bot._wait_portal_session(page)
        clock = await bot._wait_systemclock_in_url(page) or bot._extract_systemclock(page.url)
        await page.goto(build_registration_url(clock), wait_until="commit", timeout=45000)
        await page.wait_for_timeout(20000)
        probe = await bot._registration_page_probe(page)
        extra = await page.evaluate(
            """() => ({
                scriptCount: document.querySelectorAll('script[src]').length,
                bodyLen: document.body?.innerHTML?.length || 0,
            })"""
        )
        probe.update(extra)
        print(json.dumps(probe, ensure_ascii=False, indent=2))
        print("failed:", failed[:10])
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
