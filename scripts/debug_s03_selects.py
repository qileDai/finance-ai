"""Probe all ant-select elements on s03 page."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROBE = """() => {
    const nearLabel = (el) => {
        let p = el;
        for (let i = 0; i < 8 && p; i++) {
            const lbl = p.querySelector(':scope > label, :scope > .ant-form-item-label, :scope > .rowTitle, :scope > .col-form-label');
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
            const prev = p.previousElementSibling;
            if (prev && prev.innerText.trim().length < 80) return prev.innerText.trim();
            p = p.parentElement;
        }
        return '';
    };
    return [...document.querySelectorAll('.ant-select')].map((sel, i) => ({
        i,
        id: sel.id,
        className: sel.className.slice(0, 80),
        text: sel.innerText.trim().slice(0, 60),
        placeholder: sel.querySelector('.ant-select-selection-placeholder')?.innerText?.trim() || '',
        selected: sel.querySelector('.ant-select-selection-item')?.innerText?.trim() || '',
        nearLabel: nearLabel(sel),
        parentTag: sel.parentElement?.tagName,
        parentClass: (sel.parentElement?.className || '').slice(0, 60),
    }));
}"""


async def run_to_s03(page, bot, data):
    page = await bot._navigate_to_registration(page)
    await bot._ensure_simplified_chinese(page)
    for _ in range(3):
        if await bot._fill_captcha(page) and await bot._accept_terms(page):
            break
        from src.browser.icris_captcha import _reload_captcha
        await _reload_captcha(page)
    await bot._fill_user_profile_step(page, data)
    await bot._wait_for_user_info_form(page, timeout_ms=90000)
    await bot._ensure_traditional_chinese(page)
    for _ in range(30):
        n = await page.locator(".ant-select").count()
        if n >= 2:
            break
        await page.wait_for_timeout(1000)
    await page.wait_for_timeout(2000)


async def main() -> None:
    from playwright.async_api import async_playwright
    from src.browser.icris_registration import IcrisRegistrationBot
    from src.browser.launcher import close_browser_session, create_browser_context, launch_browser
    from src.materials.packager import load_mock_data
    from config.settings import settings

    bot = IcrisRegistrationBot()
    data = load_mock_data()

    async with async_playwright() as p:
        browser = await launch_browser(p)
        via_cdp = bool(settings.chrome_use_existing and browser.contexts)
        context = await create_browser_context(browser)
        page = await context.new_page()
        await run_to_s03(page, bot, data)

        selects = await page.evaluate(PROBE)
        print(json.dumps(selects, ensure_ascii=False, indent=2))
        print("count:", len(selects))

        # click each select and dump options
        for i in range(min(len(selects), 8)):
            loc = page.locator(".ant-select").nth(i)
            await loc.scroll_into_view_if_needed()
            await loc.click(force=True)
            await page.wait_for_timeout(800)
            opts = await page.evaluate("""() => [...document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
            )].map(o => o.innerText.trim()).slice(0, 20)""")
            print(f"\n--- select[{i}] label={selects[i].get('nearLabel','')!r} opts={opts[:8]}")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        Path(ROOT / "data/debug/s03_selects_probe.json").write_text(
            json.dumps(selects, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await page.wait_for_timeout(5000)
        await close_browser_session(browser, external_cdp=via_cdp)


if __name__ == "__main__":
    asyncio.run(main())
