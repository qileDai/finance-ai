"""ICRIS 账号注册浏览器自动化（不提交）"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

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


class IcrisRegistrationBot:
    """ICRIS 电子服务账号注册 - 自动填写但不提交"""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()
        self.dry_run = settings.dry_run

    async def _log_page(self, page: "Page", label: str) -> None:
        logger.info("[%s] URL: %s", label, page.url)

    async def _wait_page_ready(self, page: "Page", timeout: int = 20000) -> None:
        """用 domcontentloaded 替代 networkidle，避免政府站点长时间挂起"""
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(800)

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

        # 等待 systemclock 出现
        for _ in range(20):
            if self._extract_systemclock(page.url):
                break
            await page.wait_for_timeout(500)

        systemclock = self._extract_systemclock(page.url)

        # 等待「立即登记」按钮渲染
        register_btn = page.get_by_role("button", name=re.compile(r"立即登[记記]"))
        for _ in range(30):
            if await register_btn.count() > 0:
                break
            await page.wait_for_timeout(500)

        if await register_btn.count() > 0:
            logger.info("点击「立即登记」")
            try:
                async with page.context.expect_page(timeout=8000) as new_page_info:
                    await register_btn.first.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded", timeout=30000)
                page = new_page
                logger.info("立即登记在新标签页打开")
            except Exception:
                await register_btn.first.click()
            try:
                await page.wait_for_url("**/registration/**", timeout=30000)
                await self._wait_page_ready(page)
                await self._log_page(page, "立即登记后")
                if self._is_registration_page(page.url):
                    return page
            except Exception as e:
                logger.warning("点击立即登记未进入 registration: %s", e)

        if systemclock:
            reg_url = build_registration_url(systemclock)
            logger.info("使用门户 systemclock 打开注册页: %s", reg_url)
            await page.goto(reg_url, wait_until="domcontentloaded", timeout=30000)
            await self._wait_page_ready(page)
            await self._log_page(page, "注册页加载")
            if self._is_registration_page(page.url) and not self._is_cr_public_site(page.url):
                return page

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
            (["password", "密碼", "密码"], "MockPass@2026!"),
            (["confirmPassword", "rePassword", "確認"], "MockPass@2026!"),
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

    async def _click_next_if_exists(self, page: "Page") -> bool:
        if not self._is_registration_page(page.url):
            return False

        form = page.locator("form[action*='registration' i], form").first
        next_buttons = [
            "input[type='submit'][value*='Next' i]",
            "input[type='submit'][value*='下一步']",
            "input[type='button'][value*='Next' i]",
            "button:has-text('Next')",
            "button:has-text('下一步')",
            "button:has-text('Continue')",
        ]
        for sel in next_buttons:
            btn = form.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                if self.dry_run:
                    logger.info("[DRY RUN] 发现下一步按钮但不点击: %s", sel)
                    return True

                current_url = page.url
                await btn.click()
                try:
                    await page.wait_for_function(
                        "(url) => window.location.href !== url",
                        current_url,
                        timeout=15000,
                    )
                except Exception:
                    await self._wait_page_ready(page)

                if self._is_home_or_portal(page.url):
                    logger.error("点击下一步后跳转到首页")
                    return False
                return True
        return False

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

                # Step 2+: 多步骤表单填写（仅在 registration 流程页）
                max_steps = 6
                for step in range(max_steps):
                    if self._is_home_or_portal(page.url):
                        logger.error("步骤 %d 检测到跳转首页，停止", step + 1)
                        break

                    logger.info("填写注册表单步骤 %d", step + 1)
                    filled = await self._fill_registration_form(page, data)
                    if filled == 0 and step > 0:
                        break

                    has_next = await self._click_next_if_exists(page)
                    if not has_next:
                        break

                    if not await self._ensure_on_registration(page, f"步骤{step + 1}后"):
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
