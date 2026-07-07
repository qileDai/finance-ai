"""调试 s02 账户资料页 DOM 与下拉选择逻辑"""
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
logger = logging.getLogger("debug_s02")

OUT_DIR = ROOT / "data" / "debug"
OUT_DIR.mkdir(parents=True, exist_ok=True)


DOM_PROBE_JS = """() => {
    const norm = s => (s || '').replace(/[\\s*:：]+/g, '').trim();
    const visible = el => {
        if (!el) return false;
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    };

    const formItems = [...document.querySelectorAll('.ant-form-item, .form-group, fieldset')].map((item, idx) => {
        const label = item.querySelector('.ant-form-item-label, label, .control-label, th, .label');
        const select = item.querySelector('.ant-select');
        const checkboxes = [...item.querySelectorAll('.ant-checkbox-wrapper')].map(c => ({
            text: (c.innerText || '').trim().slice(0, 60),
            checked: c.classList.contains('ant-checkbox-wrapper-checked'),
        }));
        const radios = [...item.querySelectorAll('.ant-radio-wrapper')].map(r => ({
            text: (r.innerText || '').trim().slice(0, 60),
            checked: r.classList.contains('ant-radio-wrapper-checked'),
        }));
        const inputs = [...item.querySelectorAll('input, textarea')].map(inp => ({
            type: inp.type || inp.tagName,
            name: inp.name || '',
            id: inp.id || '',
            placeholder: inp.placeholder || '',
            value: inp.type === 'password' ? (inp.value ? '***' : '') : (inp.value || '').slice(0, 40),
            visible: visible(inp),
        }));
        return {
            idx,
            label: label ? (label.innerText || '').trim().slice(0, 80) : '',
            hasSelect: !!select,
            selectHtml: select ? select.outerHTML.slice(0, 500) : '',
            selectText: select ? (select.innerText || '').trim().slice(0, 120) : '',
            selectClasses: select ? select.className : '',
            checkboxes,
            radios,
            inputs,
            itemText: (item.innerText || '').trim().slice(0, 200),
        };
    });

    const selects = [...document.querySelectorAll('.ant-select, [role="combobox"], select')].map((sel, idx) => ({
        idx,
        tag: sel.tagName,
        classes: sel.className,
        role: sel.getAttribute('role') || '',
        text: (sel.innerText || '').trim().slice(0, 120),
        placeholder: (sel.querySelector('.ant-select-selection-placeholder') || {}).innerText || '',
        selected: (sel.querySelector('.ant-select-selection-item') || {}).innerText || '',
        html: sel.outerHTML.slice(0, 600),
        visible: visible(sel),
    }));

    const dropdowns = [...document.querySelectorAll('.ant-select-dropdown, .rc-select-dropdown, [role="listbox"]')].map((dd, idx) => ({
        idx,
        classes: dd.className,
        hidden: dd.classList.contains('ant-select-dropdown-hidden'),
        visible: visible(dd),
        options: [...dd.querySelectorAll('[role="option"], .ant-select-item-option, .ant-select-item, li')].map(o => ({
            text: (o.innerText || '').trim().slice(0, 40),
            title: o.getAttribute('title') || '',
            classes: o.className,
        })).slice(0, 20),
    }));

    const markers = {
        url: location.href,
        title: document.title,
        bodySnippet: (document.body.innerText || '').slice(0, 800),
        hasUserCategory: /用户类别|用戶類別/.test(document.body.innerText || ''),
        hasAccountProfile: /账户资料|帳戶資料|拟订用的服务|擬訂用的服務/.test(document.body.innerText || ''),
    };

    return { markers, formItems: formItems.slice(0, 20), selects: selects.slice(0, 15), dropdowns: dropdowns.slice(0, 10) };
}"""


