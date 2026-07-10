"""调试 s03 填写用户资料页 DOM 与填写逻辑"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.browser.icris_registration import IcrisRegistrationBot
from src.materials.packager import load_mock_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("debug_s03")

OUT_DIR = ROOT / "data" / "debug"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DOM_PROBE_JS = """() => {
    const visible = el => {
        if (!el) return false;
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    };
    const body = document.body ? document.body.innerText : '';
    const inputs = [...document.querySelectorAll('input, textarea, select')].map(el => ({
        tag: el.tagName,
        type: el.type || '',
        id: el.id || '',
        name: el.name || '',
        placeholder: el.placeholder || '',
        value: el.type === 'password' ? (el.value ? '***' : '') : (el.value || '').slice(0, 50),
        visible: visible(el),
        label: (() => {
            if (el.id) {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                if (lbl) return (lbl.innerText || '').trim().slice(0, 60);
            }
            const item = el.closest('.ant-form-item, fieldset, .form-group');
            const lbl = item && item.querySelector('.ant-form-item-label, label, .rowTitle, th');
            return lbl ? (lbl.innerText || '').trim().slice(0, 60) : '';
        })(),
    })).filter(x => x.visible);
    const labels = [...document.querySelectorAll('label, .ant-form-item-label, .rowTitle, th')].map(
        el => (el.innerText || '').trim().slice(0, 80)
    ).filter(Boolean).slice(0, 40);
    return {
        url: location.href,
        title: document.title,
        bodySnippet: body.slice(0, 1200),
        isUserInfo: /填写用户资料|填寫用戶資料|步骤2|步驟2/.test(body),
        isAccountProfile: /填写帐户资料|填寫帳戶資料|用户类别/.test(body),
        labels,
        inputs: inputs.slice(0, 60),
        selects: [...document.querySelectorAll('select')].map(s => ({
            id: s.id, name: s.name,
            options: [...s.options].map(o => ({ value: o.value, text: (o.textContent||'').trim() })).slice(0, 15),
        })),
    };
}"""


async def dump_page_state(page, label: str) -> dict:
    state = await page.evaluate(DOM_PROBE_JS)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"s03_{label}_{ts}.json"
    html_path = OUT_DIR / f"s03_{label}_{ts}.html"
    png_path = OUT_DIR / f"s03_{label}_{ts}.png"
    json_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(png_path), full_page=True)
    logger.info("已保存: %s", json_path)
    return state


async def main() -> None:
    data = load_mock_data()
    bot = IcrisRegistrationBot()

    from playwright.async_api import async_playwright
    from src.browser.launcher import create_browser_context, launch_browser

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()

        page = await bot._navigate_to_registration(page)
        if not page:
            logger.error("无法进入注册页")
            await browser.close()
            return

        await bot._ensure_simplified_chinese(page)

        terms_ok = False
        for rnd in range(1, 4):
            if await bot._fill_captcha(page):
                terms_ok = await bot._accept_terms(page)
                if terms_ok:
                    break
            from src.browser.icris_captcha import _reload_captcha
            await _reload_captcha(page)

        if not terms_ok:
            logger.error("条款未通过")
            await browser.close()
            return

        await bot._wait_for_account_profile_step(page, timeout_ms=120000)
        await bot._ensure_simplified_chinese(page)

        logger.info("=== s02 账户资料 ===")
        await bot._fill_user_profile_step(page, data)

        logger.info("=== 等待 s03 用户资料 ===")
        try:
            await page.wait_for_url("**/registration/s03**", timeout=60000)
        except Exception:
            pass
        await bot._wait_registration_vue(page, timeout=90000)
        for _ in range(30):
            state = await page.evaluate(DOM_PROBE_JS)
            if state.get("inputs") and state.get("isUserInfo"):
                break
            await page.wait_for_timeout(1000)
        await page.wait_for_timeout(1500)
        state = await dump_page_state(page, "user_info_before_fill")

        logger.info("=== 尝试填写用户资料 ===")
        filled = await bot._fill_user_info_step(page, data)
        logger.info("填写结果: %d", filled)

        await dump_page_state(page, "user_info_after_fill")

        logger.info("浏览器保持 120 秒…")
        await page.wait_for_timeout(120000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
