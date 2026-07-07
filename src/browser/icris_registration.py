"""ICRIS 账号注册浏览器自动化（不提交）"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
import time
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse

from config.settings import settings
from src.browser.icris_captcha import fill_captcha as fill_icris_captcha
from src.browser.launcher import create_browser_context, launch_browser
from src.llm.openai_client import LLMClient

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

REGISTRATION_BASE = (
    "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do"
)
PORTAL_URL = "https://www.e-services.cr.gov.hk/"


def build_registration_url(systemclock: str | int | None = None) -> str:
    """
    构建注册 URL。systemclock 必须来自门户会话，自行伪造会被重定向到 cr.gov.hk 首页。
    """
    clock = str(systemclock or int(time.time() * 1000))
    return (
        f"{REGISTRATION_BASE}"
        f"?systemclock={clock}&webEnv=PROD&isOnsite=false&inactiveTime=0"
    )


def append_random_username_suffix(username: str, length: int = 2) -> str:
    """在用户名末尾追加随机字母数字，避免 mock 注册时用户名重复"""
    base = (username or "").strip()
    if not base or length <= 0:
        return base
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{base}{suffix}"


def derive_icris_credentials(data: dict[str, Any]) -> tuple[str, str]:
    """从 mock 数据生成 ICRIS 用户名与符合规则的密码"""
    acct = data.get("icris_account", {})
    applicant = data.get("applicant", {})

    username = (acct.get("username") or "").strip()
    if not username:
        email = applicant.get("email", "")
        if "@" in email:
            username = email.split("@", 1)[0]
        else:
            parts = re.sub(r"[^A-Za-z0-9]", "", applicant.get("name_en", "user"))
            username = parts.lower() or "icrisuser"

    username = append_random_username_suffix(username, length=2)

    password = ensure_icris_password(acct.get("password") or data.get("password_hint", ""))
    return username, password


def ensure_icris_password(raw: str) -> str:
    """
    ICRIS 密码规则：10 位以上，首字母大写，同时包含字母和数字。
    """
    default = "Chan2026Pass"
    candidate = (raw or "").strip()
    if _password_meets_rules(candidate):
        return candidate

    # 从申请人姓名派生，如 CHAN Tai Man -> Chan2026Man
    if candidate:
        letters = "".join(c for c in candidate if c.isalpha())[:6]
        if letters:
            derived = letters[0].upper() + letters[1:].lower() + "2026Pass"
            if _password_meets_rules(derived):
                return derived

    return default


def _password_meets_rules(password: str) -> bool:
    return (
        len(password) >= 10
        and password[0].isupper()
        and any(c.isalpha() for c in password)
        and any(c.isdigit() for c in password)
    )


class IcrisRegistrationBot:
    """ICRIS 电子服务账号注册 - 自动填写但不提交"""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()
        self.dry_run = settings.dry_run

    async def _log_page(self, page: "Page", label: str) -> None:
        logger.info("[%s] URL: %s", label, page.url)

    async def _wait_registration_vue(self, page: "Page", timeout: int = 90000) -> bool:
        """等待注册条款页 Vue 挂载（checkbox / 验证码 / URL hash）"""
        try:
            await page.wait_for_function(
                """() => {
                    if (window.location.href.includes('#')) return true;
                    if (document.querySelector('input[type=checkbox], #checkCode')) return true;
                    return Array.from(document.querySelectorAll('form')).some(
                        (f) => /registration/i.test(f.getAttribute('action') || '')
                    );
                }""",
                timeout=timeout,
            )
            await page.wait_for_timeout(800)
            return True
        except Exception:
            return False

    async def _wait_page_ready(self, page: "Page", timeout: int = 60000) -> None:
        """
        等待页面可操作。政府站点 pdf.mjs 会阻塞 domcontentloaded，
        注册流程页改用 commit + 等待 Vue 条款表单。
        """
        try:
            await page.wait_for_load_state("commit", timeout=5000)
        except Exception:
            pass

        if self._is_registration_page(page.url):
            if not await self._wait_registration_vue(page, timeout=timeout):
                logger.warning("注册页业务元素未在 %dms 内出现", timeout)
            return

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(15000, timeout))
        except Exception:
            logger.debug("domcontentloaded 超时，继续（政府站点常见）")
        await page.wait_for_timeout(800)

    async def _registration_terms_visible(self, page: "Page") -> bool:
        if not self._is_registration_page(page.url) or self._is_cr_public_site(page.url):
            return False
        # Vue 挂载后 URL 会追加 #，此时条款控件可能仍在渲染
        if "#" in page.url:
            return True
        return (
            await page.locator(
                "input[type='checkbox'], input#checkCode, form[action*='registration' i]"
            ).count()
            > 0
        )

    async def _open_registration_with_clock(
        self, page: "Page", systemclock: str
    ) -> "Page | None":
        """用门户 session 的 systemclock 打开注册条款页"""
        reg_url = build_registration_url(systemclock)
        for attempt in range(1, 3):
            logger.info("使用门户 systemclock 打开注册页 (尝试 %d/2): %s", attempt, reg_url)
            await page.goto(reg_url, wait_until="commit", timeout=60000)
            if not await self._wait_registration_vue(page, timeout=60000):
                logger.warning("注册页 Vue 未挂载 (尝试 %d/2)", attempt)
                if attempt < 2:
                    await page.reload(wait_until="commit", timeout=60000)
                continue
            await self._ensure_simplified_chinese(page)
            await self._log_page(page, "注册页加载")
            if await self._registration_terms_visible(page):
                return page
            logger.warning("注册页已加载但条款控件未就绪 (尝试 %d/2)", attempt)
        return None

    async def _wait_portal_register_control(self, page: "Page", timeout: int = 15000):
        """等待门户 Vue 渲染「立即登记」控件，返回可点击的 Locator 或 None"""
        pattern = re.compile(r"立即登[记記]")
        candidates = [
            page.get_by_role("button", name=pattern),
            page.get_by_role("link", name=pattern),
            page.locator("button, a").filter(has_text=pattern),
        ]
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for loc in candidates:
                if await loc.count() > 0:
                    return loc.first
            await page.wait_for_timeout(500)
        return None

    async def _click_portal_register(self, page: "Page") -> "Page | None":
        """点击门户「立即登记」，成功则返回注册页 Page"""
        register_btn = await self._wait_portal_register_control(page, timeout=15000)
        if register_btn is None:
            return None

        logger.info("点击「立即登记」")
        try:
            async with page.context.expect_page(timeout=8000) as new_page_info:
                await register_btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("commit", timeout=30000)
            page = new_page
            logger.info("立即登记在新标签页打开")
        except Exception:
            await register_btn.click()

        try:
            await page.wait_for_url("**/registration/**", timeout=30000)
            await self._wait_page_ready(page)
            await self._log_page(page, "立即登记后")
            if self._is_registration_page(page.url):
                return page
        except Exception as e:
            logger.warning("点击立即登记未进入 registration: %s", e)
        return None

    def _is_registration_page(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return "/registration/" in path and "s0" in path

    def _is_cr_public_site(self, url: str) -> bool:
        """是否被重定向到公司注册处公开网站（非 e-services 子域）"""
        host = urlparse(url).netloc.lower()
        if "e-services.cr.gov.hk" in host:
            return False
        return "cr.gov.hk" in host

    def _extract_systemclock(self, url: str) -> str | None:
        match = re.search(r"[?&]systemclock=(\d+)", url)
        return match.group(1) if match else None

    def _is_home_or_portal(self, url: str) -> bool:
        if self._is_registration_page(url):
            return False
        if self._is_cr_public_site(url):
            return True
        lower = url.lower()
        return any(
            token in lower
            for token in ("home.do", "/index", "/portal", "/main.do", "login.do")
        )

    async def _dismiss_portal_overlays(self, page: "Page") -> None:
        """关闭 Cookie 横幅、维护通知弹窗等遮挡层"""
        close_selectors = [
            "#notification-modal .close",
            "#notification-modal button.close",
            "#notification-modal [data-dismiss='modal']",
            ".modal.show .btn-close",
            ".modal.show button.close",
        ]
        for sel in close_selectors:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(500)
                logger.info("已关闭通知弹窗")
                break
        else:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        cookie_btn = page.locator(
            ".cookie-banner button, .cookie-bar button, "
            "[class*='cookie'] button:has-text('接受')"
        ).first
        if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
            await cookie_btn.click()
            await page.wait_for_timeout(500)
            logger.info("已接受 Cookie 横幅")

    async def _page_language_state(self, page: "Page") -> str:
        """返回页面语言状态: simplified | traditional | unknown"""
        return await page.evaluate(
            """() => {
                const text = document.body ? document.body.innerText : '';
                if (/用戶類別|擬訂用的服務|帳戶資料|公司註冊處|首頁/.test(text)) return 'traditional';
                if (/用户类别|拟订用的服务|账户资料|公司注册处|首页/.test(text)) return 'simplified';
                const header = document.querySelector('header, .header, #header');
                const headerText = header ? header.innerText : '';
                if (/公司註冊處|首頁/.test(headerText)) return 'traditional';
                if (/公司注册处|首页/.test(headerText)) return 'simplified';
                return 'unknown';
            }"""
        )

    async def _is_simplified_chinese_active(self, page: "Page") -> bool:
        state = await self._page_language_state(page)
        if state == "simplified":
            return True
        if state == "traditional":
            return False
        # 页头有「繁」可点、无繁体正文时，视为已是简体
        fan = page.locator("a").filter(has_text=re.compile(r"^繁$"))
        if await fan.count() > 0 and await fan.first.is_visible():
            return True
        return False

    async def _wait_language_simplified(self, page: "Page", timeout_ms: int = 15000) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    if (/用戶類別|擬訂用的服務|公司註冊處|首頁/.test(text)) return false;
                    return /用户类别|拟订用的服务|公司注册处|首页/.test(text);
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return await self._is_simplified_chinese_active(page)

    async def _find_jian_link_info(self, page: "Page") -> dict | None:
        """定位页头「简」语言链接元数据"""
        return await page.evaluate(
            """() => {
                const items = [];
                for (const el of document.querySelectorAll('a, button, span, li')) {
                    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    if (t !== '简') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 260) continue;
                    const link = el.closest('a') || (el.tagName === 'A' ? el : null);
                    const target = link || el;
                    items.push({
                        tag: target.tagName,
                        href: target.href || target.getAttribute('href') || '',
                        onclick: target.getAttribute('onclick') || '',
                        top: r.top,
                        left: r.left,
                        id: target.id || '',
                        cls: (target.className || '').slice(0, 80),
                    });
                }
                items.sort((a, b) => a.top - b.top || a.left - b.left);
                return items[0] || null;
            }"""
        )

    async def _activate_jian_link(self, page: "Page", info: dict) -> str:
        """尝试多种方式触发「简」切换，返回使用的方法名"""
        href = (info.get("href") or "").strip()
        if href and not href.lower().startswith("javascript"):
            await page.goto(href, wait_until="commit", timeout=60000)
            return f"goto:{href[:80]}"

        result = await page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('a, button, span, li')) {
                    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    if (t !== '简') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 260) continue;
                    const target = el.closest('a') || el;
                    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    if (typeof target.click === 'function') target.click();
                    return { ok: true, tag: target.tagName, href: target.href || '' };
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

    async def _ensure_simplified_chinese(self, page: "Page") -> bool:
        """点击页头右上角「简」切换为简体中文；已是简体则跳过"""
        state = await self._page_language_state(page)
        if state == "simplified" or await self._is_simplified_chinese_active(page):
            logger.info("页面已是简体中文，跳过语言切换")
            return True

        info = await self._find_jian_link_info(page)
        if not info:
            logger.warning("未找到页头「简」链接 (state=%s)", state)
            return False

        logger.info(
            "找到「简」入口: tag=%s href=%s class=%s",
            info.get("tag"),
            (info.get("href") or "")[:100],
            info.get("cls"),
        )

        for attempt in range(1, 4):
            try:
                method = await self._activate_jian_link(page, info)
                logger.info("已触发「简」切换 (尝试 %d/3, 方式=%s)", attempt, method)
                if await self._wait_language_simplified(page, timeout_ms=12000):
                    logger.info("语言已切换为简体中文")
                    return True
                await page.wait_for_timeout(800)
            except Exception as e:
                logger.warning("「简」切换尝试 %d 失败: %s", attempt, e)
                await page.wait_for_timeout(500)

        logger.warning("点击「简」后页面仍为繁体 (state=%s)", await self._page_language_state(page))
        return await self._fallback_locale_url(page)

    def _url_with_simplified_locale(self, url: str) -> str:
        """在 URL 上附加/覆盖简体 locale 参数"""
        parsed = urlparse(url)
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key in ("locale", "lang", "request_locale", "language"):
            if key in qs:
                qs[key] = "zh_CN"
                break
        else:
            qs["locale"] = "zh_CN"
        new_query = urlencode(qs)
        return parsed._replace(query=new_query).geturl()

    async def _fallback_locale_url(self, page: "Page") -> bool:
        """回退：通过 URL 参数 / Cookie 强制简体"""
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

        locale_url = self._url_with_simplified_locale(page.url)
        if locale_url != page.url:
            logger.info("回退：通过 URL 参数切换简体 %s", locale_url[:120])
            await page.goto(locale_url, wait_until="commit", timeout=60000)
            await page.wait_for_timeout(1500)
            if await self._wait_language_simplified(page, timeout_ms=10000):
                return True
        return await self._is_simplified_chinese_active(page)

    async def _navigate_to_registration(self, page: "Page") -> "Page | None":
        """
        进入注册页流程，成功返回当前 Page（可能是新标签页），失败返回 None。
        """
        logger.info("进入电子服务门户: %s", PORTAL_URL)
        for attempt in range(1, 3):
            try:
                await page.goto(
                    PORTAL_URL,
                    wait_until="commit",
                    timeout=90000,
                )
                break
            except Exception as e:
                logger.warning("门户页加载 (尝试 %d/2): %s", attempt, e)
                if "ERR_PROXY" in str(e):
                    logger.error(
                        "疑似系统代理导致连接失败，请确认 .env 中 BROWSER_NO_PROXY=true"
                    )
                    return None
                if attempt >= 2:
                    logger.error("无法打开门户页: %s", e)
                    return None
                await page.wait_for_timeout(2000)

        try:
            await page.wait_for_url("**/e-services.cr.gov.hk/**/home.do**", timeout=30000)
        except Exception:
            pass

        for _ in range(40):
            if "e-services.cr.gov.hk" in page.url and not self._is_cr_public_site(page.url):
                break
            await page.wait_for_timeout(500)
        else:
            await self._log_page(page, "门户加载失败")
            return None

        await self._log_page(page, "门户加载")
        if self._is_cr_public_site(page.url):
            logger.error("被 disable-devtool 重定向到公开网站: %s", page.url)
            return None

        await self._dismiss_portal_overlays(page)
        await self._ensure_simplified_chinese(page)

        # 等待 systemclock 出现（门户 Vue 渲染后 URL 会带上 clock）
        for _ in range(20):
            if self._extract_systemclock(page.url):
                break
            await page.wait_for_timeout(500)

        systemclock = self._extract_systemclock(page.url)

        # 优先直接打开注册页：久等「立即登记」会导致 systemclock 会话过期
        if systemclock:
            opened = await self._open_registration_with_clock(page, systemclock)
            if opened:
                return opened
            logger.warning("systemclock 直链未加载条款页，刷新门户会话后重试")
            await page.goto(PORTAL_URL, wait_until="commit", timeout=90000)
            for _ in range(20):
                if self._extract_systemclock(page.url):
                    break
                await page.wait_for_timeout(500)
            systemclock = self._extract_systemclock(page.url)
            if systemclock:
                opened = await self._open_registration_with_clock(page, systemclock)
                if opened:
                    return opened

        clicked = await self._click_portal_register(page)
        if clicked and await self._registration_terms_visible(clicked):
            return clicked

        logger.error("未能进入注册页，当前 URL: %s", page.url)
        return None

    async def _ensure_on_registration(self, page: "Page", step_label: str) -> bool:
        await self._log_page(page, step_label)
        if self._is_cr_public_site(page.url):
            logger.error("已跳转到公司注册处公开网站: %s", page.url)
            return False
        if self._is_home_or_portal(page.url):
            logger.error("已跳转到首页/门户，停止后续操作: %s", page.url)
            return False
        if not self._is_registration_page(page.url):
            logger.warning("当前不在 registration 流程页: %s", page.url)
        return True

    async def _fill_captcha(self, page: "Page") -> bool:
        return await fill_icris_captcha(page, self.llm)

    async def _get_terms_form(self, page: "Page"):
        """定位条款页表单，避免点到页头/页脚导航链接"""
        candidates = [
            "form[action*='registration' i]",
            "form[action*='s01' i]",
            "form:has(input[type='checkbox'])",
        ]
        for sel in candidates:
            form = page.locator(sel).first
            if await form.count() > 0:
                return form
        return page.locator("form").first

    async def _accept_terms(self, page: "Page") -> bool:
        """
        仅在 registration 表单内勾选条款并点击提交型「接受」按钮。
        不点击 <a> 链接，防止误触导航回首页。
        """
        form = await self._get_terms_form(page)
        if await form.count() == 0:
            logger.warning("未找到条款表单")
            return False

        checkboxes = form.locator("input[type='checkbox']")
        cb_count = await checkboxes.count()
        checked = False
        for i in range(cb_count):
            cb = checkboxes.nth(i)
            if not await cb.is_visible():
                continue
            if not await cb.is_checked():
                await cb.check()
            checked = True
            logger.info("已勾选条款复选框 (%d/%d)", i + 1, cb_count)

        if not checked:
            logger.warning("条款页未找到可勾选 checkbox")
            return False

        accept_selectors = [
            "input[type='submit'][value*='Accept' i]",
            "input[type='submit'][value*='接受']",
            "input[type='button'][value*='Accept' i]",
            "input[type='button'][value*='接受']",
            "button[type='submit']",
            "button:has-text('Accept')",
            "button:has-text('接 受')",
            "button:has-text('接受')",
        ]
        for sel in accept_selectors:
            btn = form.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                current_url = page.url
                await btn.click()
                logger.info("已点击条款接受按钮: %s", sel)

                try:
                    await page.wait_for_function(
                        "(url) => window.location.href !== url",
                        current_url,
                        timeout=15000,
                    )
                except Exception:
                    await self._wait_page_ready(page)

                await self._log_page(page, "接受条款后")
                if self._is_home_or_portal(page.url):
                    logger.error("点击接受后跳转到首页，可能验证码错误或会话失效")
                    return False
                return True

        logger.warning("条款表单内未找到 Accept/接受 按钮")
        return False

    def _is_account_profile_url(self, url: str) -> bool:
        lower = url.lower()
        return bool(re.search(r"registration/s0[2-9]", lower) or re.search(r"[#/]s0[2-9]", lower))

    async def _is_account_profile_step(self, page: "Page") -> bool:
        """是否为账户资料步骤（URL s02+ 或页面含用户类别）"""
        if self._is_account_profile_url(page.url):
            return True
        return bool(
            await page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText : '';
                    return /用户类别|用戶類別|账户资料|帳戶資料|拟订用的服务|擬訂用的服務/.test(t);
                }"""
            )
        )

    async def _wait_for_account_profile_step(self, page: "Page", timeout_ms: int = 60000) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const href = window.location.href;
                    if (/registration\\/s0[2-9]/i.test(href) || /[#/]s0[2-9]/i.test(href)) return true;
                    const t = document.body ? document.body.innerText : '';
                    return /用户类别|用戶類別|账户资料|帳戶資料/.test(t);
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return await self._is_account_profile_step(page)

    async def _is_user_profile_step(self, page: "Page") -> bool:
        """兼容旧名：账户资料步骤"""
        return await self._is_account_profile_step(page)

    async def _fill_ant_form_by_label(self, page: "Page", label_pattern: str, value: str) -> bool:
        """通过 ant-form-item 标签填写（兼容 Vue 双向绑定）"""
        if not value:
            return False
        ok = await page.evaluate(
            """([pat, val]) => {
                const re = new RegExp(pat, 'i');
                const setNativeValue = (el, v) => {
                    const Ctor = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
                    const setter = Object.getOwnPropertyDescriptor(Ctor.prototype, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const items = document.querySelectorAll('.ant-form-item, .form-group, tr, .ant-row');
                for (const item of items) {
                    const label = item.querySelector(
                        '.ant-form-item-label, label, .control-label, th, .label'
                    );
                    const labelText = label ? (label.innerText || '').trim() : '';
                    if (!labelText || !re.test(labelText)) continue;
                    const inp = item.querySelector(
                        "input:not([type='checkbox']):not([type='radio']):not([type='hidden']), textarea, .ant-input"
                    );
                    if (!inp) continue;
                    inp.focus();
                    setNativeValue(inp, val);
                    return true;
                }
                return false;
            }""",
            [label_pattern, value],
        )
        if ok:
            logger.info("已填写表单项 [%s]", label_pattern)
            return True

        section = await self._get_ant_form_item_by_label(page, label_pattern)
        inp = section.locator(
            "input:not([type='checkbox']):not([type='radio']):not([type='hidden']), textarea, .ant-input"
        ).first
        if await inp.count() > 0:
            try:
                await inp.scroll_into_view_if_needed()
                await inp.click()
                await inp.fill(value)
                await inp.dispatch_event("input")
                await inp.dispatch_event("change")
                logger.info("Playwright 已填写表单项 [%s]", label_pattern)
                return True
            except Exception:
                pass
        return False

    async def _wait_for_account_form_ready(self, page: "Page", timeout_ms: int = 45000) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const hasNative = !!document.querySelector('#userType, #userId, #password');
                    const hasMarker = /用户类别|用戶類別|拟订用的服务|擬訂用的服務/.test(
                        document.body ? document.body.innerText : ''
                    );
                    return hasMarker && hasNative;
                }""",
                timeout=timeout_ms,
            )
            await page.wait_for_timeout(600)
            return True
        except Exception:
            return False

    async def _get_account_profile_status(self, page: "Page") -> dict[str, bool]:
        return await page.evaluate(
            """() => {
                const userType = document.querySelector('#userType');
                let userCategory = false;
                if (userType) {
                    const opt = userType.options[userType.selectedIndex];
                    userCategory = userType.value === '0' || /个人|個人/.test((opt && opt.textContent) || '');
                }
                const checked = v => {
                    const el = document.querySelector(`input[type=checkbox][value="${v}"]`);
                    return !!(el && el.checked);
                };
                const radioChecked = (n, v) => {
                    const el = document.querySelector(`input[type=radio][name="${n}"][value="${v}"]`);
                    return !!(el && el.checked);
                };
                const val = id => {
                    const el = document.querySelector(`#${id}`);
                    return !!(el && el.value);
                };
                return {
                    userCategory,
                    electronicSubmit: checked('filing'),
                    electronicSearch: checked('search'),
                    primaryAccount: radioChecked('serviceType', 'principal'),
                    username: val('userId'),
                    password: val('password'),
                    confirmPassword: val('confirm'),
                };
            }"""
        )

    async def _log_account_profile_status(self, page: "Page", prefix: str = "") -> None:
        status = await self._get_account_profile_status(page)
        logger.info("%s账户资料状态: %s", prefix, status)

    async def _locate_user_category_select(self, page: "Page"):
        """定位「用户类别」下拉框"""
        patterns = [
            page.locator(".ant-select").filter(
                has=page.locator(
                    ".ant-select-selection-placeholder, .ant-select-selection-item"
                ).filter(has_text=re.compile(r"用户类别|用戶類別|个人|個人"))
            ),
            page.locator(".ant-form-item, .form-group, div").filter(
                has_text=re.compile(r"^用户类别$|^用戶類別$|用户类别|用戶類別")
            ).locator(".ant-select"),
            page.locator(".ant-select"),
        ]
        for loc in patterns:
            if await loc.count() > 0:
                return loc.first
        return page.locator(".ant-select").first

    async def _open_user_category_dropdown(self, page: "Page") -> bool:
        user_select = await self._locate_user_category_select(page)
        if await user_select.count() == 0:
            return False
        triggers = [
            user_select.locator(".ant-select-selector").first,
            user_select.locator(".ant-select-arrow").first,
            user_select.locator("[role='combobox']").first,
            user_select,
        ]
        for trigger in triggers:
            if await trigger.count() == 0:
                continue
            try:
                await trigger.scroll_into_view_if_needed()
                await trigger.click(force=True, timeout=5000)
                await page.wait_for_timeout(700)
                return True
            except Exception:
                continue
        return False

    async def _pick_select_option_individual(self, page: "Page") -> bool:
        option_selectors = [
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title='个人']",
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title='個人']",
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) [title='个人']",
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) [title='個人']",
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
            "[role='listbox'] [role='option']",
        ]
        opt_re = re.compile(r"^个人$|^個人$|Individual", re.I)
        for sel in option_selectors:
            options = page.locator(sel)
            generic_option = (
                "ant-select-item-option" in sel
                and "title" not in sel
                and "[role='option']" not in sel
            )
            for i in range(await options.count()):
                opt = options.nth(i)
                if not await opt.is_visible():
                    continue
                txt = (await opt.inner_text()).strip()
                if generic_option and not opt_re.search(txt):
                    continue
                try:
                    content = opt.locator(".ant-select-item-option-content").first
                    target = content if await content.count() > 0 else opt
                    await target.click(force=True, timeout=3000)
                    await page.wait_for_timeout(500)
                    return True
                except Exception:
                    continue
        return await page.evaluate(
            """() => {
                const optRe = /^(个人|個人|Individual)$/i;
                const dds = [...document.querySelectorAll('.ant-select-dropdown')]
                    .filter(d => d.offsetHeight > 0 && !d.classList.contains('ant-select-dropdown-hidden'));
                const dd = dds[dds.length - 1];
                if (!dd) return false;
                const opts = [...dd.querySelectorAll(
                    '[title], .ant-select-item-option, .ant-select-item, [role="option"]'
                )];
                for (const o of opts) {
                    const txt = (o.getAttribute('title') || o.innerText || '').trim();
                    if (!optRe.test(txt)) continue;
                    const content = o.querySelector('.ant-select-item-option-content') || o;
                    content.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    content.click();
                    return true;
                }
                if (opts[0]) {
                    opts[0].click();
                    return true;
                }
                return false;
            }"""
        )

    async def _scroll_registration_form_into_view(self, page: "Page") -> None:
        await page.evaluate(
            """() => {
                const form = document.querySelector('form, .ant-form, #app');
                if (form) form.scrollIntoView({ block: 'start', behavior: 'instant' });
            }"""
        )
        await page.wait_for_timeout(400)

    def _normalize_form_label(self, text: str) -> str:
        return re.sub(r"[\s\*:：]+", "", (text or "").strip())

    async def _get_ant_form_item_by_label(self, page: "Page", label_pattern: str):
        """按 .ant-form-item-label 文字定位表单项"""
        regex = re.compile(label_pattern, re.I)
        items = page.locator(".ant-form-item")
        for i in range(await items.count()):
            item = items.nth(i)
            label = item.locator(".ant-form-item-label, label")
            if await label.count() == 0:
                continue
            txt = (await label.first.inner_text()).strip()
            norm = self._normalize_form_label(txt)
            if regex.search(txt) or regex.search(norm):
                return item
        return await self._get_labeled_section(page, label_pattern)

    async def _verify_user_category_individual(self, page: "Page") -> bool:
        return bool(
            await page.evaluate(
                """() => {
                    const sel = document.querySelector('#userType, select[name="userType"], select[id*="userType" i]');
                    if (sel) {
                        const opt = sel.options[sel.selectedIndex];
                        const txt = opt ? (opt.textContent || '').trim() : '';
                        const val = sel.value;
                        return val === '0' || /^个人$|^個人$/i.test(txt);
                    }
                    const isIndividual = t => /^(个人|個人|Individual)$/i.test((t || '').trim());
                    const picked = document.querySelector('.ant-select-selection-item');
                    return !!(picked && isIndividual(picked.innerText));
                }"""
            )
        )

    async def _select_native_user_type_individual(self, page: "Page") -> bool:
        """用户类别：原生 select#userType，个人 value=0"""
        if await self._verify_user_category_individual(page):
            logger.info("用户类别已是「个人」(#userType)")
            return True

        select = page.locator("#userType, select[name='userType']").first
        if await select.count() == 0:
            return False

        for value, label in (("0", "个人"), ("0", "個人")):
            try:
                await select.scroll_into_view_if_needed()
                await select.select_option(value=value)
                await select.dispatch_event("change")
                await page.wait_for_timeout(400)
                if await self._verify_user_category_individual(page):
                    logger.info("已通过 #userType 选择个人 (value=%s)", value)
                    return True
            except Exception:
                pass
            try:
                await select.select_option(label=label)
                await select.dispatch_event("change")
                await page.wait_for_timeout(400)
                if await self._verify_user_category_individual(page):
                    logger.info("已通过 #userType 选择个人 (label=%s)", label)
                    return True
            except Exception:
                pass

        ok = await page.evaluate(
            """() => {
                const sel = document.querySelector('#userType');
                if (!sel) return false;
                sel.value = '0';
                sel.dispatchEvent(new Event('input', { bubbles: true }));
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return sel.value === '0';
            }"""
        )
        if ok and await self._verify_user_category_individual(page):
            logger.info("JS 已设置 #userType=0 (个人)")
            return True
        return False

    async def _check_native_checkbox_by_value(self, page: "Page", value: str) -> bool:
        cb = page.locator(f"input[type='checkbox'][value='{value}']").first
        if await cb.count() == 0:
            return False
        if await cb.is_checked():
            return True
        try:
            await cb.scroll_into_view_if_needed()
            await cb.check(force=True)
            await cb.dispatch_event("change")
            return await cb.is_checked()
        except Exception:
            return bool(
                await page.evaluate(
                    """(v) => {
                        const cb = document.querySelector(`input[type=checkbox][value="${v}"]`);
                        if (!cb) return false;
                        cb.checked = true;
                        cb.dispatchEvent(new Event('input', { bubbles: true }));
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                        return cb.checked;
                    }""",
                    value,
                )
            )

    async def _select_native_radio_by_value(self, page: "Page", name: str, value: str) -> bool:
        radio = page.locator(f"input[type='radio'][name='{name}'][value='{value}']").first
        if await radio.count() == 0:
            return False
        if await radio.is_checked():
            return True
        try:
            await radio.scroll_into_view_if_needed()
            await radio.check(force=True)
            await radio.dispatch_event("change")
            return await radio.is_checked()
        except Exception:
            return bool(
                await page.evaluate(
                    """([n, v]) => {
                        const r = document.querySelector(`input[type=radio][name="${n}"][value="${v}"]`);
                        if (!r) return false;
                        r.checked = true;
                        r.dispatchEvent(new Event('input', { bubbles: true }));
                        r.dispatchEvent(new Event('change', { bubbles: true }));
                        return r.checked;
                    }""",
                    [name, value],
                )
            )

    async def _fill_native_input(self, page: "Page", selector: str, value: str) -> bool:
        if not value:
            return False
        inp = page.locator(selector).first
        if await inp.count() == 0:
            return False
        try:
            await inp.scroll_into_view_if_needed()
            await inp.click()
            await inp.fill(value)
            await inp.dispatch_event("input")
            await inp.dispatch_event("change")
            actual = await inp.input_value()
            if actual == value:
                logger.info("已填写 %s", selector)
                return True
        except Exception:
            pass
        return bool(
            await page.evaluate(
                """([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const Ctor = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
                    const setter = Object.getOwnPropertyDescriptor(Ctor.prototype, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return el.value === val;
                }""",
                [selector, value],
            )
        )

    async def _wait_for_principal_account_enabled(self, page: "Page", timeout_ms: int = 5000) -> bool:
        """勾选电子查册后，等待「主要账户」单选可用"""
        try:
            await page.wait_for_function(
                """() => {
                    const search = document.querySelector('input[type=checkbox][value="search"]');
                    const principal = document.querySelector(
                        'input[type=radio][name="serviceType"][value="principal"]'
                    );
                    return search && search.checked && principal && !principal.disabled;
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return bool(
                await page.evaluate(
                    """() => {
                        const search = document.querySelector('input[type=checkbox][value="search"]');
                        const principal = document.querySelector(
                            'input[type=radio][name="serviceType"][value="principal"]'
                        );
                        return !!(search && search.checked && principal);
                    }"""
                )
            )

    async def _select_principal_account_after_search(self, page: "Page") -> bool:
        """先确保勾选电子查册，再选择其下的主要账户"""
        if not await self._check_native_checkbox_by_value(page, "search"):
            logger.warning("未能勾选电子查册，无法选择主要账户")
            return False
        logger.info("已勾选电子查册 (value=search)")
        await page.wait_for_timeout(500)
        await self._wait_for_principal_account_enabled(page)

        for attempt in range(3):
            if await self._select_native_radio_by_value(page, "serviceType", "principal"):
                logger.info("已选择主要账户 (serviceType=principal)")
                return True
            await page.wait_for_timeout(400)
        return False

    async def _fill_account_profile_native(self, page: "Page", data: dict[str, Any]) -> int:
        """按 s02 页面真实 DOM：原生 select/checkbox/radio/input"""
        username, password = derive_icris_credentials(data)
        logger.info("注册用户名(含随机后缀): %s", username)
        filled = 0

        if await self._select_native_user_type_individual(page):
            filled += 1

        if await self._check_native_checkbox_by_value(page, "filing"):
            logger.info("已勾选电子提交 (value=filing)")
            filled += 1

        if await self._select_principal_account_after_search(page):
            filled += 2

        field_steps = [
            ("#userId", username),
            ("#password", password),
            ("#confirm", password),
        ]
        for selector, value in field_steps:
            if await self._fill_native_input(page, selector, value):
                filled += 1

        return filled

    async def _log_user_category_dom(self, page: "Page") -> None:
        info = await page.evaluate(
            """() => {
                const items = [...document.querySelectorAll('.ant-form-item')].slice(0, 8).map(item => {
                    const lbl = item.querySelector('.ant-form-item-label, label');
                    const sel = item.querySelector('.ant-select');
                    return {
                        label: lbl ? (lbl.innerText || '').trim() : '',
                        hasSelect: !!sel,
                        selectText: sel ? (sel.innerText || '').trim().slice(0, 80) : '',
                        classes: sel ? sel.className : '',
                    };
                });
                const opts = [...document.querySelectorAll(
                    '.ant-select-item-option, .ant-select-item, [role="option"]'
                )].map(o => (o.innerText || '').trim()).filter(Boolean).slice(0, 15);
                return { items, visibleOptions: opts };
            }"""
        )
        logger.warning("用户类别 DOM 诊断: %s", info)

    async def _verify_dropdown_selected(
        self, page: "Page", label_pattern: str, option_pattern: str
    ) -> bool:
        if re.search(r"用户类别|用戶類別", label_pattern, re.I) and re.search(
            r"个人|個人", option_pattern, re.I
        ):
            return await self._verify_user_category_individual(page)

        section = await self._get_ant_form_item_by_label(page, label_pattern)
        opt_re = re.compile(option_pattern, re.I)
        select = section.locator(".ant-select, .ant-select-selector").first
        if await select.count() > 0:
            txt = (await select.inner_text()).strip()
            if opt_re.search(txt) and not re.search(
                r"^用户类别$|^用戶類別$", self._normalize_form_label(txt), re.I
            ):
                return True
        for sel in (
            ".ant-select-selection-item",
            ".ant-select-selection-item-content",
            ".ant-select-selection-selected-value",
            ".ant-select-selection__rendered",
        ):
            loc = section.locator(sel)
            if await loc.count() > 0:
                txt = (await loc.first.inner_text()).strip()
                if opt_re.search(txt):
                    return True
        return False

    async def _click_visible_select_option(
        self, page: "Page", option_pattern: str
    ) -> bool:
        """点击当前可见下拉列表中的选项"""
        opt_re = re.compile(option_pattern, re.I)
        dropdown_selectors = [
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
            ".rc-select-dropdown:not(.rc-select-dropdown-hidden)",
            "[class*='select-dropdown']:not([class*='hidden'])",
        ]
        for dd_sel in dropdown_selectors:
            dropdown = page.locator(dd_sel).last
            if await dropdown.count() == 0:
                continue
            option_selectors = [
                ".ant-select-item-option",
                ".ant-select-item",
                ".rc-select-item-option",
                "[role='option']",
                "li",
            ]
            for opt_sel in option_selectors:
                options = dropdown.locator(opt_sel)
                for i in range(await options.count()):
                    opt = options.nth(i)
                    if not await opt.is_visible():
                        continue
                    txt = (await opt.inner_text()).strip()
                    if not opt_re.search(txt):
                        continue
                    try:
                        content = opt.locator(
                            ".ant-select-item-option-content, .rc-select-item-option-content"
                        ).first
                        target = content if await content.count() > 0 else opt
                        await target.scroll_into_view_if_needed()
                        await target.click(force=True, timeout=3000)
                        await page.wait_for_timeout(500)
                        return True
                    except Exception:
                        try:
                            await opt.click(force=True, timeout=3000)
                            await page.wait_for_timeout(500)
                            return True
                        except Exception:
                            continue
        return False

    async def _open_labeled_select(self, page: "Page", label_pattern: str) -> bool:
        section = await self._get_ant_form_item_by_label(page, label_pattern)
        triggers = [
            section.locator(".ant-select-arrow").first,
            section.locator(".ant-select-selector").first,
            section.locator(".ant-select").first,
            section.locator("[role='combobox']").first,
        ]
        for trigger in triggers:
            if await trigger.count() == 0 or not await trigger.is_visible():
                continue
            try:
                await trigger.scroll_into_view_if_needed()
                await trigger.click(force=True, timeout=5000)
                return True
            except Exception:
                continue

        return bool(
            await page.evaluate(
                """(labelPat) => {
                    const labelRe = new RegExp(labelPat, 'i');
                    const norm = s => (s || '').replace(/[\\s:*：]/g, '');
                    const items = [...document.querySelectorAll('.ant-form-item')];
                    const item = items.find(el => {
                        const lbl = el.querySelector('.ant-form-item-label, label');
                        return lbl && (labelRe.test(lbl.innerText || '') || labelRe.test(norm(lbl.innerText)));
                    });
                    const scope = item || document.body;
                    const select = scope.querySelector('.ant-select');
                    if (!select) return false;
                    const trigger = select.querySelector('.ant-select-arrow, .ant-select-selector') || select;
                    trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                    trigger.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                    trigger.click();
                    return true;
                }""",
                label_pattern,
            )
        )

    async def _select_ant_dropdown_by_label(
        self,
        page: "Page",
        label_pattern: str,
        option_pattern: str,
    ) -> bool:
        """Ant Design Select：按表单项标签打开下拉并选择选项（如 用户类别 → 个人）"""
        if await self._verify_dropdown_selected(page, label_pattern, option_pattern):
            logger.info("下拉已选 [%s] → %s", label_pattern, option_pattern)
            return True

        opt_re = re.compile(option_pattern, re.I)

        # 策略 1：get_by_label + role=option
        try:
            label_re = re.compile(label_pattern, re.I)
            form_item = page.locator(".ant-form-item").filter(
                has=page.locator(".ant-form-item-label, label").filter(has_text=label_re)
            ).first
            if await form_item.count() > 0:
                combo = form_item.locator("[role='combobox'], .ant-select-selector, .ant-select").first
                await combo.scroll_into_view_if_needed()
                await combo.click(force=True, timeout=5000)
                await page.wait_for_timeout(700)
                role_opt = page.get_by_role("option", name=opt_re)
                if await role_opt.count() > 0:
                    await role_opt.first.click(force=True, timeout=3000)
                    await page.wait_for_timeout(500)
                    if await self._verify_dropdown_selected(page, label_pattern, option_pattern):
                        logger.info("role 已选择下拉 [%s] → %s", label_pattern, option_pattern)
                        return True
        except Exception as exc:
            logger.debug("role 下拉选择失败: %s", exc)

        # 策略 2：打开下拉 + 点击可见选项
        for attempt in range(3):
            if not await self._open_labeled_select(page, label_pattern):
                await page.wait_for_timeout(400)
                continue
            await page.wait_for_timeout(700)
            try:
                await page.wait_for_selector(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden), "
                    ".rc-select-dropdown:not(.rc-select-dropdown-hidden)",
                    timeout=4000,
                )
            except Exception:
                pass
            if await self._click_visible_select_option(page, option_pattern):
                if await self._verify_dropdown_selected(page, label_pattern, option_pattern):
                    logger.info("已选择下拉 [%s] → %s", label_pattern, option_pattern)
                    return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        # 策略 3：键盘选择（个人通常是第一项）
        try:
            section = await self._get_ant_form_item_by_label(page, label_pattern)
            combo = section.locator(".ant-select-selector, .ant-select, [role='combobox']").first
            if await combo.count() > 0:
                await combo.click(force=True)
                await page.wait_for_timeout(500)
                await page.keyboard.press("ArrowDown")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)
                if await self._verify_dropdown_selected(page, label_pattern, option_pattern):
                    logger.info("键盘已选择下拉 [%s] → %s", label_pattern, option_pattern)
                    return True
        except Exception as exc:
            logger.debug("键盘下拉选择失败: %s", exc)

        # 策略 4：JS 一次性打开并点击
        ok = await page.evaluate(
            """([labelPat, optPat]) => {
                const labelRe = new RegExp(labelPat, 'i');
                const optRe = new RegExp(optPat, 'i');
                const norm = s => (s || '').replace(/[\\s:*：]/g, '');
                const isPlaceholder = t => /^用户类别$|^用戶類別$/i.test(norm(t));

                const items = [...document.querySelectorAll('.ant-form-item')];
                let item = items.find(el => {
                    const lbl = el.querySelector('.ant-form-item-label, label');
                    return lbl && (labelRe.test(lbl.innerText || '') || labelRe.test(norm(lbl.innerText)));
                });
                if (!item) item = items.find(el => el.querySelector('.ant-select'));

                const select = item ? item.querySelector('.ant-select') : document.querySelector('.ant-select');
                if (!select) return false;

                const trigger = select.querySelector('.ant-select-selector, .ant-select-arrow') || select;
                trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                trigger.click();

                const dropdowns = [...document.querySelectorAll('.ant-select-dropdown, .rc-select-dropdown')]
                    .filter(d => !d.classList.contains('ant-select-dropdown-hidden')
                        && !d.classList.contains('rc-select-dropdown-hidden'));
                const dd = dropdowns[dropdowns.length - 1];
                if (!dd) return false;

                const opts = [...dd.querySelectorAll(
                    '.ant-select-item-option, .ant-select-item, .rc-select-item-option, [role="option"]'
                )];
                for (const o of opts) {
                    const content = o.querySelector('.ant-select-item-option-content') || o;
                    const txt = (content.innerText || o.innerText || '').trim();
                    if (!optRe.test(txt) || isPlaceholder(txt)) continue;
                    content.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    content.click();
                    o.click();
                    return true;
                }
                if (opts[0]) {
                    opts[0].click();
                    return true;
                }
                return false;
            }""",
            [label_pattern, option_pattern],
        )
        if ok:
            await page.wait_for_timeout(600)
            if await self._verify_dropdown_selected(page, label_pattern, option_pattern):
                logger.info("JS 已选择下拉 [%s] → %s", label_pattern, option_pattern)
                return True

        await self._log_user_category_dom(page)
        logger.warning("未能选择下拉 [%s] → %s", label_pattern, option_pattern)
        return False

    async def _verify_checkbox_checked(
        self, page: "Page", text_pattern: str, *, scope=None
    ) -> bool:
        pattern = re.compile(text_pattern, re.I)
        root = scope if scope is not None else page
        checked = root.locator(
            ".ant-checkbox-wrapper-checked, .ant-checkbox-checked"
        ).filter(has_text=pattern)
        return await checked.count() > 0

    async def _ensure_checkbox_in_section(
        self,
        page: "Page",
        section_label: str,
        checkbox_pattern: str,
    ) -> bool:
        """在指定区域内勾选复选框（如 拟订用的服务 → 电子提交）"""
        section = await self._get_labeled_section(page, section_label)
        cb_re = re.compile(checkbox_pattern, re.I)

        if await self._verify_checkbox_checked(page, checkbox_pattern, scope=section):
            logger.info("已勾选 [%s] %s", section_label, checkbox_pattern)
            return True

        wrappers = section.locator(".ant-checkbox-wrapper").filter(has_text=cb_re)
        for i in range(await wrappers.count()):
            item = wrappers.nth(i)
            if not await item.is_visible():
                continue
            txt = (await item.inner_text()).strip()
            if not cb_re.search(txt) or len(txt) > 30:
                continue
            try:
                await item.scroll_into_view_if_needed()
                await item.click(timeout=3000)
                await page.wait_for_timeout(400)
                if await self._verify_checkbox_checked(page, checkbox_pattern, scope=section):
                    logger.info("已勾选 [%s] → %s", section_label, checkbox_pattern)
                    return True
            except Exception:
                pass
            inp = item.locator("input[type='checkbox']").first
            if await inp.count() > 0:
                try:
                    await inp.check(force=True)
                    await inp.dispatch_event("change")
                    await page.wait_for_timeout(400)
                    if await self._verify_checkbox_checked(
                        page, checkbox_pattern, scope=section
                    ):
                        logger.info("已 force 勾选 [%s] → %s", section_label, checkbox_pattern)
                        return True
                except Exception:
                    pass

        ok = await page.evaluate(
            """([sectionLabel, cbText]) => {
                const secRe = new RegExp(sectionLabel, 'i');
                const cbRe = new RegExp(cbText, 'i');
                const blocks = [...document.querySelectorAll('.ant-form-item, fieldset, .form-group')];
                const section = blocks.find(el => secRe.test(el.innerText || ''));
                const scope = section || document.body;
                const boxes = [...scope.querySelectorAll('.ant-checkbox-wrapper')];
                for (const w of boxes) {
                    const t = (w.innerText || '').trim();
                    if (!cbRe.test(t) || t.length > 30) continue;
                    w.click();
                    const inp = w.querySelector("input[type='checkbox']");
                    if (inp) {
                        inp.checked = true;
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    if (w.classList.contains('ant-checkbox-wrapper-checked')) return true;
                }
                return false;
            }""",
            [section_label, checkbox_pattern],
        )
        if ok:
            logger.info("JS 已勾选 [%s] → %s", section_label, checkbox_pattern)
            return True
        logger.warning("未能勾选 [%s] → %s", section_label, checkbox_pattern)
        return False

    async def _verify_option_selected(
        self,
        page: "Page",
        text_pattern: str,
        *,
        option_type: str = "radio",
    ) -> bool:
        pattern = re.compile(text_pattern, re.I)
        if option_type == "radio":
            checked = page.locator(".ant-radio-wrapper-checked, .ant-radio-checked").filter(
                has_text=pattern
            )
            if await checked.count() > 0:
                return True
            inp = page.locator("input[type='radio']:checked")
            for i in range(await inp.count()):
                el = inp.nth(i)
                wrapper = el.locator(
                    "xpath=ancestor::label[contains(@class,'radio')]"
                    " | ancestor::*[contains(@class,'ant-radio-wrapper')][1]"
                )
                if await wrapper.count() > 0:
                    txt = await wrapper.first.inner_text()
                    if pattern.search(txt):
                        return True
        else:
            checked = page.locator(
                ".ant-checkbox-wrapper-checked, .ant-checkbox-checked"
            ).filter(has_text=pattern)
            if await checked.count() > 0:
                return True
        return False

    async def _get_labeled_section(self, page: "Page", label_pattern: str):
        """定位包含指定标签文字的表单项区域"""
        regex = re.compile(label_pattern, re.I)
        selectors = [
            ".ant-form-item",
            ".ant-row.ant-form-item",
            ".form-group",
            "tr",
            "fieldset",
            "div.section",
        ]
        for sel in selectors:
            items = page.locator(sel).filter(has_text=regex)
            for i in range(await items.count()):
                item = items.nth(i)
                if not await item.is_visible():
                    continue
                txt = await item.inner_text()
                if regex.search(txt) and len(txt) < 800:
                    return item
        return page.locator("form, body").first

    async def _select_radio_in_section(
        self,
        page: "Page",
        section_label: str,
        option_pattern: str,
    ) -> bool:
        """在指定标签区域内选择单选项（如：用户类别 → 个人）"""
        section = await self._get_labeled_section(page, section_label)
        opt_re = re.compile(option_pattern, re.I)

        if await self._verify_option_selected(page, option_pattern, option_type="radio"):
            logger.info("已选中 [%s] %s（无需重复点击）", section_label, option_pattern)
            return True

        candidates = [
            section.locator(".ant-radio-wrapper").filter(has_text=opt_re),
            section.locator("label.ant-radio-wrapper").filter(has_text=opt_re),
            section.locator("label").filter(has_text=opt_re),
            section.get_by_role("radio", name=opt_re),
        ]

        for loc in candidates:
            for i in range(await loc.count()):
                item = loc.nth(i)
                if not await item.is_visible():
                    continue
                txt = (await item.inner_text()).strip()
                if re.search(r"机构|機構|团体|團體|法人|公司|组织|組織", txt, re.I):
                    continue
                try:
                    await item.scroll_into_view_if_needed()
                    await item.click(timeout=3000)
                    await page.wait_for_timeout(400)
                    if await self._verify_option_selected(page, option_pattern, option_type="radio"):
                        logger.info("已选择 [%s] → %s", section_label, option_pattern)
                        return True
                except Exception:
                    pass

                # Ant Design 隐藏 input，force check
                inp = item.locator("input[type='radio']").first
                if await inp.count() > 0:
                    try:
                        await inp.check(force=True)
                        await inp.dispatch_event("change")
                        await inp.dispatch_event("click")
                        await page.wait_for_timeout(400)
                        if await self._verify_option_selected(
                            page, option_pattern, option_type="radio"
                        ):
                            logger.info("已 force 选择 [%s] → %s", section_label, option_pattern)
                            return True
                    except Exception:
                        pass

        # JS 回退：在用户类别区域内点击「个人」
        ok = await page.evaluate(
            """([sectionLabel, optionText]) => {
                const secRe = new RegExp(sectionLabel, 'i');
                const optRe = new RegExp(optionText, 'i');
                const excludeRe = /机构|機構|团体|團體|法人|公司|组织|組織/i;
                const blocks = [...document.querySelectorAll(
                    '.ant-form-item, .ant-row, tr, fieldset, .form-group'
                )];
                const section = blocks.find(el => secRe.test(el.innerText || ''));
                const scope = section || document.body;
                const wrappers = [...scope.querySelectorAll('.ant-radio-wrapper, label')];
                for (const w of wrappers) {
                    const t = (w.innerText || '').trim();
                    if (!optRe.test(t) || excludeRe.test(t)) continue;
                    w.click();
                    const inp = w.querySelector('input[type=radio]') || w.control;
                    if (inp) {
                        inp.checked = true;
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    const checked = scope.querySelector(
                        '.ant-radio-wrapper-checked input[type=radio], input[type=radio]:checked'
                    );
                    if (checked) {
                        const wrap = checked.closest('.ant-radio-wrapper') || checked.parentElement;
                        return optRe.test((wrap && wrap.innerText) || '');
                    }
                }
                const radios = [...scope.querySelectorAll('input[type=radio]')];
                if (radios[0]) {
                    radios[0].click();
                    radios[0].checked = true;
                    radios[0].dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                return false;
            }""",
            [section_label, option_pattern],
        )
        if ok:
            logger.info("JS 已选择 [%s] → %s", section_label, option_pattern)
            await page.wait_for_timeout(400)
            return True

        logger.warning("未能选择 [%s] → %s", section_label, option_pattern)
        return False

    async def _select_user_category_individual(self, page: "Page") -> bool:
        """用户类别选择「个人」— 优先原生 #userType"""
        if await self._select_native_user_type_individual(page):
            return True

        # 回退：Ant Design 下拉（旧版页面）
        label_pat = r"用户类别|用戶類別"
        for option_pat in (r"^个人$", r"^個人$", r"个人|個人"):
            if await self._select_ant_dropdown_by_label(page, label_pat, option_pat):
                if await self._verify_user_category_individual(page):
                    return True
        await self._log_user_category_dom(page)
        return False

    async def _ensure_checkbox_by_text(self, page: "Page", text_pattern: str) -> bool:
        """全页按文字勾选复选框（电子提交/电子查册）"""
        pattern = re.compile(text_pattern, re.I)
        if await self._verify_checkbox_checked(page, text_pattern):
            return True
        wrappers = page.locator(".ant-checkbox-wrapper").filter(has_text=pattern)
        for i in range(await wrappers.count()):
            item = wrappers.nth(i)
            if not await item.is_visible():
                continue
            txt = (await item.inner_text()).strip()
            if len(txt) > 40:
                continue
            try:
                await item.scroll_into_view_if_needed()
                await item.click(force=True, timeout=3000)
                await page.wait_for_timeout(400)
                if await self._verify_checkbox_checked(page, text_pattern):
                    logger.info("已勾选复选框: %s", text_pattern)
                    return True
            except Exception:
                pass
        return await self._click_option_by_text(page, text_pattern, option_type="checkbox")

    async def _select_primary_account_radio(self, page: "Page") -> bool:
        """选择「主要账户」单选"""
        if await self._select_native_radio_by_value(page, "serviceType", "principal"):
            logger.info("已选择主要账户 (serviceType=principal)")
            return True
        if await self._verify_option_selected(page, r"主要账户|主要帳戶", option_type="radio"):
            return True
        for section in (
            r"电子查册|電子查冊",
            r"拟订用的服务|擬訂用的服務",
            r".*",
        ):
            if await self._select_radio_in_section(
                page, section, r"主要账户|主要帳戶|Primary"
            ):
                return True
        return await self._click_option_by_text(
            page, r"^主要账户$|^主要帳戶$", option_type="radio"
        )

    async def _click_option_by_text(
        self,
        page: "Page",
        text_pattern: str,
        *,
        option_type: str = "any",
    ) -> bool:
        """点击 Ant Design 单选/复选（按可见文字）"""
        pattern = re.compile(text_pattern, re.I)
        wrappers = [
            ".ant-radio-wrapper",
            ".ant-checkbox-wrapper",
            "label.ant-radio-wrapper",
            "label.ant-checkbox-wrapper",
            "label",
            "span.ant-radio + span",
        ]
        if option_type == "radio":
            wrappers = wrappers[:2] + ["label"]
        elif option_type == "checkbox":
            wrappers = [".ant-checkbox-wrapper", "label.ant-checkbox-wrapper", "label"]

        for sel in wrappers:
            loc = page.locator(sel).filter(has_text=pattern)
            count = await loc.count()
            for i in range(count):
                item = loc.nth(i)
                if not await item.is_visible():
                    continue
                try:
                    await item.scroll_into_view_if_needed()
                    await item.click(timeout=3000)
                    await page.wait_for_timeout(300)
                    logger.info("已选择: %s (%s)", text_pattern, sel)
                    return True
                except Exception:
                    continue

        role = "radio" if option_type == "radio" else "checkbox"
        if option_type in ("radio", "checkbox"):
            role_loc = page.get_by_role(role, name=pattern)
            if await role_loc.count() > 0:
                await role_loc.first.scroll_into_view_if_needed()
                await role_loc.first.click()
                logger.info("已选择(role): %s", text_pattern)
                return True

        text_loc = page.get_by_text(pattern).first
        if await text_loc.count() > 0 and await text_loc.is_visible():
            await text_loc.scroll_into_view_if_needed()
            await text_loc.click()
            logger.info("已点击文字: %s", text_pattern)
            return True

        if option_type == "checkbox":
            ok = await page.evaluate(
                """(pat) => {
                    const re = new RegExp(pat, 'i');
                    const boxes = [...document.querySelectorAll(
                        '.ant-checkbox-wrapper, label.ant-checkbox-wrapper, label'
                    )];
                    for (const w of boxes) {
                        const t = (w.innerText || '').trim();
                        if (!re.test(t)) continue;
                        w.click();
                        const inp = w.querySelector("input[type='checkbox']");
                        if (inp) {
                            inp.checked = true;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return true;
                    }
                    return false;
                }""",
                text_pattern,
            )
            if ok:
                logger.info("JS 已勾选: %s", text_pattern)
                return True
        return False

    async def _fill_input_near_label(
        self,
        page: "Page",
        label_patterns: list[str],
        value: str,
    ) -> bool:
        if not value:
            return False

        scope = page.locator("form").first
        if await scope.count() == 0:
            scope = page.locator("body")

        for pattern in label_patterns:
            regex = re.compile(pattern)
            labels = scope.locator("label, .ant-form-item-label, .form-label").filter(
                has_text=regex
            )
            for i in range(await labels.count()):
                lbl = labels.nth(i)
                if not await lbl.is_visible():
                    continue
                lbl_for = await lbl.get_attribute("for")
                if lbl_for:
                    inp = page.locator(f"#{lbl_for}")
                    if await inp.count() > 0:
                        await inp.fill(value)
                        logger.info("已填写 [%s]: %s", pattern, value)
                        return True
                container = lbl.locator(
                    "xpath=ancestor::div[contains(@class,'form-item') or "
                    "contains(@class,'ant-row')][1]"
                )
                inp = container.locator(
                    "input:not([type='checkbox']):not([type='radio']):not([type='hidden']), "
                    "textarea"
                ).first
                if await inp.count() > 0:
                    await inp.fill(value)
                    logger.info("已填写 [%s]: %s", pattern, value)
                    return True

        for pattern in label_patterns:
            for attr in ("name", "id", "placeholder", "aria-label"):
                inp = scope.locator(
                    f"input[{attr}*='{pattern}' i]:not([type='checkbox']):not([type='radio'])"
                )
                if await inp.count() > 0:
                    await inp.first.fill(value)
                    logger.info("已填写 input[%s]: %s", attr, value)
                    return True
        return False

    async def _fill_user_profile_step(self, page: "Page", data: dict[str, Any]) -> int:
        """
        填写账户资料：
        用户类别=个人 → 电子提交/电子查册 → 主要账户 → 用户名称/密码
        """
        if not await self._is_account_profile_step(page):
            await self._ensure_simplified_chinese(page)
            if not await self._wait_for_account_profile_step(page, timeout_ms=30000):
                logger.warning("当前不在账户资料步骤, url=%s", page.url)
                return 0

        await self._ensure_simplified_chinese(page)
        await self._scroll_registration_form_into_view(page)
        await self._wait_registration_vue(page, timeout=30000)
        if not await self._wait_for_account_form_ready(page):
            logger.warning("账户资料表单控件未就绪, url=%s", page.url)
        await page.wait_for_timeout(800)

        filled = 0
        username, password = derive_icris_credentials(data)
        logger.info("开始填写账户资料 (url=%s)", page.url)
        await self._log_account_profile_status(page, prefix="填写前 ")

        # 优先：s02 原生表单 (#userType / filing / search / serviceType / #userId ...)
        filled = await self._fill_account_profile_native(page, data)
        if filled >= 5:
            status = await self._get_account_profile_status(page)
            logger.info(
                "账户资料原生填写完成 %d 项 (用户名=%s, 状态=%s)",
                filled,
                username,
                status,
            )
            return filled

        # 回退：Ant Design 组件路径
        logger.info("原生填写不足 (%d)，尝试 Ant Design 回退", filled)
        if await self._select_user_category_individual(page):
            filled += 1
            await page.wait_for_timeout(800)

        checkbox_steps = [
            r"电子提交|電子提交",
            r"电子查册|電子查冊",
        ]
        for cb_pat in checkbox_steps:
            if await self._ensure_checkbox_by_text(page, cb_pat):
                filled += 1
                await page.wait_for_timeout(400)
            elif await self._ensure_checkbox_in_section(
                page, r"拟订用的服务|擬訂用的服務", cb_pat
            ):
                filled += 1
                await page.wait_for_timeout(400)

        if await self._select_primary_account_radio(page):
            filled += 1
            await page.wait_for_timeout(400)

        field_map = [
            (r"用户名称|用戶名稱|Username|userName|loginName", username),
            (r"密码|密碼|Password", password),
            (r"确认密码|確認密碼|confirmPassword|rePassword|Confirm", password),
        ]
        for label_pat, value in field_map:
            if await self._fill_ant_form_by_label(page, label_pat, value):
                filled += 1
            elif await self._fill_input_near_label(page, [label_pat], value):
                filled += 1
            elif await self._fill_field(page, [label_pat], value):
                filled += 1

        for selector, value in (("#userId", username), ("#password", password), ("#confirm", password)):
            if await self._fill_native_input(page, selector, value):
                filled += 1

        status = await self._get_account_profile_status(page)
        individual_ok = status.get("userCategory", False)
        if filled == 0:
            form_labels = await page.evaluate(
                """() => [...document.querySelectorAll(
                    '.ant-form-item-label, label, .ant-select-selection-placeholder'
                )].map(e => (e.innerText || '').trim()).filter(t => t && t.length < 40).slice(0, 30)"""
            )
            logger.warning("账户资料未填写任何字段，页面标签: %s", form_labels)
        logger.info(
            "账户资料已填写 %d 项 (用户名=%s, 个人已选=%s, 状态=%s, url=%s)",
            filled,
            username,
            individual_ok,
            status,
            page.url[:100],
        )
        return filled

    async def _fill_field(self, page: "Page", keywords: list[str], value: str) -> bool:
        if not value:
            return False

        scope = page.locator("form[action*='registration' i], form").first

        for kw in keywords:
            labels = scope.locator(f"label:has-text('{kw}')")
            lbl_count = await labels.count()
            for i in range(lbl_count):
                lbl = labels.nth(i)
                lbl_for = await lbl.get_attribute("for")
                if lbl_for:
                    inp = scope.locator(f"#{lbl_for}")
                    if await inp.count() > 0:
                        await inp.fill(value)
                        return True
                inp = lbl.locator("xpath=following::input[1] | following::textarea[1]")
                if await inp.count() > 0:
                    await inp.first.fill(value)
                    return True

        for kw in keywords:
            for attr in ("name", "id", "placeholder"):
                inp = scope.locator(f"input[{attr}*='{kw}' i], textarea[{attr}*='{kw}' i]")
                if await inp.count() > 0:
                    await inp.first.fill(value)
                    return True

        return False

    async def _fill_registration_form(self, page: "Page", data: dict[str, Any]) -> int:
        if not self._is_registration_page(page.url):
            logger.warning("跳过填表：当前不在 registration 页面")
            return 0

        applicant = data.get("applicant", {})
        username, password = derive_icris_credentials(data)
        field_map = [
            (["title", "salutation", "稱謂"], applicant.get("title", "Mr")),
            (["surname", "last", "姓"], applicant.get("name_en", "").split()[-1] if applicant.get("name_en") else ""),
            (["given", "first", "名"], applicant.get("name_en", "").split()[0] if applicant.get("name_en") else ""),
            (["nameEn", "englishName", "英文"], applicant.get("name_en", "")),
            (["nameCh", "chineseName", "中文"], applicant.get("name_cn", "")),
            (["email", "電郵", "邮箱"], applicant.get("email", "")),
            (["phone", "telephone", "電話", "电话"], applicant.get("phone", "")),
            (["idNo", "idNumber", "identity", "身份"], applicant.get("id_number", "")),
            (["address", "地址"], applicant.get("address", "")),
            (["userName", "username", "loginName", "用户名称", "用戶名稱"], username),
            (["password", "密碼", "密码"], password),
            (["confirmPassword", "rePassword", "確認"], password),
            (["hint", "passwordHint"], data.get("password_hint", "")),
            (["securityAnswer", "answer"], data.get("security_answer", "")),
        ]

        filled = 0
        for keywords, value in field_map:
            if await self._fill_field(page, keywords, str(value)):
                filled += 1

        selects = page.locator("form select")
        sel_count = await selects.count()
        for i in range(sel_count):
            sel = selects.nth(i)
            name = (await sel.get_attribute("name") or "").lower()
            if "idtype" in name or "id_type" in name or "doctype" in name:
                try:
                    await sel.select_option(label=applicant.get("id_type", "HKID"))
                except Exception:
                    await sel.select_option(index=1)

        logger.info("已填写 %d 个注册表单字段", filled)
        return filled

    async def _click_continue(self, page: "Page") -> bool:
        """点击继续/下一步（多步骤导航，dry_run 也执行）"""
        if not self._is_registration_page(page.url):
            return False

        form = page.locator("form[action*='registration' i], form").first
        continue_buttons = [
            "button.ant-btn-primary:has-text('继续')",
            "button.ant-btn-primary:has-text('繼續')",
            "button.ant-btn-primary:has-text('Continue')",
            "button:has-text('继续')",
            "button:has-text('繼續')",
            "button:has-text('Continue')",
            "button:has-text('下一步')",
            "button:has-text('Next')",
            "input[type='submit'][value*='继续' i]",
            "input[type='submit'][value*='Continue' i]",
            "input[type='submit'][value*='Next' i]",
            "input[type='button'][value*='继续' i]",
            "input[type='button'][value*='Continue' i]",
        ]
        for sel in continue_buttons:
            btn = form.locator(sel).first
            if await btn.count() == 0:
                btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                current_url = page.url
                await btn.scroll_into_view_if_needed()
                await btn.click()
                logger.info("已点击继续: %s", sel)
                try:
                    await page.wait_for_function(
                        "(url) => window.location.href !== url",
                        current_url,
                        timeout=15000,
                    )
                except Exception:
                    await self._wait_page_ready(page)
                if self._is_home_or_portal(page.url):
                    logger.error("点击继续后跳转到首页")
                    return False
                return True
        return False

    async def _click_next_if_exists(self, page: "Page") -> bool:
        """兼容旧调用，统一走 _click_continue"""
        return await self._click_continue(page)

    async def run(self, data: dict[str, Any]) -> None:
        """执行注册流程：打开浏览器 → 验证码 → 条款 → 填写表单（不提交）"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError("请先安装 Playwright: pip install playwright") from e

        keep_open = max(10, settings.browser_keep_open_seconds)

        async with async_playwright() as p:
            browser = await launch_browser(p)
            context = await create_browser_context(browser)
            page = await context.new_page()
            run_error: Exception | None = None

            try:
                page = await self._navigate_to_registration(page)
                if not page:
                    logger.error(
                        "无法进入 ICRIS 注册页。"
                        "请确认网络可访问 e-services.cr.gov.hk，且未被防火墙/代理拦截。"
                    )
                    return

                if not await self._ensure_on_registration(page, "注册条款页"):
                    logger.error("未能停留在注册条款页，当前: %s", page.url)
                    return

                await self._ensure_simplified_chinese(page)
                # Step 1: 验证码 + 条款（仅在 s01 页），验证码错误时自动刷新重试
                await self._dismiss_portal_overlays(page)
                terms_ok = False
                for captcha_round in range(1, 4):
                    filled = await self._fill_captcha(page)
                    if not filled:
                        logger.warning("验证码填写失败 (轮次 %d/3)", captcha_round)
                        continue

                    terms_ok = await self._accept_terms(page)
                    if terms_ok:
                        break

                    logger.warning(
                        "条款页未通过，可能验证码错误，刷新后重试 (轮次 %d/3)",
                        captcha_round,
                    )
                    if captcha_round < 3:
                        from src.browser.icris_captcha import _reload_captcha

                        await _reload_captcha(page)
                        if self._is_home_or_portal(page.url):
                            new_page = await self._navigate_to_registration(page)
                            if not new_page:
                                break
                            page = new_page
                            await self._ensure_on_registration(page, "重新进入条款页")

                if not terms_ok:
                    logger.error("条款页处理失败，请检查验证码或手动操作")
                    return

                if not await self._ensure_on_registration(page, "进入注册表单"):
                    return

                await self._ensure_simplified_chinese(page)
                await self._wait_for_account_profile_step(page, timeout_ms=90000)
                await self._wait_registration_vue(page, timeout=60000)

                # Step 2: 账户资料（s02）
                if await self._is_account_profile_step(page):
                    logger.info("=== 填写账户资料步骤 ===")
                    filled = await self._fill_user_profile_step(page, data)
                    if filled == 0:
                        logger.warning("账户资料未填写任何字段，请检查页面语言与 DOM")
                    if await self._click_continue(page):
                        await self._wait_registration_vue(page, timeout=60000)
                        await self._log_page(page, "账户资料继续后")
                    else:
                        logger.warning("账户资料填写后未找到「继续」按钮")
                else:
                    logger.warning("条款通过后未进入账户资料页, url=%s", page.url)

                # Step 3+: 其余多步表单
                max_steps = 6
                for step in range(max_steps):
                    if self._is_home_or_portal(page.url):
                        logger.error("步骤 %d 检测到跳转首页，停止", step + 2)
                        break
                    if await self._is_account_profile_step(page):
                        filled = await self._fill_user_profile_step(page, data)
                    else:
                        logger.info("处理注册表单步骤 %d", step + 2)
                        filled = await self._fill_registration_form(page, data)

                    if filled == 0 and step > 0:
                        break

                    if await self._click_continue(page):
                        await self._wait_registration_vue(page, timeout=60000)
                        if not await self._ensure_on_registration(page, f"步骤{step + 2}后"):
                            break
                        continue
                    break

                submit_btns = page.locator(
                    "form input[type='submit'], form button[type='submit']"
                )
                if await submit_btns.count() > 0:
                    logger.info("[DRY RUN] 检测到提交按钮，不会点击提交")

                logger.info("注册表单填写完成（未提交）")

            except Exception as e:
                run_error = e
                logger.exception("注册流程异常: %s", e)
            finally:
                logger.info("浏览器保持打开 %d 秒供检查…", keep_open)
                try:
                    await page.wait_for_timeout(keep_open * 1000)
                except Exception:
                    pass
                await browser.close()

            if run_error:
                raise run_error
