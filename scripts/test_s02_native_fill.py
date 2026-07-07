"""用已保存的 s02 HTML 验证原生表单填写逻辑"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.browser.icris_registration import IcrisRegistrationBot
from src.materials.packager import load_mock_data

HTML = ROOT / "data" / "debug" / "s02_s02_before_fill_20260706_134056.html"


async def main() -> None:
    if not HTML.exists():
        print(f"缺少调试 HTML: {HTML}")
        return

    html = HTML.read_text(encoding="utf-8")
    data = load_mock_data()
    bot = IcrisRegistrationBot()

    from playwright.async_api import async_playwright
    from src.browser.launcher import launch_browser

    async with async_playwright() as p:
        browser = await launch_browser(p)
        page = await browser.new_page()
        await page.set_content(html, wait_until="domcontentloaded")

        before = await bot._get_account_profile_status(page)
        filled = await bot._fill_account_profile_native(page, data)
        after = await bot._get_account_profile_status(page)

        print("before:", before)
        print("filled:", filled)
        print("after:", after)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
