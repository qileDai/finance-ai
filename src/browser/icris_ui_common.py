"""ICRIS 门户通用 UI（Cookie、简繁切换）。不含 s01-s05 账户登记业务逻辑。"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

_FORM_PAUSE_MS = 800


def is_cr_public_site(url: str) -> bool:
    """是否被重定向到公司注册处公开网站（非 e-services 子域）。"""
    host = urlparse(url).netloc.lower()
    if "e-services.cr.gov.hk" in host:
        return False
    return "cr.gov.hk" in host


async def wait_spin_clear(page: "Page", timeout_ms: int = 20000) -> bool:
    """等待全页 loading spinner 结束。"""
    try:
        spinning = await page.evaluate(
            "() => !!document.querySelector('.ant-spin-spinning')"
        )
        if not spinning:
            return True
        logger.info("等待页面 loading…")
        await page.wait_for_function(
            "() => !document.querySelector('.ant-spin-spinning')",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return True


async def wait_portal_ready(page: "Page", timeout_ms: int = 60000) -> bool:
    """等待 ICRIS3EP/3EF 门户 home.do 就绪（含登录表单或页头）。"""
    if is_cr_public_site(page.url):
        logger.error(
            "门户被重定向到公开站（反自动化/IP 限制）: %s — "
            "请使用 CDP 指纹浏览器并确保香港出口 IP",
            page.url,
        )
        return False

    try:
        await page.wait_for_url("**/e-services.cr.gov.hk/**", timeout=timeout_ms)
    except Exception:
        pass

    if is_cr_public_site(page.url):
        logger.error("门户 wait_for_url 后被重定向到公开站: %s", page.url)
        return False

    await wait_spin_clear(page, timeout_ms=min(20000, timeout_ms))

    try:
        await page.wait_for_function(
            """() => {
                const href = window.location.href || '';
                if (!href.includes('e-services.cr.gov.hk')) return false;
                if (href.includes('www.cr.gov.hk') && !href.includes('e-services')) return false;
                const body = document.body ? document.body.innerText : '';
                if (/載入中|Loading/i.test(body) && body.length < 200) return false;
                return document.querySelector(
                    "input[type='password'], input[placeholder*='用户'], input[placeholder*='用戶'], header, .header, #header"
                ) != null;
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        if is_cr_public_site(page.url):
            return False
        return "e-services.cr.gov.hk" in page.url


async def dismiss_google_translate(page: "Page") -> bool:
    """关闭 Google 翻译气泡/条（会遮挡 ICRIS 顶栏「简/繁」与菜单）。"""
    closed = False
    try:
        # 页面内嵌入的翻译条
        removed = await page.evaluate(
            """() => {
                let hit = false;
                for (const sel of [
                    '#google_translate_element',
                    '.goog-te-banner-frame',
                    '.goog-te-balloon-frame',
                    '.skiptranslate',
                    'iframe.goog-te-banner-frame',
                    'iframe.goog-te-menu-frame',
                ]) {
                    for (const el of document.querySelectorAll(sel)) {
                        el.remove();
                        hit = true;
                    }
                }
                const bar = document.querySelector('.goog-te-banner-frame, .VIpgJd-ZVi9od-ORHb');
                if (bar) {
                    const close = document.querySelector(
                        '.goog-close-link, .VIpgJd-ZVi9od-ORHb-OEVmcd'
                    );
                    if (close) { close.click(); hit = true; }
                }
                return hit;
            }"""
        )
        if removed:
            closed = True
            logger.info("已移除页面内 Google 翻译节点")
    except Exception:
        pass

    # Chrome 内置翻译弹层不在 DOM 内，Esc 可关掉
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
            closed = True
        except Exception:
            break
    if closed:
        logger.info("已尝试关闭 Google 翻译遮挡（Esc）")
    return closed


async def dismiss_cookie_banner(page: "Page") -> None:
    """关闭 Cookie 横幅（若存在）。"""
    cookie_btn = page.locator(
        ".cookie-banner button, .cookie-bar button, "
        "[class*='cookie'] button:has-text('接受')"
    ).first
    if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
        await cookie_btn.click()
        await page.wait_for_timeout(500)
        logger.info("已接受 Cookie 横幅")


async def dismiss_portal_modals(page: "Page") -> None:
    """关闭登录后可能出现的通知/智方便等弹窗。"""
    await dismiss_google_translate(page)
    close_selectors = [
        "#notification-modal .close",
        "#notification-modal button.close",
        "#notification-modal [data-dismiss='modal']",
        ".modal.show .btn-close",
        ".modal.show button.close",
        ".modal-dialog button.close",
        "[role='dialog'] button.close",
        "[aria-label='Close']",
    ]
    for sel in close_selectors:
        btn = page.locator(sel).first
        if await btn.count() > 0 and await btn.is_visible():
            await btn.click()
            await page.wait_for_timeout(500)
            logger.info("已关闭门户弹窗: %s", sel)
            return

    # 智方便+/通知类弹窗：点右上角 ×
    closed = await page.evaluate(
        """() => {
            for (const el of document.querySelectorAll('button, a, span, div')) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t !== '×' && t !== 'X' && t !== '✕') continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                const dialog = el.closest('.modal, [role=dialog], [class*=modal]');
                if (!dialog) continue;
                el.click();
                return true;
            }
            return false;
        }"""
    )
    if closed:
        logger.info("已关闭弹窗（×）")
        await page.wait_for_timeout(400)
        return

    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

    # 已有会话时再次登入的确认框
    body_text = ""
    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        pass
    if "尚未登出" in body_text or "重新登入" in body_text or "重新登录" in body_text:
        yes_btn = page.locator(
            "button:has-text('是'), input[type='button'][value='是'], "
            ".modal button:has-text('是'), [role='dialog'] button:has-text('是')"
        ).first
        if await yes_btn.count() > 0 and await yes_btn.is_visible():
            await yes_btn.click()
            await page.wait_for_timeout(1200)
            logger.info("已确认重新登入")


async def page_language_state(page: "Page") -> str:
    """返回 simplified | traditional | unknown"""
    url = (page.url or "").lower()
    if "locale=zh_tw" in url or "lang=zh_tw" in url:
        return "traditional"
    if "locale=zh_cn" in url or "lang=zh_cn" in url:
        return "simplified"
    return await page.evaluate(
        """() => {
            const text = document.body ? document.body.innerText : '';
            if (/用戶類別|擬訂用的服務|帳戶資料|公司註冊處|填寫用戶資料|英文姓氏|通訊語言|非香港地址|國家／地區|國家\\/地區|稱謂|實用資訊|電子服務/.test(text))
                return 'traditional';
            if (/用户类别|拟订用的服务|账户资料|公司注册处|填写用户资料|英文姓氏|通讯语言|非香港地址|国家\\/地区|称谓|实用资讯|电子服务/.test(text)
                && !/用戶類別|填寫用戶資料|通訊語言|稱謂|公司註冊處|實用資訊/.test(text))
                return 'simplified';
            const header = document.querySelector('header, .header, #header');
            const headerText = header ? header.innerText : '';
            if (/公司註冊處/.test(headerText)) return 'traditional';
            if (/公司注册处/.test(headerText)) return 'simplified';
            return 'unknown';
        }"""
    )


async def is_simplified_chinese_active(page: "Page") -> bool:
    state = await page_language_state(page)
    if state == "simplified":
        return True
    if state == "traditional":
        return False
    fan = page.locator("a").filter(has_text=re.compile(r"^繁$"))
    if await fan.count() > 0 and await fan.first.is_visible():
        return True
    return False


async def _wait_language_simplified(page: "Page", timeout_ms: int = 15000) -> bool:
    try:
        await page.wait_for_function(
            """() => {
                const text = document.body ? document.body.innerText : '';
                if (/用戶類別|擬訂用的服務|公司註冊處|首頁/.test(text)) return false;
                return /用户类别|拟订用的服务|公司注册处|首页|成立公司/.test(text);
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return await is_simplified_chinese_active(page)


async def _find_jian_link_info(page: "Page") -> dict | None:
    """定位页头语言切换「简」链接（兼容 ICRIS3EP 顶栏）。"""
    return await page.evaluate(
        """() => {
            const items = [];
            const textOk = (t) => ['简', '简体', '簡體', 'SC'].includes(t);
            for (const el of document.querySelectorAll('a, button, span, li, div')) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                const href = el.href || el.getAttribute('href') || '';
                const hit = textOk(t) || /locale=zh_CN|lang=zh_CN|zh-CN/i.test(href);
                if (!hit) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.top > 420) continue;
                const link = el.closest('a') || (el.tagName === 'A' ? el : null);
                const target = link || el;
                items.push({
                    tag: target.tagName,
                    href: target.href || target.getAttribute('href') || '',
                    top: r.top,
                    left: r.left,
                    cls: (target.className || '').slice(0, 80),
                    text: t.slice(0, 12),
                });
            }
            items.sort((a, b) => a.top - b.top || a.left - b.left);
            return items[0] || null;
        }"""
    )


async def _activate_jian_link(page: "Page", info: dict) -> str:
    """尝试多种方式触发「简」切换，返回使用的方法名。"""
    href = (info.get("href") or "").strip()
    # 登录后禁止 goto 换语言（会打断 session），仅允许 JS/Playwright 点击
    if href and not href.lower().startswith("javascript"):
        if "locale=" in href.lower() or "lang=" in href.lower():
            logger.debug("跳过语言 href goto，改用点击: %s", href[:80])
        else:
            await page.goto(href, wait_until="commit", timeout=60000)
            return f"goto:{href[:80]}"

    result = await page.evaluate(
        """() => {
            const textOk = (t) => ['简', '简体', '簡體', 'SC'].includes(t);
            for (const el of document.querySelectorAll('a, button, span, li, div')) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                const href = el.href || el.getAttribute('href') || '';
                if (!textOk(t) && !/locale=zh_CN|lang=zh_CN/i.test(href)) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.top > 420) continue;
                const target = el.closest('a') || el;
                target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                if (typeof target.click === 'function') target.click();
                return { ok: true, tag: target.tagName, text: t };
            }
            return { ok: false };
        }"""
    )
    if result and result.get("ok"):
        return f"js-click:{result.get('tag')}"

    loc = page.locator("a").filter(has_text=re.compile(r"^简$")).first
    if await loc.count() == 0:
        loc = page.get_by_text("简", exact=True).first
    await loc.scroll_into_view_if_needed()
    await loc.click(force=True, timeout=5000)
    return "playwright-force-click"


def _url_with_simplified_locale(url: str) -> str:
    parsed = urlparse(url)
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("locale", "lang", "request_locale", "language"):
        if key in qs:
            qs[key] = "zh_CN"
            break
    else:
        qs["locale"] = "zh_CN"
    return parsed._replace(query=urlencode(qs)).geturl()


async def _fallback_locale_url(page: "Page") -> bool:
    try:
        await page.context.add_cookies(
            [
                {
                    "name": "locale",
                    "value": "zh_CN",
                    "domain": "www.e-services.cr.gov.hk",
                    "path": "/",
                },
                {
                    "name": "lang",
                    "value": "zh_CN",
                    "domain": ".e-services.cr.gov.hk",
                    "path": "/",
                },
            ]
        )
    except Exception as e:
        logger.debug("设置 locale Cookie 失败: %s", e)

    locale_url = _url_with_simplified_locale(page.url)
    if locale_url != page.url:
        logger.info("回退：通过 URL 参数切换简体 %s", locale_url[:120])
        await page.goto(locale_url, wait_until="commit", timeout=60000)
        await page.wait_for_timeout(1500)
        if await _wait_language_simplified(page, timeout_ms=10000):
            return True
    return await is_simplified_chinese_active(page)


async def _open_header_tools(page: "Page") -> None:
    """展开页头右侧菜单（语言切换可能在内）。"""
    jian = page.locator("a").filter(has_text=re.compile(r"^简$")).first
    if await jian.count() > 0 and await jian.is_visible():
        return
    opened = await page.evaluate(
        """() => {
            const header = document.querySelector('header, .header, #header');
            if (!header) return false;
            const buttons = [...header.querySelectorAll('button, a[role=button]')];
            buttons.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
            for (const btn of buttons) {
                const r = btn.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                if (r.right < window.innerWidth - 200) continue;
                const label = (btn.innerText || btn.getAttribute('aria-label') || '').trim();
                if (/登出|logout|help|帮助|\\?/i.test(label)) continue;
                btn.click();
                return true;
            }
            return false;
        }"""
    )
    if opened:
        await page.wait_for_timeout(800)


async def ensure_simplified_chinese(page: "Page", *, allow_url_fallback: bool = False) -> bool:
    """切换为简体中文；已是简体则跳过。

    allow_url_fallback=False（默认）：登录后仅点击页头「简」，不做 URL/Cookie 回退导航。
    """
    await dismiss_google_translate(page)
    state = await page_language_state(page)
    if state == "simplified" or await is_simplified_chinese_active(page):
        logger.info("页面已是简体中文，跳过语言切换")
        return True

    await wait_spin_clear(page, timeout_ms=60000)
    try:
        await page.wait_for_function(
            "() => (document.body?.innerText || '').trim().length > 120",
            timeout=90000,
        )
    except Exception:
        logger.warning("页面正文加载较慢，继续尝试语言检测")
    await page.wait_for_timeout(800)

    state = await page_language_state(page)
    if state == "simplified" or await is_simplified_chinese_active(page):
        logger.info("页面已是简体中文，跳过语言切换")
        return True

    info = await _find_jian_link_info(page)
    if not info:
        await _open_header_tools(page)
        info = await _find_jian_link_info(page)
    if not info:
        logger.warning("未找到页头「简」链接 (state=%s)", state)
        if allow_url_fallback:
            return await _fallback_locale_url(page)
        return False

    logger.info(
        "找到「简」入口: text=%s tag=%s href=%s",
        info.get("text"),
        info.get("tag"),
        (info.get("href") or "")[:100],
    )

    for attempt in range(1, 4):
        try:
            method = await _activate_jian_link(page, info)
            logger.info("已触发「简」切换 (尝试 %d/3, 方式=%s)", attempt, method)
            if await _wait_language_simplified(page, timeout_ms=12000):
                logger.info("语言已切换为简体中文")
                return True
            await page.wait_for_timeout(_FORM_PAUSE_MS)
        except Exception as e:
            logger.warning("「简」切换尝试 %d 失败: %s", attempt, e)
            await page.wait_for_timeout(500)

    logger.warning("点击「简」后页面仍为繁体 (state=%s)", await page_language_state(page))
    if allow_url_fallback:
        return await _fallback_locale_url(page)
    return False
