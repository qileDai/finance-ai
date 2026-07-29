"""ICRIS 登录及材料填写浏览器自动化"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import settings
from src.browser.icris_captcha import fill_captcha as fill_icris_captcha
from src.browser.launcher import create_browser_context, launch_browser
from src.email.imap_client import IcrisAccount
from src.llm.openai_client import LLMClient

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.e-services.cr.gov.hk/ICRIS3EP/system/home.do?webEnv=PROD"


class IcrisLoginBot:
    """登录 ICRIS 并填写公司注册材料（不最终提交）"""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()
        self.dry_run = settings.dry_run

    async def _fill_captcha(self, page) -> None:
        await fill_icris_captcha(page, self.llm)

    async def _login(self, page, account: IcrisAccount) -> None:
        logger.info("登录 ICRIS: %s", account.username)

        user_selectors = [
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[name*='login' i]",
            "#username",
            "#userId",
        ]
        for sel in user_selectors:
            inp = page.locator(sel).first
            if await inp.count() > 0 and await inp.is_visible():
                await inp.fill(account.username)
                break

        pass_selectors = [
            "input[type='password']",
            "input[name*='pass' i]",
            "input[id*='pass' i]",
        ]
        for sel in pass_selectors:
            inp = page.locator(sel).first
            if await inp.count() > 0 and await inp.is_visible():
                await inp.fill(account.password)
                break

        await self._fill_captcha(page)

        login_btns = [
            "button:has-text('Login')",
            "button:has-text('登入')",
            "button:has-text('登录')",
            "input[value*='Login']",
            "input[value*='登入']",
        ]
        for sel in login_btns:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                logger.info("已点击登录")
                return

    async def _navigate_to_incorporation(self, page) -> None:
        """导航到成立公司功能"""
        links = [
            "a:has-text('Incorporation')",
            "a:has-text('成立公司')",
            "a:has-text('Local Company')",
            "a:has-text('本地公司')",
            "text=成立公司",
        ]
        for sel in links:
            link = page.locator(sel).first
            if await link.count() > 0 and await link.is_visible():
                await link.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                logger.info("已进入成立公司模块")
                return
        logger.warning("未找到成立公司入口，请手动导航")

    async def _fill_company_form(self, page, data: dict[str, Any]) -> None:
        """填写 NNC1 相关字段"""
        field_map = [
            (["companyNameEn", "englishName", "英文公司名"], data.get("company_name_en", "")),
            (["companyNameCh", "chineseName", "中文公司名"], data.get("company_name_cn", "")),
            (["email", "電郵"], data.get("contact", {}).get("email", "")),
            (["phone", "telephone", "電話"], data.get("contact", {}).get("phone", "")),
            (["flat", "floor", "room"], data.get("registered_office", {}).get("flat_floor", "")),
            (["building"], data.get("registered_office", {}).get("building", "")),
            (["street"], data.get("registered_office", {}).get("street", "")),
            (["district"], data.get("registered_office", {}).get("district", "")),
            (["share", "capital", "股本"], str(data.get("share_capital", {}).get("total_shares", ""))),
            (["business", "nature", "业务"], data.get("business_nature_desc", "")),
        ]

        filled = 0
        for keywords, value in field_map:
            if not value:
                continue
            for kw in keywords:
                inp = page.locator(
                    f"input[name*='{kw}' i], input[id*='{kw}' i], textarea[name*='{kw}' i]"
                ).first
                if await inp.count() > 0 and await inp.is_visible():
                    await inp.fill(str(value))
                    filled += 1
                    break

        # 填写董事信息
        directors = data.get("directors", [])
        for idx, director in enumerate(directors[:3]):
            prefix = f"director{idx}" if idx > 0 else "director"
            for kw, val in [
                (["nameEn", "directorName"], director.get("name_en", "")),
                (["nameCh"], director.get("name_cn", "")),
                (["idNo"], director.get("id_number", "")),
                (["email"], director.get("email", "")),
            ]:
                if not val:
                    continue
                inp = page.locator(
                    f"input[name*='{prefix}' i][name*='{kw}' i], "
                    f"input[id*='director'][id*='{idx}']"
                ).first
                if await inp.count() > 0:
                    await inp.fill(val)

        logger.info("已填写 %d 个公司注册字段", filled)

    async def run(self, account: IcrisAccount, data: dict[str, Any]) -> None:
        try:
            from src.browser.launcher import import_async_playwright

            async_playwright = import_async_playwright()
        except RuntimeError:
            raise
        except ImportError as e:
            raise RuntimeError(
                "请先安装 Playwright: pip install playwright && playwright install chromium"
            ) from e

        async with async_playwright() as p:
            browser = await launch_browser(p)
            context = await create_browser_context(browser)
            page = await context.new_page()

            logger.info("打开 ICRIS 登录页面")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(2000)

            await self._login(page, account)
            await page.wait_for_timeout(3000)

            await self._navigate_to_incorporation(page)
            await page.wait_for_timeout(2000)

            # 多步骤填写
            for step in range(6):
                logger.info("填写材料步骤 %d", step + 1)
                await self._fill_company_form(page, data)

                next_btn = page.locator(
                    "button:has-text('Next'), button:has-text('下一步'), "
                    "button:has-text('Continue'), input[value*='Next']"
                ).first
                if await next_btn.count() > 0 and await next_btn.is_visible():
                    if self.dry_run:
                        logger.info("[DRY RUN] 发现下一步，不继续点击")
                        break
                    await next_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                else:
                    break

            logger.info("材料填写完成（未提交），浏览器保持打开 60 秒")
            await page.wait_for_timeout(60000)
            await browser.close()
