"""Probe ICRIS s01 registration page DOM after load."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from src.browser.launcher import create_browser_context, launch_browser
from src.browser.icris_registration import (
    IcrisRegistrationBot,
    PORTAL_URL,
    build_registration_url,
)


async def main() -> None:
    bot = IcrisRegistrationBot()
    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()
        await page.goto(PORTAL_URL, wait_until="commit", timeout=45000)
        await bot._wait_portal_session(page)
        clock = await bot._wait_systemclock_in_url(page) or bot._extract_systemclock(page.url)
        print("systemclock:", clock)
        reg_url = build_registration_url(clock)
        print("reg_url:", reg_url)
        await page.goto(reg_url, wait_until="commit", timeout=45000)
        for wait in (5, 15, 30, 45):
            await page.wait_for_timeout(wait * 1000)
            probe = await bot._registration_page_probe(page)
            extra = await page.evaluate(
                """() => ({
                    title: document.title,
                    htmlLen: document.documentElement?.outerHTML?.length || 0,
                    bodyLen: document.body?.innerHTML?.length || 0,
                    frameCount: window.frames.length,
                    scripts: [...document.querySelectorAll('script[src]')].slice(0,8).map(s => s.src),
                    appRoot: !!document.querySelector('#app, #root, [id*=app]'),
                })"""
            )
            probe.update(extra)
            print(f"--- after {wait}s cumulative ---")
            print(json.dumps(probe, ensure_ascii=False, indent=2))
            if probe.get("checkCode") or probe.get("checkbox"):
                break
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
