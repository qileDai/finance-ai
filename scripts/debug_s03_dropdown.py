"""Debug s03 dropdown fields only — district + language."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("debug_s03_dropdown")

PROBE_SELECTS = """() => {
    const norm = s => (s || '').replace(/[\\s/／:*：]/g, '');
    return [...document.querySelectorAll('.ant-form-item')].map((el, idx) => {
        const lbl = el.querySelector('.ant-form-item-label, label');
        const label = lbl ? (lbl.innerText || '').trim() : '';
        const sel = el.querySelector('.ant-select');
        const selected = el.querySelector('.ant-select-selection-item');
        const placeholder = el.querySelector('.ant-select-selection-placeholder');
        const search = el.querySelector('.ant-select-selection-search-input');
        const native = el.querySelector('select');
        return {
            idx,
            label,
            normLabel: norm(label),
            hasAntSelect: !!sel,
            selected: selected ? selected.innerText.trim() : '',
            placeholder: placeholder ? placeholder.innerText.trim() : '',
            hasSearchInput: !!search,
            nativeSelect: native ? [...native.options].slice(0,5).map(o => o.textContent.trim()) : [],
        };
    }).filter(x => x.label || x.hasAntSelect);
}"""


async def main() -> None:
    from playwright.async_api import async_playwright
    from src.browser.icris_registration import IcrisRegistrationBot
    from src.browser.launcher import close_browser_session, create_browser_context, launch_browser
    from src.materials.packager import load_mock_data

    bot = IcrisRegistrationBot()
    data = load_mock_data()

    async with async_playwright() as p:
        browser = await launch_browser(p)
        via_cdp = bool(getattr(__import__("config.settings", fromlist=["settings"]).settings, "chrome_use_existing", False) and browser.contexts)
        context = await create_browser_context(browser)
        page = await context.new_page()

        page = await bot._navigate_to_registration(page)
        if not page:
            return
        await bot._ensure_simplified_chinese(page)
        for rnd in range(1, 4):
            if await bot._fill_captcha(page) and await bot._accept_terms(page):
                break
            from src.browser.icris_captcha import _reload_captcha
            await _reload_captcha(page)

        await bot._fill_user_profile_step(page, data)
        await bot._wait_for_user_info_form(page, timeout_ms=60000)
        try:
            await bot._ensure_traditional_chinese(page)
        except Exception as e:
            logger.warning("语言切换跳过: %s", e)
        await bot._wait_for_ant_selects(page, min_count=2, timeout_ms=20000)
        await page.wait_for_timeout(2000)

        items = await page.evaluate(PROBE_SELECTS)
        print("=== FORM ITEMS ===")
        print(json.dumps(items, ensure_ascii=False, indent=2))

        for label_pat, opt, name in [
            (["郵遞區號", "邮递区号", "區/市", "区/市"], "香港仔", "district"),
            (["通訊語言", "通讯语言"], "English", "language"),
        ]:
            print(f"\n=== TRY {name}: {opt} ===")
            ok = await bot._select_ant_select_by_keywords(page, label_pat, opt)
            print(f"result: {ok}")
            await page.wait_for_timeout(1000)
            items2 = await page.evaluate(PROBE_SELECTS)
            for it in items2:
                if "郵遞" in it["label"] or "通訊" in it["label"] or "邮递" in it["label"] or "通讯" in it["label"]:
                    print(json.dumps(it, ensure_ascii=False))

        await page.wait_for_timeout(30000)
        await close_browser_session(browser, external_cdp=via_cdp)


if __name__ == "__main__":
    asyncio.run(main())