async def dump_page_state(page, label: str) -> dict:
    state = await page.evaluate(DOM_PROBE_JS)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"s02_{label}_{ts}.json"
    html_path = OUT_DIR / f"s02_{label}_{ts}.html"
    png_path = OUT_DIR / f"s02_{label}_{ts}.png"

    json_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(png_path), full_page=True)

    logger.info("已保存调试产物: %s", json_path)
    logger.info("已保存截图: %s", png_path)
    logger.info("页面状态: url=%s userCategory=%s", state["markers"]["url"], state["markers"].get("hasUserCategory"))
    return state


async def try_select_strategies(page, bot: IcrisRegistrationBot) -> None:
    logger.info("=== 尝试用户类别选择策略 ===")
    before = await bot._verify_user_category_individual(page)
    logger.info("选择前 个人已选=%s", before)

    strategies = [
        ("open_user_category_dropdown + pick", _try_open_and_pick),
        ("select_user_category_individual", bot._select_user_category_individual),
        ("select_ant_dropdown_by_label", lambda p: bot._select_ant_dropdown_by_label(p, r"用户类别|用戶類別", r"个人|個人")),
    ]
    for name, fn in strategies:
        logger.info("--- 策略: %s ---", name)
        try:
            ok = await fn(page)
            after = await bot._verify_user_category_individual(page)
            status = await bot._get_account_profile_status(page)
            logger.info("策略 %s => ok=%s, 个人已选=%s, 状态=%s", name, ok, after, status)
            if after:
                logger.info("成功策略: %s", name)
                return
        except Exception as exc:
            logger.exception("策略 %s 异常: %s", name, exc)
        await dump_page_state(page, f"after_{name}")
        await page.wait_for_timeout(800)

    logger.warning("所有策略均未选中「个人」")


async def _try_open_and_pick(page) -> bool:
    bot = IcrisRegistrationBot()
    if not await bot._open_user_category_dropdown(page):
        return False
    await page.wait_for_timeout(800)
    await dump_page_state(page, "dropdown_opened")
    return await bot._pick_select_option_individual(page)


async def main() -> None:
    data = load_mock_data()
    bot = IcrisRegistrationBot()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("请先安装 playwright")
        return

    async with async_playwright() as p:
        from src.browser.launcher import create_browser_context, launch_browser

        browser = await launch_browser(p)
        context = await create_browser_context(browser)
        page = await context.new_page()

        logger.info("=== 1. 导航到注册页 ===")
        page = await bot._navigate_to_registration(page)
        if not page:
            logger.error("无法进入注册页")
            await browser.close()
            return

        await bot._ensure_simplified_chinese(page)
        await dump_page_state(page, "s01_terms")

        logger.info("=== 2. 验证码 + 条款 ===")
        terms_ok = False
        for rnd in range(1, 4):
            filled = await bot._fill_captcha(page)
            logger.info("验证码填写: %s (轮次 %d)", filled, rnd)
            if not filled:
                continue
            terms_ok = await bot._accept_terms(page)
            if terms_ok:
                break
            from src.browser.icris_captcha import _reload_captcha
            await _reload_captcha(page)

        if not terms_ok:
            logger.error("条款页未通过，请在浏览器手动完成验证码/条款后按回车继续")
            input("按 Enter 继续调试...")

        logger.info("=== 3. 等待 s02 账户资料页 ===")
        await bot._wait_for_account_profile_step(page, timeout_ms=120000)
        await bot._wait_for_account_form_ready(page, timeout_ms=60000)
        await bot._ensure_simplified_chinese(page)

        state = await dump_page_state(page, "s02_before_fill")
        if not state["markers"]["hasUserCategory"]:
            logger.warning("页面未检测到「用户类别」，当前可能不在 s02")
            input("请手动导航到账户资料页后按 Enter...")

        await try_select_strategies(page, bot)
        await dump_page_state(page, "s02_after_strategies")

        logger.info("=== 4. 尝试完整填写账户资料 ===")
        filled = await bot._fill_user_profile_step(page, data)
        status = await bot._get_account_profile_status(page)
        logger.info("完整填写结果: filled=%d, status=%s", filled, status)
        await dump_page_state(page, "s02_after_full_fill")

        logger.info("浏览器保持打开 120 秒供手动检查...")
        await page.wait_for_timeout(120000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
