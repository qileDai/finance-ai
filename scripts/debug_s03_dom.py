"""Probe s03 select-like elements after 本地地址."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOM_PROBE = """() => {
    const vis = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const body = document.body.innerText || '';
    return {
        url: location.href,
        title: document.title,
        hasUserInfo: /填寫用戶資料|填写用户资料/.test(body),
        antSelect: document.querySelectorAll('.ant-select').length,
        combobox: [...document.querySelectorAll('[role=combobox]')].filter(vis).length,
        nativeSelect: [...document.querySelectorAll('select')].filter(vis).length,
        dropdownTriggers: [...document.querySelectorAll(
            '.ant-select-selector, .dropdown-toggle, .custom-select, .form-control'
        )].filter(vis).slice(0, 15).map(el => ({
            tag: el.tagName,
            cls: (el.className || '').slice(0, 80),
            txt: (el.innerText || '').trim().slice(0, 50),
            ph: el.getAttribute('placeholder') || '',
        })),
        labels: [...document.querySelectorAll('label, .ant-form-item-label, .rowTitle')].map(
            el => (el.innerText || '').trim().slice(0, 80)
        ).filter(t => t && /通訊|郵遞|區|语言|語言|地址/.test(t)).slice(0, 20),
        bodyLen: body.length,
    };
}"""


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

        page = await bot._navigate_to_registration(page)
        await bot._ensure_simplified_chinese(page)
        for _ in range(3):
            if await bot._fill_captcha(page) and await bot._accept_terms(page):
                break
            from src.browser.icris_captcha import _reload_captcha
            await _reload_captcha(page)

        await bot._fill_user_profile_step(page, data)
        try:
            await page.wait_for_url("**/registration/s03**", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        await page.evaluate(
            "(() => { const w = document.querySelector('#formWrapper, .formWrapper, main'); if (w) w.scrollTop = w.scrollHeight; window.scrollTo(0, document.body.scrollHeight); })()"
        )
        await page.wait_for_timeout(1500)

        print("=== BEFORE LOCAL ADDRESS ===")
        print(json.dumps(await page.evaluate(DOM_PROBE), ensure_ascii=False, indent=2))

        await bot._select_radio_in_section(page, "地址", r"本地地址|本地")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)

        print("\n=== AFTER LOCAL ADDRESS ===")
        print(json.dumps(await page.evaluate(DOM_PROBE), ensure_ascii=False, indent=2))

        await bot._log_ant_selects(page, "probe")
        Path(ROOT / "data/debug/s03_dom_probe.json").write_text(
            json.dumps(await page.evaluate(DOM_PROBE), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await page.screenshot(path=str(ROOT / "data/debug/s03_probe.png"), full_page=True)
        await page.wait_for_timeout(10000)
        await close_browser_session(browser, external_cdp=via_cdp)


if __name__ == "__main__":
    asyncio.run(main())
