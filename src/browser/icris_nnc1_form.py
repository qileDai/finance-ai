"""ICRIS3EP 已激活账号：NNC1 公司填表（与 ICRIS3EF s01-s05 账户登记分离）。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT, settings
from src.browser.icris_ui_common import (
    dismiss_cookie_banner,
    dismiss_google_translate,
    dismiss_portal_modals,
    ensure_simplified_chinese,
    is_cr_public_site,
    wait_portal_ready,
    wait_spin_clear,
)
from src.browser.launcher import create_browser_context, launch_browser
from src.email.imap_client import IcrisAccount

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.e-services.cr.gov.hk/ICRIS3EP/system/home.do?webEnv=PROD"

# 菜单文案：半角/全角括号、NNC 与 1 之间可有可无空格
NNC1_MENU_LABEL_PAT = re.compile(
    r"股份有限公司[（(]\s*表格\s*NNC\s*1\s*[）)]",
    re.I,
)

# NNC1 底栏：储存/存储/儲存及继续（ICRIS 页面可能为繁体或混用）
NNC1_SAVE_CONTINUE_PAT = re.compile(
    r"(储存|存储|儲存)及(继续|繼續)|Save\s*(?:and|&)\s*Continue",
    re.I,
)


class IcrisNnc1FormBot:
    """阶段 3：已激活账号登录 ICRIS3EP → NNC1 填表（不最终提交）。"""

    def __init__(self) -> None:
        self.dry_run = settings.dry_run

    async def _maybe_screenshot(self, page, label: str) -> str:
        shot_dir = PROJECT_ROOT / "data" / "icris_form_screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = shot_dir / f"nnc1_{label}_{stamp}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            logger.info("NNC1 截图 [%s]: %s", label, path)
        except Exception as e:
            logger.warning("截图失败 [%s]: %s", label, e)
        return str(path)

    async def _login(self, page, account: IcrisAccount) -> None:
        logger.info("ICRIS3EP 登录: %s", account.username)

        if await self._is_logged_in(page):
            logger.info("检测到已有登录会话，跳过登入")
            await dismiss_portal_modals(page)
            return

        for sel in [
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[placeholder*='用户' i]",
            "input[placeholder*='用戶' i]",
            "#username",
            "#userId",
        ]:
            inp = page.locator(sel).first
            if await inp.count() > 0 and await inp.is_visible():
                await inp.fill(account.username)
                break

        for sel in ["input[type='password']", "input[name*='pass' i]", "input[id*='pass' i]"]:
            inp = page.locator(sel).first
            if await inp.count() > 0 and await inp.is_visible():
                await inp.fill(account.password)
                break

        captcha = page.locator("#checkCode").first
        if await captcha.count() > 0 and await captcha.is_visible():
            from src.browser.icris_captcha import fill_captcha
            from src.llm.openai_client import LLMClient

            await fill_captcha(page, LLMClient())

        await self._click_password_login_button(page)
        await dismiss_portal_modals(page)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        logger.info("已点击密码登入")

    async def _click_password_login_button(self, page) -> None:
        """点击用户名/密码区域的登入按钮，排除「以数码证书登入」「智方便」等其它入口。

        登入按钮文案可能变动，优先按密码框附近位置定位，文字仅作辅助。
        """
        exclude_pat = (
            r"数码证书|數碼證書|digital\s*certificate|智方便|iAM\s*Smart|iam\s*smart|"
            r"忘记|忘記|forgot|了解更多|learn\s*more|其他方式"
        )
        login_pat = r"登[录入]|登录|login|submit|进入|進入"

        result = await page.evaluate(
            """({ excludeSrc, loginSrc }) => {
                const exclude = new RegExp(excludeSrc, 'i');
                const loginPat = new RegExp(loginSrc, 'i');
                const pwd = document.querySelector("input[type='password']");
                if (!pwd) return { ok: false, reason: 'no_password' };

                let container = pwd.closest('form');
                if (!container) {
                    let node = pwd.parentElement;
                    for (let i = 0; i < 12 && node && node !== document.body; i++) {
                        const hasPwd = node.querySelector("input[type='password']");
                        const btns = node.querySelectorAll(
                            'button, input[type="submit"], input[type="button"], a[role="button"]'
                        );
                        if (hasPwd && btns.length > 0) {
                            container = node;
                            break;
                        }
                        node = node.parentElement;
                    }
                }
                if (!container) container = document.body;

                const pwdRect = pwd.getBoundingClientRect();
                const containerRect = container.getBoundingClientRect();
                const candidates = [];

                for (const el of container.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], a[role="button"], a.btn, .btn'
                )) {
                    const text = (
                        el.innerText || el.value || el.getAttribute('aria-label') ||
                        el.title || el.textContent || ''
                    ).replace(/\\s+/g, ' ').trim();
                    if (!text) continue;
                    if (exclude.test(text)) continue;

                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < pwdRect.top - 10) continue;
                    // 底部通栏大按钮通常是「数码证书登入」，排除
                    if (r.width > containerRect.width * 0.75 && r.top > pwdRect.bottom + 120) {
                        continue;
                    }

                    const distY = Math.abs(r.top - (pwdRect.bottom + 36));
                    const distX = Math.abs(r.left - pwdRect.right);
                    let score = distY + distX * 0.15;
                    if (loginPat.test(text)) score -= 80;
                    candidates.push({ el, score, text: text.slice(0, 48) });
                }

                // 若按文字未命中，放宽：密码框下方 200px 内第一个可见按钮（仍排除证书/智方便）
                if (!candidates.length) {
                    for (const el of container.querySelectorAll('button, input[type="submit"], input[type="button"]')) {
                        const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
                        if (!text || exclude.test(text)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top < pwdRect.top || r.top > pwdRect.bottom + 200) continue;
                        if (r.width > containerRect.width * 0.75) continue;
                        candidates.push({ el, score: r.top - pwdRect.bottom, text: text.slice(0, 48) });
                    }
                }

                candidates.sort((a, b) => a.score - b.score);
                if (!candidates.length) return { ok: false, reason: 'no_candidate' };

                const target = candidates[0].el;
                target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                if (typeof target.click === 'function') target.click();
                return { ok: true, text: candidates[0].text };
            }""",
            {"excludeSrc": exclude_pat, "loginSrc": login_pat},
        )

        if result and result.get("ok"):
            logger.info("已点击密码登入按钮: %s", result.get("text"))
            return

        raise RuntimeError("未找到用户名/密码区域的登入按钮（已排除数码证书登入）")

    async def _is_logged_in(self, page) -> bool:
        logout = page.locator(
            "a:has-text('登出'), a:has-text('Logout'), button:has-text('登出')"
        ).first
        if await logout.count() > 0 and await logout.is_visible():
            return True
        return await page.evaluate(
            """() => {
                const text = document.body ? document.body.innerText : '';
                return /登出|Logout/i.test(text)
                    && /成立公司|电子服务|电子服務|最新消息|实用资讯|實用資訊/.test(text);
            }"""
        )

    async def _wait_login_complete(self, page) -> None:
        """等待密码登入完成：顶栏出现「登出」即视为成功（登录表单可能仍在 DOM）。"""
        logger.info("等待登录完成…")
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    return /登出|Logout/i.test(text)
                        && /成立公司|电子服务|电子服務|最新消息|实用资讯|實用資訊/.test(text);
                }""",
                timeout=90000,
            )
        except Exception as e:
            if not await self._is_logged_in(page):
                raise RuntimeError(f"登录未完成: {e}") from e
            logger.warning("登录检测超时但已见登出链接，继续: %s", e)
        await wait_spin_clear(page, timeout_ms=30000)
        await page.wait_for_timeout(1000)

    async def _wait_dashboard(self, page) -> None:
        if not await self._is_logged_in(page):
            raise RuntimeError("未检测到已登录状态（无「登出」链接）")
        await dismiss_portal_modals(page)
        await wait_spin_clear(page, timeout_ms=60000)
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body?.innerText || '';
                    return text.trim().length > 150
                        && (/成立公司|登出|电子服务|电子服務|Incorporation|Information/i.test(text));
                }""",
                timeout=90000,
            )
        except Exception as e:
            logger.warning("Dashboard 正文加载超时，继续: %s", e)
        await page.wait_for_timeout(1500)
        logger.info("Dashboard 已加载: %s", page.url[:120])

    async def _maximize_browser_window(self, page) -> None:
        """NNC1 顶栏下拉菜单需要宽屏；最大化 Chrome 窗口。"""
        try:
            session = await page.context.new_cdp_session(page)
            target_info = await session.send("Target.getTargetInfo")
            target_id = target_info.get("targetInfo", {}).get("targetId")
            if target_id:
                win = await session.send(
                    "Browser.getWindowForTarget", {"targetId": target_id}
                )
                window_id = win.get("windowId")
                if window_id:
                    await session.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"windowState": "maximized"}},
                    )
                    logger.info("Chrome 窗口已最大化 (CDP)")
                    await page.wait_for_timeout(800)
                    return
        except Exception as e:
            logger.debug("CDP 最大化失败: %s", e)
        try:
            await page.evaluate(
                """() => {
                    if (window.screen?.availWidth) {
                        window.moveTo(0, 0);
                        window.resizeTo(window.screen.availWidth, window.screen.availHeight);
                    }
                }"""
            )
            logger.info("Chrome 窗口已 resize 至屏幕尺寸")
        except Exception as e:
            logger.debug("window.resizeTo 失败: %s", e)

    async def _is_wide_top_nav(self, page) -> bool:
        return await page.evaluate(
            """() => {
                let topNav = 0;
                for (const el of document.querySelectorAll('a, button, [role=menuitem]')) {
                    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    if (!/成立公司|提交文件|查册|主页|Incorporation|Submit/.test(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < 180) topNav++;
                }
                return topNav >= 2;
            }"""
        )

    def _is_nnc1_shares_label(self, text: str) -> bool:
        """匹配「股份有限公司(表格NNC1)」，排除 NNC1G 等其它表格。"""
        if not text:
            return False
        compact = re.sub(r"\s+", "", text)
        if any(x in compact for x in ("数码证书", "智方便", "NNC1G")):
            return False
        if "股份有限公司" not in compact:
            return False
        return bool(re.search(r"NNC\s*1", text, re.I))

    async def _click_menu_item(
        self, page, label_pat: re.Pattern[str], *, max_y: float = 450, tag: str = ""
    ) -> bool:
        scopes = [
            page.locator("#hdr"),
            page.locator("header"),
            page.locator("ul.secondLevelMenu").first,
            page,
        ]
        for scope in scopes:
            candidates = scope.get_by_text(label_pat)
            count = await candidates.count()
            for i in range(count):
                loc = candidates.nth(i)
                if not await loc.is_visible():
                    continue
                box = await loc.bounding_box()
                if not box or box["y"] > max_y or box["y"] < 85:
                    continue
                current_url = page.url
                click_el = loc.locator("xpath=ancestor-or-self::a[1]").first
                target = click_el if await click_el.count() > 0 else loc
                try:
                    await target.scroll_into_view_if_needed()
                    await target.click(force=True, timeout=10000)
                except Exception:
                    await loc.evaluate("el => (el.closest('a') || el).click()")
                try:
                    await page.wait_for_function(
                        "(url) => window.location.href !== url",
                        current_url,
                        timeout=20000,
                    )
                except Exception:
                    pass
                await wait_spin_clear(page, timeout_ms=60000)
                await page.wait_for_timeout(2000)
                text = ((await loc.inner_text()) or "").strip()
                logger.info("已点击菜单项%s: %s → %s", tag, text[:60], page.url[:120])
                return True
        return False

    async def _wait_hdr_dropdown(self, page, timeout_ms: int = 8000) -> bool:
        """等待顶栏「成立公司」下拉出现。"""
        try:
            await page.wait_for_function(
                """() => {
                    for (const el of document.querySelectorAll(
                        'ul.secondLevelMenu a, ul.secondLevelMenu li, #hdr ul li a'
                    )) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top < 85 || r.top > 480) continue;
                        const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                        if (/本地公司|非香港公司|提交文件/.test(t)) return true;
                    }
                    return false;
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    async def _hdr_menu_js_action(
        self,
        page,
        *,
        label_re: str,
        max_y: float = 480,
        min_y: float = 85,
        action: str = "click",
        prefer_right: bool = False,
    ) -> str | None:
        """在顶栏下拉区按文案匹配元素并 hover/click（排除页脚同名链接）。"""
        return await page.evaluate(
            """({ labelRe, maxY, minY, action, preferRight }) => {
                const re = new RegExp(labelRe, 'i');
                const exclude = /数码证书|智方便|Digital Certificate|NNC1G/i;
                const candidates = [];
                for (const el of document.querySelectorAll('a, li, span, button, [role=menuitem]')) {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || !re.test(text)) continue;
                    if (exclude.test(text)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < minY || r.top > maxY) continue;
                    if (r.left < 40) continue;
                    const compact = text.replace(/\\s+/g, '');
                    if (compact.length > 80) continue;
                    candidates.push({ el, r, text, area: r.width * r.height });
                }
                if (!candidates.length) return null;
                if (preferRight) {
                    candidates.sort((a, b) => b.r.left - a.r.left || a.r.top - b.r.top);
                } else {
                    candidates.sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left || b.area - a.area);
                }
                const { el, text } = candidates[0];
                const target = el.closest('a') || el.closest('li') || el;
                const r = target.getBoundingClientRect();
                if (action === 'hover') {
                    target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                } else {
                    (target.closest('a') || target).click();
                }
                return JSON.stringify({
                    text: text.slice(0, 80),
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    action,
                });
            }""",
            {
                "labelRe": label_re,
                "maxY": max_y,
                "minY": min_y,
                "action": action,
                "preferRight": prefer_right,
            },
        )

    async def _wait_nnc1_dropdown_item(self, page, timeout_ms: int = 6000) -> bool:
        """等待「股份有限公司(表格NNC1)」出现在顶栏下拉第三列。"""
        try:
            await page.wait_for_function(
                """() => {
                    const exclude = /数码证书|智方便|NNC1G/i;
                    for (const el of document.querySelectorAll('a, li, span')) {
                        const text = (el.innerText || el.textContent || '').trim();
                        const compact = text.replace(/\\s+/g, '');
                        if (exclude.test(text)) continue;
                        if (!compact.includes('股份有限公司') || !/NNC\\s*1/i.test(text)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top < 85 || r.top > 500) continue;
                        return true;
                    }
                    return false;
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    async def _expand_local_company_submenu(self, page, *, click: bool = False) -> bool:
        """宽屏：成立公司下拉 → 悬停「本地公司」展开第三列（默认不点击，避免跳转关闭菜单）。"""
        local_pat = re.compile(r"本地公司")
        scopes = [
            page.locator("#hdr"),
            page.locator("ul.secondLevelMenu"),
            page.locator("header"),
        ]
        for scope in scopes:
            candidates = scope.get_by_text(local_pat)
            count = await candidates.count()
            for i in range(count):
                local = candidates.nth(i)
                if not await local.is_visible():
                    continue
                text = ((await local.inner_text()) or "").strip()
                compact = re.sub(r"\s+", "", text)
                if compact != "本地公司":
                    continue
                box = await local.bounding_box()
                if not box or box["y"] < 85 or box["y"] > 480:
                    continue
                await local.scroll_into_view_if_needed()
                await local.hover()
                await page.wait_for_timeout(400)
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                await page.mouse.move(cx, cy)
                await page.wait_for_timeout(300)
                if click:
                    click_target = local.locator("xpath=ancestor-or-self::a[1]").first
                    try:
                        if await click_target.count() > 0:
                            await click_target.click(force=True, timeout=5000)
                        else:
                            await local.click(force=True, timeout=5000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(900)
                else:
                    await page.wait_for_timeout(700)
                    await self._wait_nnc1_dropdown_item(page, timeout_ms=4000)
                logger.info(
                    "已展开「本地公司」子菜单 (y=%.0f, click=%s)",
                    box["y"],
                    click,
                )
                return True

        raw = await self._hdr_menu_js_action(
            page, label_re=r"^本地公司$", action="hover"
        )
        if raw:
            info = json.loads(raw)
            await page.mouse.move(info["x"], info["y"])
            await page.wait_for_timeout(500)
            if click:
                await self._hdr_menu_js_action(
                    page, label_re=r"^本地公司$", action="click"
                )
                await page.wait_for_timeout(900)
            else:
                await self._wait_nnc1_dropdown_item(page, timeout_ms=4000)
            logger.info("已展开「本地公司」子菜单 (JS y=%.0f, click=%s)", info["y"], click)
            return True

        logger.warning("未找到可见的「本地公司」菜单项")
        return False

    async def _click_nnc1_menu_label(self, page) -> bool:
        """点击「股份有限公司(表格NNC1)」。"""
        if await self._click_menu_item(
            page, NNC1_MENU_LABEL_PAT, tag=" NNC1"
        ):
            return True

        nnc1_candidates = page.get_by_text(re.compile(r"NNC\s*1", re.I))
        count = await nnc1_candidates.count()
        for i in range(count):
            loc = nnc1_candidates.nth(i)
            if not await loc.is_visible():
                continue
            text = ((await loc.inner_text()) or "").strip()
            if not self._is_nnc1_shares_label(text):
                continue
            box = await loc.bounding_box()
            if not box or box["y"] > 480 or box["y"] < 85:
                continue
            current_url = page.url
            click_el = loc.locator("xpath=ancestor-or-self::a[1]").first
            target = click_el if await click_el.count() > 0 else loc
            await target.click(force=True, timeout=10000)
            try:
                await page.wait_for_function(
                    "(url) => window.location.href !== url",
                    current_url,
                    timeout=20000,
                )
            except Exception:
                pass
            await wait_spin_clear(page, timeout_ms=60000)
            await page.wait_for_timeout(2000)
            logger.info("已进入 NNC1 入口: %s → %s", text[:60], page.url[:120])
            return True

        clicked = await page.evaluate(
            """() => {
                const exclude = /数码证书|智方便|NNC1G/i;
                for (const el of document.querySelectorAll(
                    '#hdr a, #hdr li, header a, ul.secondLevelMenu a, ul.thirdLevelMenu a'
                )) {
                    const text = (el.innerText || el.textContent || '').trim();
                    const compact = text.replace(/\\s+/g, '');
                    if (exclude.test(text)) continue;
                    if (!compact.includes('股份有限公司') || !/NNC\\s*1/i.test(text)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top < 85 || r.top > 480) continue;
                    (el.closest('a') || el).click();
                    return text.slice(0, 80);
                }
                return '';
            }"""
        )
        if clicked:
            await wait_spin_clear(page, timeout_ms=60000)
            await page.wait_for_timeout(2500)
            logger.info("已进入 NNC1 入口 (JS): %s → %s", clicked, page.url[:120])
            return True
        raw = await self._hdr_menu_js_action(
            page,
            label_re=r"股份有限公司.*NNC\s*1",
            max_y=500,
            action="click",
            prefer_right=True,
        )
        if raw:
            info = json.loads(raw)
            current_url = page.url
            await page.mouse.click(info["x"], info["y"])
            try:
                await page.wait_for_function(
                    "(url) => window.location.href !== url",
                    current_url,
                    timeout=20000,
                )
            except Exception:
                pass
            await wait_spin_clear(page, timeout_ms=60000)
            await page.wait_for_timeout(2000)
            logger.info(
                "已进入 NNC1 入口 (JS 坐标): %s → %s",
                info.get("text"),
                page.url[:120],
            )
            return True

        return False

    async def _open_nnc1_wide_cascade(self, page) -> bool:
        """宽屏三级菜单：成立公司(已展开) → hover 本地公司 → 股份有限公司(表格NNC1)。"""
        if not await self._expand_local_company_submenu(page, click=False):
            await self._expand_local_company_submenu(page, click=True)
        return await self._click_nnc1_menu_label(page)

    async def _open_main_nav(self, page, *, force: bool = False) -> None:
        """小屏：展开汉堡菜单。force=True 时宽屏也强制展开侧边栏。"""
        if not force:
            if await self._is_wide_top_nav(page):
                logger.info("检测到宽屏顶栏导航，跳过汉堡菜单")
                return
            nav_link = page.locator(
                "nav a:has-text('成立公司'), header a:has-text('成立公司'), "
                "a:has-text('成立公司'), a:has-text('Incorporation')"
            ).first
            if await nav_link.count() > 0 and await nav_link.is_visible():
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
                    if (r.right < window.innerWidth - 180) continue;
                    const label = (btn.innerText || btn.getAttribute('aria-label') || '').trim();
                    if (/登出|logout|help|帮助|\\?/i.test(label)) continue;
                    btn.click();
                    return true;
                }
                return false;
            }"""
        )
        if opened:
            logger.info("已展开顶栏汉堡菜单")
            await page.wait_for_timeout(900)

    async def _on_nnc1_form_page(self, page) -> bool:
        url = page.url or ""
        if "/nnc1/" in url.lower():
            return True
        return await page.evaluate(
            """() => /输入基本资料|輸入基本資料|输入公司资料|法团成立表格|表格 NNC1/i.test(document.body?.innerText || '')"""
        )

    async def _open_nnc1(self, page) -> None:
        """宽屏：成立公司 → 本地公司 → 股份有限公司(表格NNC1)；小屏：侧边栏。"""
        await self._maximize_browser_window(page)
        await wait_spin_clear(page, timeout_ms=30000)
        await page.wait_for_timeout(500)

        wide = await self._is_wide_top_nav(page)
        if not wide:
            await self._open_main_nav(page)

        nav_targets = await self._find_incorporation_navs(page, wide=wide)
        if not nav_targets:
            raise RuntimeError("未找到顶栏「成立公司」菜单")

        for idx, nav in enumerate(nav_targets):
            await nav.scroll_into_view_if_needed()
            box = await nav.bounding_box()
            if wide:
                if box:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    await page.mouse.move(cx, cy)
                    await page.wait_for_timeout(200)
                try:
                    await nav.hover()
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
                logger.info(
                    "顶栏展开 成立公司 #%d (%.0f, %.0f)",
                    idx + 1,
                    box["x"] if box else -1,
                    box["y"] if box else -1,
                )
                if not await self._wait_hdr_dropdown(page):
                    try:
                        await nav.click(timeout=5000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(800)
                    await self._wait_hdr_dropdown(page, timeout_ms=5000)
                await page.wait_for_timeout(600)
                if await self._open_nnc1_wide_cascade(page):
                    return
            else:
                await nav.click()
                await page.wait_for_timeout(1200)
                if await self._expand_local_company_submenu(page):
                    pass
                if await self._click_nnc1_menu_label(page):
                    return
                if await self._click_nnc1_submenu(page, wide=False):
                    return

        logger.warning("顶栏菜单未命中，强制汉堡菜单重试")
        await self._open_main_nav(page, force=True)
        nav_targets = await self._find_incorporation_navs(page, wide=False)
        for nav in nav_targets:
            try:
                await nav.click(force=True, timeout=10000)
            except Exception:
                await nav.evaluate("el => el.click()")
            await page.wait_for_timeout(1200)
            await self._expand_local_company_submenu(page)
            if await self._click_nnc1_menu_label(page):
                return
            if await self._click_nnc1_submenu(page, wide=False):
                return

        raise RuntimeError("未找到「股份有限公司(表格NNC1)」子菜单项")

    async def _find_incorporation_navs(self, page, *, wide: bool) -> list[Any]:
        """返回「成立公司」候选（宽屏：顶栏 y<95 优先，再 y<200）。"""
        bands = [95, 200] if wide else [9999]
        candidates = page.locator("a, button, [role='menuitem']").filter(
            has_text=re.compile(r"^成立公司$|^Incorporation$")
        )
        count = await candidates.count()
        ranked: list[tuple[float, float, int]] = []
        for i in range(count):
            el = candidates.nth(i)
            if not await el.is_visible():
                continue
            box = await el.bounding_box()
            if not box or box["y"] < 15:
                continue
            ranked.append((box["y"], box["x"], i))
        ranked.sort(key=lambda t: (t[0], t[1]))
        if not wide:
            sidebar = [(y, x, i) for y, x, i in ranked if y > 120]
            if sidebar:
                ranked = sidebar

        indices: list[int] = []
        for band in bands:
            for y, _, i in ranked:
                if band < 9999 and y > band:
                    continue
                if i not in indices:
                    indices.append(i)

        return [candidates.nth(i) for i in indices]

    async def _find_incorporation_nav(self, page, *, wide: bool) -> Any:
        navs = await self._find_incorporation_navs(page, wide=wide)
        return navs[0] if navs else None

    async def _click_nnc1_submenu(self, page, *, wide: bool) -> bool:
        """侧边栏等场景的 NNC1 兜底点击。"""
        if await self._click_nnc1_menu_label(page):
            return True
        y_max = 550 if wide else 9999

        links = page.locator("a, button, [role='menuitem']")
        count = await links.count()
        for i in range(count):
            link = links.nth(i)
            if not await link.is_visible():
                continue
            text = ((await link.inner_text()) or "").strip()
            if not self._is_nnc1_shares_label(text):
                continue
            box = await link.bounding_box()
            if not box or box["y"] > y_max:
                continue
            await link.scroll_into_view_if_needed()
            try:
                await link.click(timeout=15000)
            except Exception:
                await link.click(force=True, timeout=15000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await page.wait_for_timeout(2500)
            logger.info("已进入 NNC1 入口: %s → %s", text[:60], page.url[:120])
            return True

        clicked = await page.evaluate(
            """({ yMax }) => {
                const pat = /股份有限公司.*NNC\\s*1|表格\\s*NNC\\s*1|Form NNC1|NNC\\s*1/i;
                const exclude = /数码证书|智方便|Digital Certificate/i;
                for (const el of document.querySelectorAll('a, button, [role=menuitem], li')) {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!pat.test(text) || exclude.test(text)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top > yMax) continue;
                    const target = el.closest('a') || el;
                    target.click();
                    return text.slice(0, 80);
                }
                return '';
            }""",
            {"yMax": y_max},
        )
        if clicked:
            await page.wait_for_timeout(2500)
            logger.info("已进入 NNC1 入口 (JS): %s → %s", clicked, page.url[:120])
            return True
        return False

    async def _wait_efiling_terms_ready(self, page) -> None:
        """等待 e-filing 条款页渲染完成，且「接受」按钮可见可点。"""
        logger.info("等待 e-filing 条款页加载: %s", page.url[:120])
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            logger.debug("e-filing networkidle 超时，继续等待条款 DOM")

        try:
            await page.wait_for_function(
                """() => {
                    const body = document.body ? document.body.innerText : '';
                    if (/載入中|加载中|Loading/i.test(body) && body.length < 400) return false;
                    if (document.querySelector('.ant-spin-spinning')) return false;
                    const hasTerms = /条款及条件|條款及條件|Terms and Conditions|电子提交服务|電子提交服務/.test(body);
                    if (!hasTerms) return false;
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button], .btn'
                    )) {
                        const raw = (el.innerText || el.value || el.textContent || '').trim();
                        const t = raw.replace(/\\s+/g, '');
                        if (t !== '接受' && t !== 'Accept') continue;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                        if (el.closest('[class*=cookie], [class*=Cookie]')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        return true;
                    }
                    return false;
                }""",
                timeout=90000,
            )
            logger.info("e-filing 条款页已就绪，「接受」按钮可见")
        except Exception as e:
            await self._maybe_screenshot(page, "terms_wait_fail")
            raise RuntimeError(f"e-filing 条款页加载超时（未出现「接受」按钮）: {e}")

    async def _find_efiling_accept_button(self, page) -> Any | None:
        """返回条款页「接受」按钮 locator（排除 Cookie / 拒绝）。"""
        accept_selectors = [
            "button:has-text('接受')",
            "a:has-text('接受')",
            "input[type='submit'][value*='接受']",
            "input[type='button'][value*='接受']",
            "[role='button']:has-text('接受')",
            ".btn:has-text('接受')",
            "button:has-text('Accept')",
            "a:has-text('Accept')",
        ]
        for sel in accept_selectors:
            candidates = page.locator(sel)
            count = await candidates.count()
            for i in range(count - 1, -1, -1):
                btn = candidates.nth(i)
                if not await btn.is_visible():
                    continue
                label = ((await btn.inner_text()) or (await btn.get_attribute("value")) or "").strip()
                if "拒绝" in label or "Reject" in label:
                    continue
                in_cookie = await btn.evaluate(
                    "el => !!el.closest('[class*=cookie], [class*=Cookie]')"
                )
                if in_cookie:
                    continue
                disabled = await btn.evaluate(
                    "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
                if disabled:
                    continue
                return btn
        return None

    async def _accept_efiling_terms(self, page) -> None:
        """e-filing 条款页：等加载完成后再点「接受」（非 s01 注册条款 checkbox 流程）。"""
        await self._wait_efiling_terms_ready(page)

        # 仅滚条款正文区到底（勿滚整页，否则「接受」会离开视口）
        await page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('textarea, div, section')) {
                    const text = el.innerText || '';
                    if (!/条款|條款|Terms|电子提交|電子提交|申请/.test(text)) continue;
                    if (el.scrollHeight <= el.clientHeight + 20) continue;
                    if (el.querySelector('button, a')) continue;
                    el.scrollTop = el.scrollHeight;
                }
            }"""
        )
        await page.wait_for_timeout(500)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)

        # 滚动后再次确认按钮仍可见（Vue 重绘时可能短暂消失）
        try:
            await page.wait_for_function(
                """() => {
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button]'
                    )) {
                        const t = (el.innerText || el.value || '').replace(/\\s+/g, '');
                        if (t !== '接受' && t !== 'Accept') continue;
                        if (el.closest('[class*=cookie], [class*=Cookie]')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return false;
                }""",
                timeout=15000,
            )
        except Exception:
            logger.warning("滚动后「接受」按钮短暂不可见，继续尝试点击")

        btn = await self._find_efiling_accept_button(page)
        if btn is not None:
            current_url = page.url
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            try:
                await btn.click(timeout=15000)
            except Exception:
                await btn.click(force=True, timeout=15000)
            logger.info("已点击 e-filing 接受按钮")
            try:
                await page.wait_for_function(
                    "(url) => window.location.href !== url",
                    current_url,
                    timeout=30000,
                )
            except Exception:
                pass
            await wait_spin_clear(page, timeout_ms=60000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            logger.info("条款接受后 URL: %s", page.url[:120])
            await self._wait_after_accept(page)
            return

        clicked = await page.evaluate(
            """() => {
                const pick = (el) => {
                    const t = (el.innerText || el.value || el.textContent || '').replace(/\\s+/g, '');
                    return t === '接受' || t === 'Accept';
                };
                for (const el of document.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], [role=button], .btn'
                )) {
                    if (!pick(el)) continue;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    if (el.closest('[class*=cookie], [class*=Cookie]')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const btn = el.closest('button, a, [role=button], .btn') || el;
                    btn.click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            await wait_spin_clear(page, timeout_ms=60000)
            await page.wait_for_timeout(2500)
            logger.info("已点击 e-filing 接受 (JS)，URL: %s", page.url[:120])
            await self._wait_after_accept(page)
            return

        accept_btn = page.get_by_text("接受", exact=True).last
        if await accept_btn.count() > 0 and await accept_btn.is_visible():
            await accept_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await accept_btn.click(force=True, timeout=15000)
            await wait_spin_clear(page, timeout_ms=60000)
            await page.wait_for_timeout(2500)
            logger.info("已点击 e-filing 接受 (get_by_text)，URL: %s", page.url[:120])
            await self._wait_after_accept(page)
            return

        await self._maybe_screenshot(page, "terms_no_accept")
        raise RuntimeError("e-filing 条款页未找到「接受」按钮")

    async def _wait_after_accept(self, page) -> None:
        """接受条款后等待进入 NNC1 填表页。"""
        try:
            await page.wait_for_function(
                """() => {
                    const href = location.href || '';
                    const text = document.body?.innerText || '';
                    return /\\/nnc1\\//i.test(href)
                        || /输入基本资料|輸入基本資料|法团成立表格|法團成立表格|表格 NNC1|表格NNC1/.test(text);
                }""",
                timeout=90000,
            )
        except Exception as e:
            logger.warning("等待 NNC1 填表页超时: %s, url=%s", e, page.url[:120])
        await wait_spin_clear(page, timeout_ms=60000)
        await page.wait_for_timeout(1500)

    async def _wait_nnc1_form_ready(self, page) -> None:
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            logger.debug("NNC1 networkidle 超时，继续等待表单 DOM")
        try:
            await page.wait_for_function(
                """() => {
                    const body = document.body?.innerText || '';
                    if (document.querySelector('.ant-spin-spinning')) return false;
                    if (/載入中|加载中|Loading/i.test(body) && body.length < 400) return false;
                    return /输入基本资料|輸入基本資料|选择语言|選擇語言|法团印章|法團印章/.test(body);
                }""",
                timeout=90000,
            )
        except Exception as e:
            logger.warning("NNC1 步骤1 页面检测超时: %s", e)
        logger.info("NNC1 表单就绪: %s", page.url[:120])

    async def _wait_nnc1_save_continue_ready(self, page) -> None:
        """等待步骤底栏「储存/存储及继续」按钮渲染完成。"""
        logger.info("等待 NNC1「储存及继续」按钮: %s", page.url[:120])
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            logger.debug("NNC1 底栏 networkidle 超时，继续等待按钮")

        await self._scroll_form_to_bottom(page)

        try:
            await page.wait_for_function(
                """() => {
                    const body = document.body?.innerText || '';
                    if (document.querySelector('.ant-spin-spinning')) return false;
                    if (/載入中|加载中|Loading/i.test(body) && body.length < 400) return false;
                    const pat = /(储存|存储|儲存)及(继续|繼續)|Save\\s*(?:and|&)\\s*Continue/i;
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button], .btn'
                    )) {
                        const raw = (el.innerText || el.value || el.textContent || '').trim();
                        const compact = raw.replace(/\\s+/g, '');
                        if (!pat.test(compact) && !pat.test(raw)) continue;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        return true;
                    }
                    return false;
                }""",
                timeout=90000,
            )
            logger.info("NNC1「储存及继续」按钮已就绪")
        except Exception as e:
            await self._maybe_screenshot(page, "save_continue_wait_fail")
            raise RuntimeError(f"NNC1 底栏「储存及继续」加载超时: {e}")

    async def _find_save_continue_button(self, page) -> Any | None:
        labels = (
            "储存及继续",
            "存储及继续",
            "儲存及繼續",
            "儲存及继续",
            "Save and Continue",
            "Save & Continue",
        )
        for text in labels:
            btn = page.get_by_role("button", name=text).first
            if await btn.count() == 0:
                btn = page.locator(
                    f"button:has-text('{text}'), input[type='button'][value*='{text}'], "
                    f"input[type='submit'][value*='{text}'], a:has-text('{text}')"
                ).first
            if await btn.count() > 0 and await btn.is_visible():
                disabled = await btn.evaluate(
                    "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
                if not disabled:
                    return btn

        candidates = page.locator("button, a, input[type='button'], input[type='submit'], [role='button']")
        count = await candidates.count()
        for i in range(count):
            el = candidates.nth(i)
            if not await el.is_visible():
                continue
            raw = ((await el.inner_text()) or (await el.get_attribute("value")) or "").strip()
            compact = re.sub(r"\s+", "", raw)
            if not NNC1_SAVE_CONTINUE_PAT.search(compact) and not NNC1_SAVE_CONTINUE_PAT.search(raw):
                continue
            disabled = await el.evaluate(
                "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
            )
            if disabled:
                continue
            return el
        return None

    async def _scroll_form_to_bottom(self, page) -> None:
        await page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('main, form, [class*=content], [class*=form]')) {
                    if (el.scrollHeight > el.clientHeight + 20) {
                        el.scrollTop = el.scrollHeight;
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
            }"""
        )
        await page.wait_for_timeout(600)

    async def _check_common_seal_checkbox(self, page) -> None:
        """步骤1：滚到底部，勾选法团印章选项。"""
        await self._scroll_form_to_bottom(page)
        checked = await page.evaluate(
            """() => {
                const pat = /法团印章|法團印章|common seal|已拟备法团印章|已擬備法團印章/i;
                for (const cb of document.querySelectorAll('input[type=checkbox]')) {
                    const ctx = (
                        cb.closest('label, tr, div, li, fieldset')?.innerText || ''
                    ).replace(/\\s+/g, ' ');
                    if (!pat.test(ctx)) continue;
                    const r = cb.getBoundingClientRect();
                    if (r.width <= 0 && r.height <= 0) continue;
                    if (!cb.checked) cb.click();
                    return ctx.slice(0, 80);
                }
                for (const el of document.querySelectorAll('label, span, div, p, td')) {
                    const text = (el.innerText || '').replace(/\\s+/g, ' ');
                    if (!pat.test(text)) continue;
                    const box = el.querySelector('input[type=checkbox]')
                        || el.closest('div, tr, li')?.querySelector('input[type=checkbox]');
                    if (!box) continue;
                    if (!box.checked) box.click();
                    return text.slice(0, 80);
                }
                return '';
            }"""
        )
        if checked:
            logger.info("已勾选法团印章: %s", checked)
            return

        cb = page.locator(
            "xpath=//*[contains(text(),'法团印章') or contains(text(),'法團印章')]"
            "//ancestor-or-self::*[1]//input[@type='checkbox'] | "
            "//following::input[@type='checkbox'][1]"
        ).first
        if await cb.count() > 0:
            if not await cb.is_checked():
                await cb.scroll_into_view_if_needed()
                await cb.check(force=True)
            logger.info("已勾选法团印章 (Playwright)")
            return
        raise RuntimeError("未找到「法团印章」勾选框")

    async def _click_save_and_continue(self, page) -> None:
        """点击「储存/存储及继续」进入下一步（等页面加载完成后再点）。"""
        await self._wait_nnc1_save_continue_ready(page)
        await self._scroll_form_to_bottom(page)
        await page.wait_for_timeout(400)

        btn = await self._find_save_continue_button(page)
        if btn is not None:
            current_url = page.url
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            try:
                await btn.click(timeout=15000)
            except Exception:
                await btn.click(force=True, timeout=15000)
            label = ((await btn.inner_text()) or (await btn.get_attribute("value")) or "").strip()
            logger.info("已点击「%s」", label or "储存及继续")
            try:
                await page.wait_for_function(
                    "(url) => window.location.href !== url",
                    current_url,
                    timeout=30000,
                )
            except Exception:
                pass
            await wait_spin_clear(page, timeout_ms=90000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            return

        clicked = await page.evaluate(
            """() => {
                const pat = /(储存|存储|儲存)及(继续|繼續)|Save\\s*(?:and|&)\\s*Continue/i;
                for (const el of document.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], [role=button], .btn'
                )) {
                    const raw = (el.innerText || el.value || el.textContent || '').trim();
                    const compact = raw.replace(/\\s+/g, '');
                    if (!pat.test(compact) && !pat.test(raw)) continue;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    (el.closest('button, a, [role=button]') || el).click();
                    return raw.slice(0, 40);
                }
                return '';
            }"""
        )
        if clicked:
            await wait_spin_clear(page, timeout_ms=90000)
            await page.wait_for_timeout(2000)
            logger.info("已点击「%s」(JS)", clicked)
            return

        await self._maybe_screenshot(page, "save_continue_fail")
        raise RuntimeError("未找到「储存及继续」按钮")

    async def _fill_field_by_label(self, page, labels: list[str], value: str) -> bool:
        if not value:
            return False
        for label_text in labels:
            field = page.get_by_label(label_text).first
            if await field.count() > 0:
                disabled = await field.evaluate(
                    "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
                if disabled:
                    continue
                await field.scroll_into_view_if_needed()
                await field.fill(value)
                logger.info("已填写 [%s]: %s", label_text, value[:80])
                return True
            field = page.locator(
                f"xpath=//*[contains(normalize-space(.), '{label_text}')]"
                "/following::input[not(@type='hidden')][1]"
            ).first
            if await field.count() > 0 and await field.is_visible():
                disabled = await field.evaluate(
                    "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
                if disabled:
                    continue
                await field.fill(value)
                logger.info("已填写 [%s]: %s", label_text, value[:80])
                return True

        hit = await page.evaluate(
            """({ labels, value }) => {
                for (const pat of labels) {
                    for (const el of document.querySelectorAll('label, span, div, td, th, p')) {
                        const t = (el.innerText || '').trim();
                        if (!t.includes(pat)) continue;
                        let inp = el.querySelector(
                            'input:not([type=hidden]):not([type=checkbox]), textarea'
                        );
                        if (!inp) {
                            const row = el.closest('tr, div, fieldset, form');
                            inp = row?.querySelector(
                                'input:not([type=hidden]):not([type=checkbox]), textarea'
                            );
                        }
                        if (!inp) continue;
                        if (inp.disabled || inp.getAttribute('aria-disabled') === 'true') continue;
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 0 && r.height <= 0) continue;
                        inp.focus();
                        inp.value = value;
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        return pat;
                    }
                }
                return '';
            }""",
            {"labels": labels, "value": value},
        )
        if hit:
            logger.info("已填写 [%s]: %s", hit, value[:80])
            return True
        logger.warning("未找到字段: %s", labels[0])
        return False

    def _resolve_share_capital(self, data: dict[str, Any]) -> dict[str, Any]:
        """从 share_capital 或 registered_capital 解析股本数额（注册资本）。"""
        from src.materials.aggregator import _parse_share_capital

        sc = dict(data.get("share_capital") or {})
        cap_raw = data.get("registered_capital")
        cap_int = _parse_share_capital(str(cap_raw or ""))
        if sc.get("total_shares"):
            try:
                cap_int = int(sc["total_shares"])
            except (TypeError, ValueError):
                pass
        elif sc.get("paid_up"):
            try:
                cap_int = int(sc["paid_up"])
            except (TypeError, ValueError):
                pass
        total_shares = int(sc.get("total_shares") or cap_int)
        paid_up = int(sc.get("paid_up") or cap_int)
        currency = str(sc.get("currency") or "HKD").strip() or "HKD"
        return {
            "currency": currency,
            "total_shares": total_shares,
            "paid_up": paid_up,
            "subscribed": paid_up,
        }

    async def _scroll_to_section(self, page, keywords: list[str]) -> None:
        """滚动到包含指定标题/标签的表单区块。"""
        joined = "|".join(re.escape(k) for k in keywords if k)
        if not joined:
            return
        await page.evaluate(
            """(pat) => {
                const re = new RegExp(pat, 'i');
                const scrollers = [
                    document.querySelector('#formWrapper'),
                    document.querySelector('.formWrapper'),
                    document.querySelector('main'),
                    document.querySelector('form'),
                ].filter(Boolean);
                const scrollStep = () => {
                    for (const w of scrollers) w.scrollTop += 500;
                    window.scrollBy(0, 500);
                };
                for (let i = 0; i < 40; i++) {
                    for (const el of document.querySelectorAll(
                        'h1,h2,h3,h4,legend,th,.rowTitle,label,span,div,p'
                    )) {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 120) continue;
                        if (!re.test(t)) continue;
                        el.scrollIntoView({ block: 'center', behavior: 'instant' });
                        return true;
                    }
                    scrollStep();
                }
                window.scrollTo(0, document.body.scrollHeight);
                return false;
            }""",
            joined,
        )
        await page.wait_for_timeout(600)

    async def _select_radio_by_patterns(self, page, patterns: list[str]) -> bool:
        """按文案选中 radio（排除含「非」的互斥项）。"""
        hit = await page.evaluate(
            """(patterns) => {
                const pats = patterns.map(p => (p || '').replace(/\\s+/g, ''));
                for (const input of document.querySelectorAll('input[type=radio]')) {
                    const ctx = (
                        input.closest('label, tr, div, li, fieldset')?.innerText || ''
                    ).replace(/\\s+/g, '');
                    if (!pats.some(p => p && ctx.includes(p))) continue;
                    if (/非香港|非本港|NonHongKong/i.test(ctx) && /香港/.test(ctx)) continue;
                    if (!input.checked) input.click();
                    return ctx.slice(0, 60);
                }
                for (const label of document.querySelectorAll('label')) {
                    const t = (label.innerText || '').replace(/\\s+/g, '');
                    if (!pats.some(p => p && t.includes(p))) continue;
                    if (/非香港|非本港/.test(t)) continue;
                    const input = label.querySelector('input[type=radio]');
                    if (input) {
                        if (!input.checked) input.click();
                        return t.slice(0, 60);
                    }
                    label.click();
                    return t.slice(0, 60);
                }
                return '';
            }""",
            patterns,
        )
        if hit:
            logger.info("已选择单选: %s", hit)
            await page.wait_for_timeout(400)
            return True
        return False

    async def _select_option_by_label(
        self, page, label_patterns: list[str], option: str
    ) -> bool:
        """在标签附近的 native select / ant-select 中选择选项。"""
        if not option:
            return False
        hit = await page.evaluate(
            """({ labels, option }) => {
                const norm = s => (s || '').replace(/[\\s/／:*：]/g, '');
                const kws = labels.map(norm).filter(Boolean);
                const esc = option.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                const optRe = new RegExp(esc, 'i');

                const pickNative = (sel) => {
                    const opt = [...sel.options].find(o =>
                        optRe.test((o.textContent || '').trim()) ||
                        optRe.test((o.value || '').trim())
                    );
                    if (!opt) return false;
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return (opt.textContent || '').trim().slice(0, 40);
                };

                const titleEls = [...document.querySelectorAll(
                    '.rowTitle, th, label, .ant-form-item-label, .control-label, span, div'
                )];
                for (const title of titleEls) {
                    const tt = norm((title.innerText || '').trim());
                    if (!kws.some(k => k && tt.includes(k))) continue;
                    const containers = [
                        title.closest('tr'),
                        title.closest('td'),
                        title.closest('.ant-row, .row, .form-group, fieldset, .ant-form-item'),
                        title.parentElement,
                        title.parentElement?.parentElement,
                    ].filter(Boolean);
                    for (const container of containers) {
                        const native = container.querySelector('select');
                        if (native) {
                            const picked = pickNative(native);
                            if (picked) return picked;
                        }
                        const ant = container.querySelector('.ant-select');
                        if (ant) {
                            const trigger = ant.querySelector('.ant-select-selector') || ant;
                            trigger.scrollIntoView({ block: 'center' });
                            trigger.click();
                            return '__ant__';
                        }
                        const next = container.nextElementSibling;
                        if (next) {
                            const sel = next.querySelector('select, .ant-select');
                            if (sel?.tagName === 'SELECT') {
                                const picked = pickNative(sel);
                                if (picked) return picked;
                            }
                            if (sel?.classList?.contains('ant-select')) {
                                (sel.querySelector('.ant-select-selector') || sel).click();
                                return '__ant__';
                            }
                        }
                    }
                }
                return '';
            }""",
            {"labels": label_patterns, "option": option},
        )
        if hit == "__ant__":
            opt = page.locator(".ant-select-item-option").filter(
                has_text=re.compile(re.escape(option), re.I)
            ).first
            if await opt.count() > 0:
                await opt.click(timeout=5000)
                logger.info("已选择下拉 [%s]: %s", label_patterns[0], option)
                await page.wait_for_timeout(400)
                return True
            clicked = await page.evaluate(
                """(option) => {
                    const esc = option.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                    const re = new RegExp(esc, 'i');
                    for (const el of document.querySelectorAll('.ant-select-item-option')) {
                        const t = (el.innerText || '').trim();
                        if (!re.test(t)) continue;
                        el.click();
                        return t;
                    }
                    return '';
                }""",
                option,
            )
            if clicked:
                logger.info("已选择下拉 [%s]: %s", label_patterns[0], clicked)
                await page.wait_for_timeout(400)
                return True
            return False
        if hit:
            logger.info("已选择下拉 [%s]: %s", label_patterns[0], hit)
            await page.wait_for_timeout(400)
            return True
        return False

    async def _fill_registered_office_hk(self, page, office: dict[str, Any]) -> None:
        """填写公司在香港的注册办事处的建议地址。"""
        await self._scroll_to_section(
            page,
            [
                "注册办事处的建议地址",
                "註冊辦事處的建議地址",
                "Registered Office",
            ],
        )
        await self._select_radio_by_patterns(
            page, ["香港地址", "本港地址", "Hong Kong Address"]
        )

        flat = (office.get("flat_floor") or "").strip()
        building = (office.get("building") or "").strip()
        street = (
            (office.get("street_en") or office.get("street") or "").strip()
        )
        district = (office.get("district") or "").strip()
        region = (office.get("region") or "Hong Kong").strip()

        if flat:
            ok = await self._fill_field_by_label(
                page,
                [
                    "室 / 楼",
                    "室／樓",
                    "室/楼/座",
                    "Flat / Floor",
                ],
                flat,
            )
            if not ok:
                await page.evaluate(
                    """(value) => {
                        const re = /室.*樓|室.*楼|Flat.*Floor/i;
                        for (const el of document.querySelectorAll('th, label, .rowTitle, span, div')) {
                            const t = (el.innerText || '').trim();
                            if (!re.test(t)) continue;
                            const row = el.closest('tr, .ant-row, .row, fieldset');
                            const inp = row?.querySelector('textarea, input:not([type=hidden]):not([type=checkbox])');
                            if (!inp || inp.disabled) continue;
                            inp.focus();
                            inp.value = value;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                        return false;
                    }""",
                    flat,
                )
                logger.info("已填写 [室/楼/座]: %s", flat[:80])
        if building:
            await self._fill_field_by_label(
                page,
                ["大廈", "大厦", "Building"],
                building,
            )
        if street:
            await self._fill_field_by_label(
                page,
                [
                    "街道",
                    "屋苑",
                    "地段",
                    "村等",
                    "Street",
                    "Estate",
                    "Lot",
                    "Village",
                ],
                street,
            )
        if district:
            await self._select_option_by_label(
                page,
                ["區", "区", "District", "郵遞區號"],
                district,
            )
        if region:
            await self._select_option_by_label(
                page,
                ["地區", "地区", "Region", "Area"],
                region,
            )

    async def _fill_share_table_input(self, page, inp_loc, value: str) -> bool:
        """填写股本表单元格内的 input（Playwright fill，兼容 React/ant-input）。"""
        if not value or await inp_loc.count() == 0:
            return False
        el = inp_loc.first
        try:
            if not await el.is_visible():
                return False
        except Exception:
            pass
        try:
            disabled = await el.evaluate(
                "e => e.disabled || e.getAttribute('aria-disabled') === 'true'"
            )
            if disabled:
                return False
        except Exception:
            return False
        await el.scroll_into_view_if_needed()
        try:
            await el.click(timeout=5000)
        except Exception:
            pass
        try:
            await el.fill(value, timeout=10000)
        except Exception:
            await el.click(force=True)
            await el.fill(value, timeout=10000)
        try:
            await el.press("Tab")
        except Exception:
            pass
        try:
            got = self._norm_share_num(await el.input_value())
            return got == self._norm_share_num(value)
        except Exception:
            return True

    def _norm_share_num(self, raw: str) -> str:
        return (raw or "").replace(",", "").strip()

    async def _fill_share_capital_table(self, page, sc: dict[str, Any]) -> None:
        """股本表只填第一行（注册资本）；禁止扫全页以免误改公司名称。"""
        await self._scroll_to_section(
            page,
            [
                "股本及最初的股份持有",
                "股本及最初",
                "公司組成時的股本",
                "公司组成时的股本",
                "Share Capital",
                "股份持有情况",
                "股份持有情況",
            ],
        )
        await page.wait_for_timeout(500)

        amount = str(sc.get("subscribed") or sc.get("paid_up") or "")
        shares = str(sc.get("total_shares") or amount)
        currency = str(sc.get("currency") or "HKD").strip() or "HKD"
        if not shares and not amount:
            logger.warning("股本数据为空，跳过")
            return

        table = page.locator("table").filter(
            has_text=re.compile(r"股份的類別|股份的类别")
        ).first
        if await table.count() == 0:
            logger.warning("未找到股本表")
            return

        await table.scroll_into_view_if_needed()
        header_row = table.locator("tr").filter(has=page.locator("th")).first
        if await header_row.count() == 0:
            header_row = table.locator("tr").first

        header_texts = await header_row.locator("th, td").all_inner_texts()
        col_map: dict[str, int] = {}
        for idx, raw in enumerate(header_texts):
            h = re.sub(r"\s+", "", (raw or "").strip())
            if not h:
                continue
            if re.search(r"股份的類別|股份的类别", h):
                col_map["class"] = idx
            elif re.search(r"建議發行的股份總數|建议发行的股份总数", h):
                col_map["shares"] = idx
            elif re.search(r"貨幣單位|货币单位", h):
                col_map["currency"] = idx
            elif re.search(r"創辦成員認購|创办成员认购", h):
                col_map["subscribed"] = idx
            elif re.search(r"將要繳付或視為已繳付|将要缴付或视为已缴付", h):
                col_map["paid"] = idx

        data_row = table.locator("tr").filter(
            has=page.locator("td input:not([type=hidden]), td textarea")
        ).first
        if await data_row.count() == 0:
            logger.warning("股本表无数据行")
            return

        filled: list[str] = []

        class_idx = col_map.get("class", 0)
        class_cell = data_row.locator("td").nth(class_idx)
        class_inp = class_cell.locator(
            "input:not([type=hidden]):not([type=checkbox]), textarea"
        ).first
        if await class_inp.count() > 0:
            cur = (await class_inp.input_value() or "").strip()
            if not cur:
                if await self._fill_share_table_input(page, class_inp, "Ordinary"):
                    filled.append("class")

        shares_idx = col_map.get("shares", 1)
        shares_cell = data_row.locator("td").nth(shares_idx)
        shares_inp = shares_cell.locator(
            "input:not([type=hidden]):not([type=checkbox]), textarea"
        ).filter(has_not=page.locator("[type=hidden]")).last
        if await shares_inp.count() == 0:
            shares_inp = shares_cell.locator(
                "input:not([type=hidden]):not([type=checkbox]), textarea"
            ).first
        if shares and await shares_inp.count() > 0:
            if await self._fill_share_table_input(page, shares_inp, shares):
                filled.append("shares")

        cur_idx = col_map.get("currency", 2)
        cur_cell = data_row.locator("td").nth(cur_idx)
        sel = cur_cell.locator("select").first
        if await sel.count() > 0:
            try:
                await sel.select_option(label=re.compile(re.escape(currency), re.I))
                filled.append("currency")
            except Exception:
                try:
                    await sel.select_option(value=currency)
                    filled.append("currency")
                except Exception as e:
                    logger.debug("股本货币 select 失败: %s", e)
        elif await cur_cell.locator(".ant-select").count() > 0:
            trigger = cur_cell.locator(".ant-select-selector").first
            await trigger.click(timeout=5000)
            opt = page.locator(".ant-select-item-option").filter(
                has_text=re.compile(re.escape(currency), re.I)
            ).first
            if await opt.count() > 0:
                await opt.click(timeout=5000)
                filled.append("currency")
            else:
                await page.keyboard.press("Escape")

        sub_idx = col_map.get("subscribed", 3)
        sub_cell = data_row.locator("td").nth(sub_idx)
        sub_inp = sub_cell.locator(
            "input:not([type=hidden]):not([type=checkbox]), textarea"
        ).first
        if amount and await sub_inp.count() > 0:
            if await self._fill_share_table_input(page, sub_inp, amount):
                filled.append("subscribed")

        paid_idx = col_map.get("paid", 4)
        paid_cell = data_row.locator("td").nth(paid_idx)
        paid_inp = paid_cell.locator(
            "input:not([type=hidden]):not([type=checkbox]), textarea"
        ).first
        if amount and await paid_inp.count() > 0:
            if await self._fill_share_table_input(page, paid_inp, amount):
                filled.append("paid")

        # 核对「建議發行的股份總數」；未写入则 JS 兜底
        shares_ok = False
        if shares and await shares_inp.count() > 0:
            got = self._norm_share_num(await shares_inp.input_value())
            shares_ok = got == self._norm_share_num(shares)
        if shares and not shares_ok:
            js_shares = await page.evaluate(
                """({ shares, sharesIdx }) => {
                    const table = [...document.querySelectorAll('table')].find(tb =>
                        /股份的類別|股份的类别/.test((tb.innerText || '').slice(0, 500))
                    );
                    if (!table) return false;
                    const dataRow = [...table.querySelectorAll('tr')].find(tr =>
                        tr.querySelector('td input:not([type=hidden]), td textarea')
                    );
                    if (!dataRow) return false;
                    const cell = dataRow.querySelectorAll('td')[sharesIdx];
                    if (!cell) return false;
                    const inp = cell.querySelector(
                        'input:not([type=hidden]):not([type=checkbox]), textarea'
                    );
                    if (!inp) return false;
                    inp.focus();
                    inp.click();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (setter) setter.call(inp, shares);
                    else inp.value = shares;
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                    inp.blur();
                    return ((inp.value || '').replace(/,/g, '').trim() === String(shares));
                }""",
                {"shares": shares, "sharesIdx": shares_idx},
            )
            if js_shares:
                filled.append("shares-js")
            elif await shares_inp.count() > 0:
                # 最后尝试：逐字输入
                try:
                    await shares_inp.click(timeout=5000)
                    await shares_inp.press("Control+a")
                    await shares_inp.type(shares, delay=30)
                    await shares_inp.press("Tab")
                    got = self._norm_share_num(await shares_inp.input_value())
                    if got == self._norm_share_num(shares):
                        filled.append("shares-type")
                except Exception as e:
                    logger.warning("股份总数 type 兜底失败: %s", e)

        # 清空第二行及以后误填的数字
        extra_rows = table.locator("tr").filter(
            has=page.locator("td input:not([type=hidden]), td textarea")
        )
        extra_count = await extra_rows.count()
        for i in range(1, extra_count):
            row = extra_rows.nth(i)
            inputs = row.locator(
                "input:not([type=hidden]):not([type=checkbox]):not([disabled])"
            )
            in_count = await inputs.count()
            for j in range(in_count):
                inp = inputs.nth(j)
                val = (await inp.input_value() or "").replace(",", "").strip()
                if val and re.fullmatch(r"\d+(\.\d+)?", val):
                    try:
                        await inp.fill("")
                        filled.append(f"cleared-row-{i + 1}")
                    except Exception:
                        pass

        if filled:
            logger.info(
                "已填写股本第一行: shares=%s amount=%s currency=%s detail=%s",
                shares,
                amount,
                currency,
                filled,
            )
        else:
            logger.warning("股本第一行填写失败")

    async def _fill_step1_basic_info(self, page, data: dict[str, Any]) -> None:
        """步骤1：输入基本资料 — 勾选法团印章 → 存储及继续。"""
        logger.info("NNC1 步骤1: 输入基本资料")
        await self._wait_nnc1_form_ready(page)
        await self._maybe_screenshot(page, "step1_before")
        await self._check_common_seal_checkbox(page)
        await self._maybe_screenshot(page, "step1_checked")
        await self._click_save_and_continue(page)
        await self._maybe_screenshot(page, "step1_after")

    async def _fill_step2_company_info(self, page, data: dict[str, Any]) -> None:
        """步骤2：输入公司资料 — 名称、注册地址、股本。"""
        logger.info("NNC1 步骤2: 输入公司资料")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_function(
                """() => {
                    if (document.querySelector('.ant-spin-spinning')) return false;
                    const t = document.body?.innerText || '';
                    return /输入公司资料|輸入公司資料|公司英文名称|公司英文名稱/.test(t);
                }""",
                timeout=90000,
            )
        except Exception:
            logger.warning("步骤2 页面文案检测超时，继续填表")

        name_en = (data.get("company_name_en") or "").strip()
        name_cn = (data.get("company_name_cn") or "").strip()
        await self._fill_field_by_label(
            page,
            [
                "建议采用的公司英文名称",
                "建議採用的公司英文名稱",
                "Proposed English Company Name",
                "公司英文名称",
            ],
            name_en,
        )
        await self._fill_field_by_label(
            page,
            [
                "建议采用的公司中文名称",
                "建議採用的公司中文名稱",
                "Proposed Chinese Company Name",
                "公司中文名称",
            ],
            name_cn,
        )

        office = dict(data.get("registered_office") or {})
        sc = self._resolve_share_capital(data)

        await self._scroll_form_to_bottom(page)
        await self._fill_registered_office_hk(page, office)
        await self._fill_share_capital_table(page, sc)

        # 防止股本填写误改公司名称：若英文名变成纯数字则重填
        await self._ensure_company_names(page, name_en, name_cn)

        await self._maybe_screenshot(page, "step2_filled")
        await self._click_save_and_continue(page)
        await self._maybe_screenshot(page, "step2_after")

    async def _ensure_company_names(self, page, name_en: str, name_cn: str) -> None:
        """核对并必要时重填公司英文/中文名称。"""
        bad = await page.evaluate(
            """({ nameEn, nameCn }) => {
                const find = (pats) => {
                    for (const pat of pats) {
                        for (const el of document.querySelectorAll('label, th, .rowTitle, span, div')) {
                            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (!t.includes(pat) || t.length > 60) continue;
                            const row = el.closest('tr, .ant-row, .ant-form-item, fieldset, div');
                            const inp = row?.querySelector(
                                'textarea, input:not([type=hidden]):not([type=checkbox])'
                            );
                            if (!inp) continue;
                            return (inp.value || '').trim();
                        }
                    }
                    return '';
                };
                const en = find(['建議採用的公司英文名稱', '建议采用的公司英文名称', '公司英文名稱', '公司英文名称']);
                const cn = find(['建議採用的公司中文名稱', '建议采用的公司中文名称', '公司中文名稱', '公司中文名称']);
                const enBad = !en || /^[\\d,]+$/.test(en) || (nameEn && en !== nameEn && /^\\d/.test(en));
                const cnBad = nameCn && (!cn || /^[\\d,]+$/.test(cn));
                return { en, cn, enBad, cnBad };
            }""",
            {"nameEn": name_en, "nameCn": name_cn},
        )
        if bad and bad.get("enBad") and name_en:
            logger.warning(
                "公司英文名称异常(%r)，重新填写",
                (bad.get("en") or "")[:40],
            )
            await self._fill_field_by_label(
                page,
                [
                    "建议采用的公司英文名称",
                    "建議採用的公司英文名稱",
                    "Proposed English Company Name",
                ],
                name_en,
            )
        if bad and bad.get("cnBad") and name_cn:
            logger.warning(
                "公司中文名称异常(%r)，重新填写",
                (bad.get("cn") or "")[:40],
            )
            await self._fill_field_by_label(
                page,
                [
                    "建议采用的公司中文名称",
                    "建議採用的公司中文名稱",
                    "Proposed Chinese Company Name",
                ],
                name_cn,
            )

    async def run(
        self,
        account: IcrisAccount,
        data: dict[str, Any],
        *,
        force_isolated: bool = False,
        screenshot_path: str = "",
    ) -> tuple[bool, str]:
        """登录 → 简体 → NNC1 → 接受条款 → 填表 stub。"""
        from src.browser.launcher import import_async_playwright

        async_playwright = import_async_playwright()
        async with async_playwright() as p:
            browser = await launch_browser(p, force_isolated=force_isolated)
            context = await create_browser_context(browser)
            page = await context.new_page()
            await self._maximize_browser_window(page)

            try:
                logger.info(
                    "IcrisNnc1FormBot: 打开 ICRIS3EP 登录页 (CDP=%s)",
                    not force_isolated,
                )
                await page.goto(LOGIN_URL, wait_until="commit", timeout=90000)
                if not await wait_portal_ready(page, timeout_ms=90000):
                    if is_cr_public_site(page.url):
                        raise RuntimeError(
                            "门户被重定向到公开站 — 请使用 CDP 指纹浏览器并确保香港出口 IP"
                        )
                    raise RuntimeError("ICRIS3EP 登录页未就绪（可能仍在載入中）")
                await dismiss_cookie_banner(page)
                await page.wait_for_selector(
                    "input[type='password'], input[placeholder*='用户'], input[placeholder*='用戶']",
                    timeout=60000,
                )

                await self._login(page, account)
                await self._wait_login_complete(page)
                await self._wait_dashboard(page)
                await dismiss_cookie_banner(page)
                await dismiss_google_translate(page)

                if not await ensure_simplified_chinese(page, allow_url_fallback=False):
                    await page.wait_for_timeout(3000)
                    if not await ensure_simplified_chinese(page, allow_url_fallback=False):
                        probe = await page.evaluate(
                            """() => {
                                const t = document.body?.innerText || '';
                                return {
                                    simplified: /公司注册处|用户|首页|实用资讯/.test(t)
                                        && !/公司註冊處|實用資訊|主頁/.test(t),
                                    dashboard: /登出|成立公司|提交文件|查册|查冊/.test(t),
                                    sample: t.slice(0, 200),
                                };
                            }"""
                        )
                        if probe.get("simplified"):
                            logger.warning("语言切换未成功，但页面似为简体，继续")
                        elif probe.get("dashboard"):
                            # NNC1 菜单/表单已兼容简繁；繁体仪表盘可继续填表
                            logger.warning(
                                "未能切换简体，仪表盘已可用（繁体），继续 NNC1: %s",
                                (probe.get("sample") or "")[:80],
                            )
                        else:
                            await self._maybe_screenshot(page, "lang_fail")
                            raise RuntimeError("无法切换为简体中文")

                if await self._on_nnc1_form_page(page):
                    logger.info("已在 NNC1 填表页，跳过菜单导航: %s", page.url[:120])
                else:
                    await self._open_nnc1(page)
                    await self._accept_efiling_terms(page)
                await self._maybe_screenshot(page, "after_terms")

                await self._fill_step1_basic_info(page, data)
                await self._fill_step2_company_info(page, data)

                logger.info("IcrisNnc1FormBot: NNC1 步骤1-2 填表完成")
                return True, screenshot_path or ""
            except Exception as e:
                logger.exception("IcrisNnc1FormBot 失败")
                await self._maybe_screenshot(page, "error")
                return False, str(e)
            finally:
                if screenshot_path and not page.is_closed():
                    try:
                        await page.screenshot(path=screenshot_path, full_page=True)
                    except Exception as shot_err:
                        logger.warning("最终截图失败: %s", shot_err)
                await browser.close()
