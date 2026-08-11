"""ICRIS 账号注册浏览器自动化（不提交）"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse

from config.settings import settings
from src.browser.icris_captcha import fill_captcha as fill_icris_captcha
from src.browser.launcher import close_browser_session, create_browser_context, launch_browser
from src.llm.openai_client import LLMClient

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

_PAGE_PAUSE_MS = 80
_POLL_MS = 100
_FORM_PAUSE_MS = 120
_SPIN_TIMEOUT_MS = 45000

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
    """从 company_data 生成 ICRIS 用户名与符合规则的密码（同一次流程用户名保持一致）。

    若 aggregator 已预生成 icris_account.username + password（yingtai 模式），
    直接使用，不追加随机后缀、不重新派生密码。
    """
    session = data.setdefault("_icris_session", {})
    if session.get("username") and session.get("password"):
        return session["username"], session["password"]

    acct = data.get("icris_account", {})
    applicant = data.get("applicant", {})

    username = (acct.get("username") or "").strip()
    password_raw = (acct.get("password") or "").strip()

    # 预生成凭证（yingtai 模式）：username + password 均非空 → 原样使用
    if username and password_raw:
        session["username"] = username
        session["password"] = password_raw
        return username, password_raw

    # 旧逻辑：邮箱用户名 + 随机后缀 + 姓名派生密码
    if not username:
        email = applicant.get("email", "")
        if "@" in email:
            username = email.split("@", 1)[0]
        else:
            parts = re.sub(r"[^A-Za-z0-9]", "", applicant.get("name_en", "user"))
            username = parts.lower() or "icrisuser"

    username = append_random_username_suffix(username, length=2)
    password = ensure_icris_password(password_raw or data.get("password_hint", ""))
    session["username"] = username
    session["password"] = password
    return username, password


def split_applicant_english_name(name_en: str) -> tuple[str, str]:
    """英文姓名 → (名, 姓)，如 CHAN Tai Man → (Tai Man, CHAN)"""
    parts = [p for p in (name_en or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return " ".join(parts[1:]), parts[0]


def derive_mock_china_address(applicant: dict[str, Any]) -> dict[str, str]:
    """非香港地址（中国大陆）mock 数据"""
    cn = applicant.get("address_cn")
    if isinstance(cn, dict):
        return {
            "room": str(cn.get("room", "8楼A室")),
            "building": str(cn.get("building", "幸福大厦")),
            "street": str(cn.get("street", "科技园南路1号")),
            "region": str(cn.get("region", "广东省深圳市南山区 518000")),
        }
    return {
        "room": "8楼A室",
        "building": "快乐大厦",
        "street": "中关村大街1号",
        "region": "广东省广州市天河区 510000",
    }


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
    """ICRIS 电子服务账号注册 - 自动填写；仅显式开关下才点最终提交"""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()
        self.dry_run = settings.dry_run
        # 双重开关：DRY_RUN=false 且 ICRIS_ALLOW_SUBMIT=true 才允许最终提交
        self.allow_submit = (not settings.dry_run) and bool(settings.icris_allow_submit)
        self._locale: str | None = None

    async def _log_page(self, page: "Page", label: str) -> None:
        logger.info("[%s] URL: %s", label, page.url)

    async def _is_spinning(self, page: "Page") -> bool:
        try:
            return bool(
                await page.evaluate(
                    "() => !!document.querySelector('.ant-spin-spinning')"
                )
            )
        except Exception:
            return False

    async def _get_validation_errors(self, page: "Page") -> list[str]:
        try:
            errs = await page.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll(
                        '.ant-form-item-explain-error, .ant-message-error'
                    )) {
                        const t = (el.innerText || '').trim();
                        if (t) out.push(t);
                    }
                    return [...new Set(out)].slice(0, 8);
                }"""
            )
            return errs or []
        except Exception:
            return []

    async def _wait_spin_clear(self, page: "Page", timeout_ms: int | None = None) -> bool:
        """等待全页 loading 结束；超时则 Esc 尝试恢复（防卡死）"""
        if page.is_closed():
            return False
        limit = timeout_ms or _SPIN_TIMEOUT_MS
        if not await self._is_spinning(page):
            return True
        logger.info("等待页面 loading…")
        try:
            await page.wait_for_function(
                "() => !document.querySelector('.ant-spin-spinning')",
                timeout=limit,
            )
            return True
        except Exception:
            pass
        if not await self._is_spinning(page):
            return True
        logger.warning("loading 超时 %dms，尝试 Esc 恢复", limit)
        errors = await self._get_validation_errors(page)
        if errors:
            logger.warning("校验错误: %s", errors[:4])
        for _ in range(2):
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass
            if not await self._is_spinning(page):
                return True
        return not await self._is_spinning(page)

    async def _wait_registration_vue(self, page: "Page", timeout: int = 45000) -> bool:
        """等待注册条款页 Vue 挂载（checkbox / 验证码 / 步骤文案）"""
        await self._wait_spin_clear(page, timeout_ms=min(30000, timeout))
        try:
            await page.wait_for_function(
                """() => {
                    const href = window.location.href;
                    const body = document.body?.innerText || '';
                    if (/\/registration\\/s0[1-9]/i.test(href)) {
                        if (/验证码|驗證碼|条款|條款|接受|Accept|注册|註冊/i.test(body)) return true;
                    }
                    if (href.includes('#')) return true;
                    if (document.querySelector(
                        'input[type=checkbox], #checkCode, img[src^="data:image"]'
                    )) return true;
                    if (document.querySelector('.ant-form, #uam-content, .formSection')) {
                        if (/registration|注册|註冊/i.test(body)) return true;
                    }
                    return Array.from(document.querySelectorAll('form')).some(
                        (f) => /registration/i.test(f.getAttribute('action') || '')
                    );
                }""",
                timeout=timeout,
            )
            await page.wait_for_timeout(_PAGE_PAUSE_MS)
            return True
        except Exception:
            return False

    async def _registration_page_probe(self, page: "Page") -> dict[str, Any]:
        """诊断注册页 DOM 状态（Vue 检测失败时）"""
        try:
            return await page.evaluate(
                """() => {
                    const body = document.body?.innerText || '';
                    return {
                        href: window.location.href,
                        spinning: !!document.querySelector('.ant-spin-spinning'),
                        checkCode: !!document.querySelector('#checkCode'),
                        checkbox: !!document.querySelector('input[type=checkbox]'),
                        captchaImg: !!document.querySelector('img[src^="data:image"]'),
                        formCount: document.querySelectorAll('form').length,
                        bodySample: body.slice(0, 200),
                    };
                }"""
            )
        except Exception as e:
            return {"error": str(e)}

    async def _dump_page_diagnostics(self, page: "Page", label: str = "") -> None:
        """输出完整页面诊断信息（反自动化排查用）"""
        try:
            diag = await page.evaluate(
                """() => {
                    const body = document.body;
                    return {
                        url: window.location.href,
                        title: document.title,
                        bodyLen: body ? body.innerHTML.length : 0,
                        bodySample: body ? body.innerText.slice(0, 300) : '',
                        hasVue: !!window.__vue_app__ || !!document.querySelector('[data-v-]'),
                        cookies: document.cookie.slice(0, 200),
                        scripts: Array.from(document.querySelectorAll('script[src]'))
                            .map(s => s.src.slice(0, 80)).slice(0, 10),
                        forms: document.querySelectorAll('form').length,
                        inputs: document.querySelectorAll('input').length,
                    };
                }"""
            )
            logger.warning(
                "[%s诊断] url=%s title=%s bodyLen=%d vue=%s forms=%d inputs=%d "
                "body=%s cookies=%s scripts=%s",
                label,
                diag.get("url", "")[:120],
                diag.get("title", "")[:40],
                diag.get("bodyLen", 0),
                diag.get("hasVue", False),
                diag.get("forms", 0),
                diag.get("inputs", 0),
                diag.get("bodySample", "")[:150],
                diag.get("cookies", "")[:100],
                diag.get("scripts", [])[:5],
            )
        except Exception as e:
            logger.debug("诊断信息获取失败: %s", e)

    async def _registration_terms_ready(self, page: "Page") -> bool:
        """条款页是否可交互（比 _registration_terms_visible 更宽松）"""
        if await self._registration_terms_visible(page):
            return True
        if not self._is_registration_page(page.url):
            return False
        probe = await self._registration_page_probe(page)
        if probe.get("checkCode") or probe.get("checkbox") or probe.get("captchaImg"):
            return True
        sample = probe.get("bodySample") or ""
        return bool(re.search(r"验证码|驗證碼|条款|條款|接受|Accept", sample))

    async def _wait_page_ready(self, page: "Page", timeout: int = 25000) -> None:
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
        await self._wait_spin_clear(page, timeout_ms=min(20000, timeout))
        await page.wait_for_timeout(_PAGE_PAUSE_MS)

    async def _wait_portal_session(self, page: "Page", timeout_ms: int = 20000) -> bool:
        """等待门户 home.do 就绪（替代固定轮询）

        检测被重定向到 cr.gov.hk 公开站的情况，并输出诊断信息。
        """
        # 先检查是否被反自动化重定向到公开站
        if self._is_cr_public_site(page.url):
            logger.error(
                "门户被重定向到公开站（反自动化检测）: %s — "
                "ICRIS 检测到 Playwright TLS/HTTP 指纹，"
                "请尝试: 1) 使用 CDP 连接真实 Chrome; 2) 增强 stealth 伪装",
                page.url,
            )
            return False

        try:
            await page.wait_for_url("**/e-services.cr.gov.hk/**/home.do**", timeout=timeout_ms)
        except Exception:
            pass

        # 再次检查重定向
        if self._is_cr_public_site(page.url):
            logger.error("门户在 wait_for_url 后被重定向到公开站: %s", page.url)
            return False

        try:
            await page.wait_for_function(
                """() => {
                    const href = window.location.href;
                    if (!href.includes('e-services.cr.gov.hk')) return false;
                    if (href.includes('www.cr.gov.hk') && !href.includes('e-services')) return false;
                    return /home\\.do|systemclock=/i.test(href)
                        || document.querySelector('header, .header, #header');
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            if self._is_cr_public_site(page.url):
                logger.error("门户 wait_for_function 后被重定向到公开站: %s", page.url)
                return False
            return "e-services.cr.gov.hk" in page.url and not self._is_cr_public_site(page.url)

    async def _wait_systemclock_in_url(self, page: "Page", timeout_ms: int = 15000) -> str | None:
        """等待 URL 出现 systemclock 参数"""
        try:
            await page.wait_for_function(
                "() => /[?&]systemclock=\\d+/.test(window.location.href)",
                timeout=timeout_ms,
            )
        except Exception:
            pass
        return self._extract_systemclock(page.url)

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
        """用门户 session 的 systemclock 打开注册条款页（导航阶段不切换语言）"""
        reg_url = build_registration_url(systemclock)
        for attempt in range(1, 3):
            logger.info("打开注册页 (尝试 %d/2): %s", attempt, reg_url[:100])
            await page.goto(reg_url, wait_until="commit", timeout=45000)

            # 检查是否被重定向到公开站
            if self._is_cr_public_site(page.url):
                logger.error("注册页被重定向到公开站（反自动化检测）: %s", page.url)
                await self._dump_page_diagnostics(page, "注册页重定向")
                return None

            vue_ok = await self._wait_registration_vue(page, timeout=45000)
            if not vue_ok:
                probe = await self._registration_page_probe(page)
                logger.warning(
                    "注册页 Vue 未挂载 (尝试 %d/2) probe=%s",
                    attempt,
                    {k: probe.get(k) for k in ("spinning", "checkCode", "checkbox", "bodySample")},
                )
                # 输出完整诊断信息
                await self._dump_page_diagnostics(page, f"Vue未挂载-{attempt}")
                if self._is_registration_page(page.url) and await self._registration_terms_ready(page):
                    logger.info("URL 已在 registration，按条款页就绪继续")
                    await self._log_page(page, "注册页加载")
                    return page
                if attempt < 2:
                    await page.reload(wait_until="commit", timeout=45000)
                continue
            await self._wait_spin_clear(page, timeout_ms=20000)
            await self._log_page(page, "注册页加载")
            if await self._registration_terms_ready(page):
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
            await page.wait_for_timeout(_POLL_MS)
        return None

    async def _click_portal_register(self, page: "Page") -> "Page | None":
        """点击门户「立即登记」，成功则返回注册页 Page"""
        register_btn = await self._wait_portal_register_control(page, timeout=12000)
        if register_btn is None:
            return None

        logger.info("点击「立即登记」")
        try:
            async with page.context.expect_page(timeout=6000) as new_page_info:
                await register_btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("commit", timeout=20000)
            page = new_page
            logger.info("立即登记在新标签页打开")
        except Exception:
            await register_btn.click()

        try:
            await page.wait_for_url("**/registration/**", timeout=20000)
            await self._wait_spin_clear(page, timeout_ms=20000)
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
        if self._locale == "simplified":
            return True
        state = await self._page_language_state(page)
        if state == "simplified" or await self._is_simplified_chinese_active(page):
            logger.info("页面已是简体中文，跳过语言切换")
            self._locale = "simplified"
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
                    self._locale = "simplified"
                    return True
                await page.wait_for_timeout(_FORM_PAUSE_MS)
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

    async def _is_traditional_chinese_active(self, page: "Page") -> bool:
        state = await self._page_language_state(page)
        if state == "traditional":
            return True
        if state == "simplified":
            return False
        jian = page.locator("a").filter(has_text=re.compile(r"^简$"))
        if await jian.count() > 0 and await jian.first.is_visible():
            return True
        return False

    async def _wait_language_traditional(self, page: "Page", timeout_ms: int = 15000) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    if (/用户类别|拟订用的服务|公司注册处|首页/.test(text)) return false;
                    return /用戶類別|擬訂用的服務|公司註冊處|首頁/.test(text);
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return await self._is_traditional_chinese_active(page)

    async def _find_fan_link_info(self, page: "Page") -> dict | None:
        """定位页头「繁」语言链接元数据"""
        return await page.evaluate(
            """() => {
                const items = [];
                for (const el of document.querySelectorAll('a, button, span, li')) {
                    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    if (t !== '繁') continue;
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

    async def _activate_fan_link(self, page: "Page", info: dict) -> str:
        """尝试多种方式触发「繁」切换，返回使用的方法名"""
        href = (info.get("href") or "").strip()
        if href and not href.lower().startswith("javascript"):
            await page.goto(href, wait_until="commit", timeout=60000)
            return f"goto:{href[:80]}"

        result = await page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('a, button, span, li')) {
                    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    if (t !== '繁') continue;
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

        loc = page.locator("a").filter(has_text=re.compile(r"^繁$")).first
        if await loc.count() == 0:
            loc = page.get_by_text("繁", exact=True).first
        await loc.scroll_into_view_if_needed()
        await loc.click(force=True, timeout=5000)
        return "playwright-force-click"

    def _url_with_traditional_locale(self, url: str) -> str:
        parsed = urlparse(url)
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key in ("locale", "lang", "request_locale", "language"):
            if key in qs:
                qs[key] = "zh_TW"
                break
        else:
            qs["locale"] = "zh_TW"
        new_query = urlencode(qs)
        return parsed._replace(query=new_query).geturl()

    async def _fallback_traditional_locale_url(self, page: "Page") -> bool:
        """回退：通过 URL 参数 / Cookie 强制繁体"""
        try:
            await page.context.add_cookies(
                [
                    {
                        "name": "locale",
                        "value": "zh_TW",
                        "domain": "www.e-services.cr.gov.hk",
                        "path": "/",
                    },
                    {
                        "name": "lang",
                        "value": "zh_TW",
                        "domain": ".e-services.cr.gov.hk",
                        "path": "/",
                    },
                ]
            )
        except Exception as e:
            logger.debug("设置繁体 locale Cookie 失败: %s", e)

        locale_url = self._url_with_traditional_locale(page.url)
        if locale_url != page.url:
            logger.info("回退：通过 URL 参数切换繁体 %s", locale_url[:120])
            await page.goto(locale_url, wait_until="commit", timeout=60000)
            await page.wait_for_timeout(1500)
            if await self._wait_language_traditional(page, timeout_ms=10000):
                return True
        return await self._is_traditional_chinese_active(page)

    async def _ensure_traditional_chinese(self, page: "Page") -> bool:
        """点击页头右上角「繁」切换为繁体中文；已是繁体则跳过"""
        if await self._is_traditional_chinese_active(page):
            logger.info("页面已是繁体中文，跳过语言切换")
            return True

        info = await self._find_fan_link_info(page)
        if not info:
            logger.warning("未找到页头「繁」链接 (state=%s)", await self._page_language_state(page))
            return await self._fallback_traditional_locale_url(page)

        logger.info(
            "找到「繁」入口: tag=%s href=%s class=%s",
            info.get("tag"),
            (info.get("href") or "")[:100],
            info.get("cls"),
        )

        for attempt in range(1, 4):
            try:
                method = await self._activate_fan_link(page, info)
                logger.info("已触发「繁」切换 (尝试 %d/3, 方式=%s)", attempt, method)
                if await self._wait_language_traditional(page, timeout_ms=12000):
                    logger.info("语言已切换为繁体中文")
                    return True
                await page.wait_for_timeout(800)
            except Exception as e:
                logger.warning("「繁」切换尝试 %d 失败: %s", attempt, e)
                await page.wait_for_timeout(500)

        logger.warning("点击「繁」后页面仍为简体 (state=%s)", await self._page_language_state(page))
        return await self._fallback_traditional_locale_url(page)

    async def _navigate_to_registration(self, page: "Page") -> "Page | None":
        """
        进入注册页流程，成功返回当前 Page（可能是新标签页），失败返回 None。

        增强了对反自动化重定向的检测和诊断。
        """
        logger.info("进入电子服务门户: %s", PORTAL_URL)
        for attempt in range(1, 3):
            try:
                await page.goto(PORTAL_URL, wait_until="commit", timeout=45000)
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
                await page.wait_for_timeout(1500)

        # 检测是否被反自动化重定向
        if self._is_cr_public_site(page.url):
            logger.error(
                "门户被重定向到公开站（HTTP 层反自动化检测）: %s — "
                "ICRIS 服务端检测到 Playwright TLS/HTTP 指纹。"
                "解决方案: 1) 确保本机安装了 Chrome; "
                "2) 程序会自动尝试 CDP 连接真实 Chrome; "
                "3) 或手动设置 CHROME_USE_EXISTING=true 连接已开的 Chrome",
                page.url,
            )
            await self._dump_page_diagnostics(page, "门户重定向")
            return None

        if not await self._wait_portal_session(page, timeout_ms=20000):
            # 再次检查是否被重定向
            if self._is_cr_public_site(page.url):
                logger.error(
                    "门户加载后重定向到公开站: %s — ICRIS 反自动化检测",
                    page.url,
                )
                await self._dump_page_diagnostics(page, "门户加载后重定向")
                return None
            await self._log_page(page, "门户加载失败")
            await self._dump_page_diagnostics(page, "门户加载失败")
            return None

        await self._log_page(page, "门户加载")
        if self._is_cr_public_site(page.url):
            logger.error("被 disable-devtool 重定向到公开网站: %s", page.url)
            return None

        await self._dismiss_portal_overlays(page)

        systemclock = await self._wait_systemclock_in_url(page, timeout_ms=12000)
        if not systemclock:
            await page.wait_for_timeout(800)
            systemclock = self._extract_systemclock(page.url)

        if systemclock:
            opened = await self._open_registration_with_clock(page, systemclock)
            if opened:
                return opened
            logger.warning("systemclock 直链未加载条款页，刷新门户会话后重试")
            await page.goto(PORTAL_URL, wait_until="commit", timeout=45000)
            await self._wait_portal_session(page, timeout_ms=15000)
            systemclock = await self._wait_systemclock_in_url(page, timeout_ms=12000)
            if systemclock:
                opened = await self._open_registration_with_clock(page, systemclock)
                if opened:
                    return opened

        clicked = await self._click_portal_register(page)
        if clicked and await self._registration_terms_ready(clicked):
            return clicked

        if self._is_registration_page(page.url):
            await self._wait_spin_clear(page, timeout_ms=30000)
            if await self._registration_terms_ready(page):
                logger.info("已在 registration URL，继续流程: %s", page.url[:120])
                return page
            probe = await self._registration_page_probe(page)
            logger.warning("registration URL 但条款未就绪: %s", probe)

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
                    pass

                if not await self._wait_spin_clear(page, timeout_ms=30000):
                    logger.warning("接受条款后 loading 未结束")
                    return False

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
        return re.sub(r"[\s/／\*:：]+", "", (text or "").strip())

    async def _ant_select_surround_text(self, page: "Page", index: int) -> str:
        """获取第 index 个 ant-select 附近的标签/占位符文字"""
        try:
            return await page.evaluate(
                """(idx) => {
                    const sel = document.querySelectorAll('.ant-select')[idx];
                    if (!sel) return '';
                    const parts = [];
                    const ph = sel.querySelector('.ant-select-selection-placeholder');
                    if (ph) parts.push(ph.innerText.trim());
                    const picked = sel.querySelector('.ant-select-selection-item');
                    if (picked) parts.push(picked.innerText.trim());
                    let p = sel.parentElement;
                    for (let i = 0; i < 10 && p; i++) {
                        const lbl = p.querySelector(
                            ':scope > label, :scope > .ant-form-item-label, '
                            + ':scope > .rowTitle, :scope > .col-form-label'
                        );
                        if (lbl && lbl.innerText.trim()) parts.push(lbl.innerText.trim());
                        const prev = p.previousElementSibling;
                        if (prev) {
                            const t = prev.innerText.trim();
                            if (t && t.length < 120) parts.push(t);
                        }
                        p = p.parentElement;
                    }
                    return parts.join(' | ');
                }""",
                index,
            )
        except Exception:
            return ""

    async def _wait_for_ant_selects(
        self, page: "Page", min_count: int = 1, timeout_ms: int = 15000
    ) -> None:
        try:
            await page.wait_for_function(
                f"() => document.querySelectorAll('.ant-select').length >= {min_count}",
                timeout=timeout_ms,
            )
        except Exception:
            pass
        await page.wait_for_timeout(_FORM_PAUSE_MS)

    async def _log_ant_selects(self, page: "Page", prefix: str = "") -> None:
        count = await page.locator(".ant-select").count()
        rows = []
        for i in range(min(count, 12)):
            ctx = (await self._ant_select_surround_text(page, i))[:100]
            rows.append({"i": i, "context": ctx})
        logger.info("%s ant-select 列表 (%d): %s", prefix, count, rows)

    async def _select_ant_select_by_keywords(
        self, page: "Page", keywords: list[str], option: str
    ) -> bool:
        """
        按标签/rowTitle/占位符定位下拉并选择（兼容 strut-address 表格布局）。
        """
        if not option:
            return False
        opt_re = re.compile(re.escape(option), re.I)
        kw_norm = [self._normalize_form_label(k) for k in keywords if k]

        await page.evaluate(
            """() => {
                const w = document.querySelector('#formWrapper, .formWrapper, main');
                if (w) w.scrollTop = w.scrollHeight;
                window.scrollTo(0, document.body.scrollHeight);
            }"""
        )
        await page.wait_for_timeout(_FORM_PAUSE_MS)

        opened = await page.evaluate(
            """([keywords, option]) => {
                const norm = s => (s || '').replace(/[\\s/／:*：]/g, '');
                const kws = keywords.map(k => norm(k));
                const esc = option.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                const optRe = new RegExp(esc, 'i');

                const titleEls = [...document.querySelectorAll(
                    '.rowTitle, th, label, .ant-form-item-label, .control-label'
                )];

                const pickNative = (sel) => {
                    const opt = [...sel.options].find(o => optRe.test((o.textContent || '').trim()));
                    if (!opt) return false;
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                };

                const openAnt = (selectEl) => {
                    const trigger = selectEl.querySelector('.ant-select-selector, .ant-select-arrow')
                        || selectEl;
                    trigger.scrollIntoView({ block: 'center' });
                    trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                    trigger.click();
                };

                for (const title of titleEls) {
                    const tt = norm((title.innerText || '').trim());
                    if (!kws.some(k => k && tt.includes(k))) continue;

                    const containers = [
                        title.closest('tr'),
                        title.closest('.ant-row, .row, .form-group, fieldset, .content, .ant-form-item'),
                        title.parentElement,
                        title.parentElement?.parentElement,
                    ].filter(Boolean);

                    for (const container of containers) {
                        let select = container.querySelector('.ant-select, [role=combobox], select');
                        if (!select) {
                            const td = title.closest('td');
                            const next = td?.nextElementSibling;
                            if (next) select = next.querySelector('.ant-select, [role=combobox], select');
                        }
                        if (!select) continue;
                        if (select.tagName === 'SELECT') {
                            if (pickNative(select)) return { mode: 'native', label: title.innerText.trim() };
                        } else {
                            openAnt(select);
                            return { mode: 'ant', label: title.innerText.trim() };
                        }
                    }
                }

                // 回退：扫描全部 ant-select / combobox 的上下文
                const all = [...document.querySelectorAll('.ant-select, [role=combobox]')];
                for (let i = 0; i < all.length; i++) {
                    const sel = all[i];
                    const parts = [];
                    const ph = sel.querySelector('.ant-select-selection-placeholder');
                    if (ph) parts.push(ph.innerText.trim());
                    let p = sel.parentElement;
                    for (let d = 0; d < 8 && p; d++) {
                        const lbl = p.querySelector(':scope > label, :scope > .rowTitle, :scope > .ant-form-item-label');
                        if (lbl) parts.push(lbl.innerText.trim());
                        p = p.parentElement;
                    }
                    const ctx = norm(parts.join(' | '));
                    if (!kws.some(k => k && ctx.includes(k))) continue;
                    if (sel.tagName === 'SELECT') {
                        if (pickNative(sel)) return { mode: 'native', label: parts.join(' | ') };
                    } else {
                        openAnt(sel);
                        return { mode: 'ant', label: parts.join(' | '), index: i };
                    }
                }
                return { mode: 'none' };
            }""",
            [keywords, option],
        )

        if opened.get("mode") == "native":
            logger.info("已选原生下拉 [%s] → %s", opened.get("label", ""), option)
            return True
        if opened.get("mode") != "ant":
            await self._log_ant_selects(page, "未匹配")
            logger.warning("未找到匹配下拉: %s → %s", keywords, option)
            return False

        logger.info("已打开下拉 [%s] → 选择 %s", opened.get("label", ""), option)

        for attempt in range(3):
            try:
                dd = page.locator(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden)"
                ).last
                try:
                    await dd.wait_for(state="visible", timeout=4000)
                except Exception:
                    pass

                search = page.locator(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden) input"
                ).last
                if await search.count() > 0 and await search.is_visible():
                    await search.fill("")
                    await search.type(option, delay=35)
                    await page.wait_for_timeout(500)

                opt = page.locator(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
                    ".ant-select-item-option"
                ).filter(has_text=opt_re).first
                if await opt.count() > 0:
                    await opt.click(force=True, timeout=3000)
                else:
                    role_opt = page.get_by_role("option", name=opt_re)
                    if await role_opt.count() > 0:
                        await role_opt.first.click(force=True, timeout=3000)
                    else:
                        await page.keyboard.press("Enter")

                await page.wait_for_timeout(400)
                verified = await page.evaluate(
                    """([keywords, option]) => {
                        const norm = s => (s || '').replace(/[\\s/／:*：]/g, '');
                        const kws = keywords.map(k => norm(k));
                        const esc = option.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                        const optRe = new RegExp(esc, 'i');
                        const titleEls = [...document.querySelectorAll('.rowTitle, th, label, .ant-form-item-label')];
                        for (const title of titleEls) {
                            const tt = norm((title.innerText || '').trim());
                            if (!kws.some(k => k && tt.includes(k))) continue;
                            const row = title.closest('tr, .ant-form-item, .content, .row') || title.parentElement;
                            if (!row) continue;
                            const txt = row.innerText || '';
                            if (optRe.test(txt)) return true;
                        }
                        return optRe.test(document.body.innerText || '');
                    }""",
                    [keywords, option],
                )
                if verified:
                    logger.info("已选下拉 [%s] → %s", keywords[0], option)
                    return True
            except Exception as exc:
                logger.debug("下拉选项点击失败 (尝试 %d): %s", attempt + 1, exc)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(200)

        logger.warning("未能选择下拉 %s → %s", keywords, option)
        return await self._select_dropdown_by_rowtitle_playwright(page, keywords, option)

    async def _select_dropdown_by_rowtitle_playwright(
        self, page: "Page", keywords: list[str], option: str
    ) -> bool:
        """Playwright 回退：按 rowTitle/label 所在表格行定位下拉"""
        opt_re = re.compile(re.escape(option), re.I)
        kw_res = [re.compile(re.escape(k), re.I) for k in keywords if k]
        titles = page.locator(".rowTitle, th, label, .ant-form-item-label, .control-label")

        for i in range(await titles.count()):
            txt = (await titles.nth(i).inner_text()).strip()
            if not any(r.search(txt) for r in kw_res):
                continue

            row = titles.nth(i).locator("xpath=ancestor::tr[1]")
            scope = row if await row.count() > 0 else titles.nth(i).locator(
                "xpath=ancestor::*[contains(@class,'content') or contains(@class,'row') or contains(@class,'form-group')][1]"
            )
            if await scope.count() == 0:
                scope = titles.nth(i).locator("xpath=..")

            trigger = scope.locator(
                ".ant-select-selector, .ant-select, [role=combobox], select"
            ).first
            if await trigger.count() == 0:
                trigger = titles.nth(i).locator(
                    "xpath=following-sibling::td[1] | following-sibling::div[1]"
                ).locator(".ant-select-selector, .ant-select, [role=combobox], select").first

            if await trigger.count() == 0:
                continue

            try:
                tag = await trigger.evaluate("el => el.tagName")
                await trigger.scroll_into_view_if_needed()
                if tag == "SELECT":
                    await trigger.select_option(label=option)
                else:
                    await trigger.click(force=True, timeout=5000)
                    await page.wait_for_timeout(400)
                    search = page.locator(
                        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) input"
                    ).last
                    if await search.count() > 0:
                        await search.fill(option)
                        await page.wait_for_timeout(400)
                    opt = page.locator(
                        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option"
                    ).filter(has_text=opt_re).first
                    if await opt.count() > 0:
                        await opt.click(force=True)
                    else:
                        await page.get_by_role("option", name=opt_re).first.click(
                            force=True, timeout=3000
                        )
                await page.wait_for_timeout(400)
                if opt_re.search(await scope.inner_text()):
                    logger.info("rowTitle 已选 [%s] → %s", txt[:30], option)
                    return True
            except Exception as exc:
                logger.debug("rowTitle 选择失败 [%s]: %s", txt[:30], exc)
            await page.keyboard.press("Escape")

        return False

    async def _get_ant_form_item_by_label(self, page: "Page", label_pattern: str):
        """按 .ant-form-item-label 文字定位表单项（优先最短/最精确标签）"""
        regex = re.compile(label_pattern, re.I)
        items = page.locator(".ant-form-item")
        best = None
        best_len = 9999
        for i in range(await items.count()):
            item = items.nth(i)
            label = item.locator(".ant-form-item-label, label")
            if await label.count() == 0:
                continue
            txt = (await label.first.inner_text()).strip()
            norm = self._normalize_form_label(txt)
            if regex.search(txt) or regex.search(norm):
                n = len(norm)
                if n < best_len:
                    best = item
                    best_len = n
        if best is not None:
            return best
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

    async def _click_first_visible_select_option(self, page: "Page") -> bool:
        """点击当前可见下拉列表中的第一项（跳过「请选择」等占位项）"""
        skip_re = re.compile(r"^请选择$|^請選擇$|^select$", re.I)
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
                ".ant-select-item-option:not(.ant-select-item-option-disabled)",
                ".ant-select-item:not(.ant-select-item-disabled)",
                ".rc-select-item-option:not(.rc-select-item-option-disabled)",
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
                    if not txt or skip_re.search(txt):
                        continue
                    try:
                        content = opt.locator(
                            ".ant-select-item-option-content, .rc-select-item-option-content"
                        ).first
                        target = content if await content.count() > 0 else opt
                        await target.scroll_into_view_if_needed()
                        await target.click(force=True, timeout=3000)
                        await page.wait_for_timeout(500)
                        logger.info("已选择下拉首项: %s", txt[:40])
                        return True
                    except Exception:
                        try:
                            await opt.click(force=True, timeout=3000)
                            await page.wait_for_timeout(500)
                            logger.info("已选择下拉首项: %s", txt[:40])
                            return True
                        except Exception:
                            continue
        return False

    async def _select_ant_dropdown_first_option_by_label(
        self,
        page: "Page",
        label_pattern: str,
    ) -> bool:
        """Ant Design Select：按表单项标签打开下拉并选择第一项"""
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
            if await self._click_first_visible_select_option(page):
                logger.info("已选择下拉首项 [%s]", label_pattern)
                return True
            try:
                await page.keyboard.press("ArrowDown")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)
                section = await self._get_ant_form_item_by_label(page, label_pattern)
                selected = section.locator(".ant-select-selection-item").first
                if await selected.count() > 0:
                    txt = (await selected.inner_text()).strip()
                    if txt and not re.search(r"请选择|請選擇", txt):
                        logger.info("键盘已选择下拉首项 [%s] → %s", label_pattern, txt[:40])
                        return True
            except Exception:
                pass
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
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
            # 策略 2b：输入关键字筛选（如 香港仔 / English）
            search = re.sub(r"[\^$()\[\]]", "", option_pattern.split("|")[0])
            if search and len(search) <= 24:
                try:
                    await page.keyboard.type(search, delay=60)
                    await page.wait_for_timeout(600)
                    if await self._click_visible_select_option(page, option_pattern):
                        if await self._verify_dropdown_selected(
                            page, label_pattern, option_pattern
                        ):
                            logger.info(
                                "搜索已选择下拉 [%s] → %s", label_pattern, option_pattern
                            )
                            return True
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(500)
                    if await self._verify_dropdown_selected(
                        page, label_pattern, option_pattern
                    ):
                        logger.info(
                            "Enter 已选择下拉 [%s] → %s", label_pattern, option_pattern
                        )
                        return True
                except Exception as exc:
                    logger.debug("搜索下拉选择失败: %s", exc)
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
        await page.wait_for_timeout(_FORM_PAUSE_MS)

        filled = 0
        username, password = derive_icris_credentials(data)
        logger.info("开始填写账户资料 (url=%s)", page.url)
        await self._log_account_profile_status(page, prefix="填写前 ")

        # 优先：s02 原生表单 (#userType / filing / search / serviceType / #userId ...)
        filled = await self._fill_account_profile_native(page, data)
        if filled < 5:
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

            for selector, value in (
                ("#userId", username),
                ("#password", password),
                ("#confirm", password),
            ):
                if await self._fill_native_input(page, selector, value):
                    filled += 1
        else:
            status = await self._get_account_profile_status(page)
            logger.info(
                "账户资料原生填写完成 %d 项 (用户名=%s, 状态=%s)",
                filled,
                username,
                status,
            )

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

        if filled > 0 and status.get("userCategory") and status.get("username"):
            if await self._click_account_profile_continue(page):
                await self._log_page(page, "账户资料继续后")
            else:
                logger.warning("账户资料填写后未能点击「继续」")
        elif filled > 0:
            logger.warning("账户资料未完整，跳过点击继续: %s", status)

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
                        if await inp.is_disabled():
                            continue
                        await inp.fill(value)
                        return True
                inp = lbl.locator("xpath=following::input[1] | following::textarea[1]")
                if await inp.count() > 0:
                    if await inp.first.is_disabled():
                        continue
                    await inp.first.fill(value)
                    return True

        for kw in keywords:
            for attr in ("name", "id", "placeholder"):
                inp = scope.locator(f"input[{attr}*='{kw}' i], textarea[{attr}*='{kw}' i]")
                if await inp.count() > 0:
                    await inp.first.fill(value)
                    return True

        return False

    def _is_user_info_url(self, url: str) -> bool:
        return bool(re.search(r"registration/s03", url.lower()))

    async def _is_user_info_step(self, page: "Page") -> bool:
        if self._is_user_info_url(page.url):
            return True
        return bool(
            await page.evaluate(
                """() => /填写用户资料|填寫用戶資料|步骤2\\s*-\\s*填写用户资料/.test(
                    document.body ? document.body.innerText : ''
                )"""
            )
        )

    async def _wait_for_user_info_form(self, page: "Page", timeout_ms: int = 90000) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const t = document.body ? document.body.innerText : '';
                    if (!/填写用户资料|填寫用戶資料/.test(t)) return false;
                    const inputs = document.querySelectorAll(
                        "input:not([disabled]):not([type='hidden'])"
                    );
                    const selects = document.querySelectorAll(
                        '.ant-select, [role=combobox], select:not([disabled])'
                    );
                    return inputs.length >= 2 || selects.length >= 1;
                }""",
                timeout=timeout_ms,
            )
            await page.wait_for_timeout(_FORM_PAUSE_MS)
            return True
        except Exception:
            return await self._is_user_info_step(page)

    async def _fill_enabled_field_by_label(
        self,
        page: "Page",
        label_pattern: str,
        value: str,
        *,
        field_type: str = "text",
    ) -> bool:
        """按标签填写可用字段（跳过 disabled，兼容 Vue）"""
        if not value:
            return False
        ok = await page.evaluate(
            """([labelPat, val, ftype]) => {
                const labelRe = new RegExp(labelPat, 'i');
                const setNativeValue = (el, v) => {
                    const Ctor = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
                    const setter = Object.getOwnPropertyDescriptor(Ctor.prototype, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const blocks = document.querySelectorAll(
                    '.ant-form-item, fieldset, .form-group, tr, .content'
                );
                for (const item of blocks) {
                    const label = item.querySelector(
                        'label, .ant-form-item-label, .rowTitle, th, .control-label'
                    );
                    const labelText = label ? (label.innerText || '').trim() : '';
                    if (!labelText || !labelRe.test(labelText)) continue;
                    if (ftype === 'select') {
                        const sel = item.querySelector('select:not([disabled])');
                        if (!sel) continue;
                        const opt = [...sel.options].find(o =>
                            new RegExp(val, 'i').test((o.textContent || '').trim()) || o.value === val
                        );
                        if (opt) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                        continue;
                    }
                    const inp = item.querySelector(
                        "input:not([disabled]):not([type='checkbox']):not([type='radio']):not([type='hidden']), "
                        + "textarea:not([disabled])"
                    );
                    if (!inp) continue;
                    inp.focus();
                    setNativeValue(inp, val);
                    return true;
                }
                return false;
            }""",
            [label_pattern, value, field_type],
        )
        if ok:
            logger.info("已填写用户资料 [%s]", label_pattern)
        return bool(ok)

    async def _fill_by_placeholder(
        self,
        page: "Page",
        placeholder_pattern: str,
        value: str,
        *,
        index: int = 0,
    ) -> bool:
        """按 input placeholder 填写（s03 Vue 表单常用 placeholder 作标签）"""
        if not value:
            return False
        ph_re = re.compile(placeholder_pattern, re.I)
        try:
            inputs = page.locator(
                "input.ant-input:not([disabled]), "
                "textarea.ant-input:not([disabled]), "
                "input:not([disabled]):not([type='hidden']):not([type='checkbox']):not([type='radio'])"
            )
            matches: list[Any] = []
            for i in range(await inputs.count()):
                item = inputs.nth(i)
                ph = (await item.get_attribute("placeholder") or "").strip()
                aria = (await item.get_attribute("aria-label") or "").strip()
                if ph_re.search(ph) or ph_re.search(aria):
                    matches.append(item)
            if len(matches) > index:
                target = matches[index]
                await target.scroll_into_view_if_needed()
                await target.fill(value)
                await target.dispatch_event("input")
                await target.dispatch_event("change")
                logger.info("已按 placeholder 填写 [%s]", placeholder_pattern)
                return True
        except Exception as exc:
            logger.debug("placeholder fill failed: %s", exc)

        ok = await page.evaluate(
            """([pat, val, idx]) => {
                const phRe = new RegExp(pat, 'i');
                const setNativeValue = (el, v) => {
                    const Ctor = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
                    const setter = Object.getOwnPropertyDescriptor(Ctor.prototype, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const inputs = [...document.querySelectorAll(
                    "input:not([disabled]):not([type='hidden']):not([type='checkbox']):not([type='radio']), "
                    + "textarea:not([disabled])"
                )].filter(inp => {
                    const ph = inp.placeholder || inp.getAttribute('aria-label') || '';
                    return phRe.test(ph);
                });
                if (inputs.length <= idx) return false;
                const inp = inputs[idx];
                inp.focus();
                setNativeValue(inp, val);
                return true;
            }""",
            [placeholder_pattern, value, index],
        )
        if ok:
            logger.info("已按 placeholder(JS) 填写 [%s]", placeholder_pattern)
        return bool(ok)

    async def _fill_user_info_step(self, page: "Page", data: dict[str, Any]) -> int:
        """填写用户资料（s03）：称谓/姓名/地址/联络资料"""
        if not await self._is_user_info_step(page):
            if not await self._wait_for_user_info_form(page, timeout_ms=30000):
                logger.warning("当前不在用户资料步骤, url=%s", page.url)
                return 0

        await self._ensure_traditional_chinese(page)
        if not await self._wait_for_user_info_form(page):
            logger.warning("用户资料表单未就绪, url=%s", page.url)
        try:
            await page.wait_for_function(
                """() => /郵遞區號|邮递区号|通訊語言|通讯语言|區.*市.*省/.test(
                    document.body ? document.body.innerText : ''
                )""",
                timeout=20000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(_FORM_PAUSE_MS)

        applicant = data.get("applicant", {})
        given, surname = split_applicant_english_name(applicant.get("name_en", ""))
        title = applicant.get("title", "Mr")
        id_type = applicant.get("id_type", "HKID")
        email = applicant.get("email", "")
        phone = applicant.get("phone", "")
        addr = derive_mock_china_address(applicant)

        logger.info("开始填写用户资料 (url=%s)", page.url[:120])

        filled = 0

        async def _inc(ok: bool, label: str = "") -> None:
            nonlocal filled
            if ok:
                filled += 1
                if label:
                    logger.info("用户资料: %s", label)
                await page.wait_for_timeout(250)

        # 姓名（placeholder 为主；中文姓名留空不填）
        for pat, val, name in [
            (r"英文姓氏|英文姓", surname, "英文姓氏"),
            (r"英文名字|英文名", given, "英文名字"),
        ]:
            ok = await self._fill_by_placeholder(page, pat, val)
            if not ok:
                ok = await self._fill_enabled_field_by_label(page, pat, val)
            await _inc(ok, name)

        for label_pat, value, ftype, name in [
            (r"称谓|稱謂|Title", title, "select", "称谓"),
            (r"身份识别类别|身份識別類別|证件类型|證件類型", id_type, "select", "证件类型"),
            (
                r"身份识别号码|身份識別號碼|证件号码|證件號碼",
                applicant.get("id_number", ""),
                "text",
                "证件号码",
            ),
        ]:
            await _inc(
                await self._fill_enabled_field_by_label(
                    page, label_pat, str(value), field_type=ftype
                ),
                name,
            )

        # 本地地址（香港仔为 HK 地区选项）
        if not await self._select_radio_in_section(page, r"地址", r"本地地址|本地"):
            await _inc(
                await self._select_radio_in_section(page, r"地址", r"非香港地址|非香港"),
                "非香港地址",
            )
        else:
            await _inc(True, "本地地址")
        try:
            await page.wait_for_function(
                """() => {
                    const sels = [...document.querySelectorAll('select')];
                    if (sels.some(s => s.options && s.options.length > 1)) return true;
                    return document.querySelectorAll('.ant-select, [role=combobox]').length > 0;
                }""",
                timeout=15000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(_FORM_PAUSE_MS)

        for pat, val, name in [
            (r"室.*楼.*座|室.*樓.*座", addr["room"], "室/楼/座"),
            (r"大厦|大廈", addr["building"], "大厦"),
            (r"街道|屋苑|地段|村", addr["street"], "街道"),
        ]:
            ok = await self._fill_by_placeholder(page, pat, val)
            if not ok:
                ok = await self._fill_enabled_field_by_label(page, pat, val)
            await _inc(ok, name)

        # 区/市/省/州/邮递区号 → 香港仔
        await _inc(
            await self._select_ant_select_by_keywords(
                page,
                ["郵遞區號", "邮递区号", "區/市", "区/市", "區市省", "州"],
                "香港仔",
            ),
            "区/市/省=香港仔",
        )

        # 国家/地区（仅非香港地址时可能出现）
        if await page.locator(".ant-select").count() > 1:
            country_ok = await self._select_ant_select_by_keywords(
                page, ["国家", "國家", "国家/地区", "國家/地區"], "中国"
            )
            if not country_ok:
                country_ok = await self._select_ant_select_by_keywords(
                    page, ["国家", "國家", "国家/地区", "國家/地區"], "中國"
                )
            await _inc(country_ok, "国家/地区")

        # 电邮 + 确认电邮
        for pat, name in [
            (r"^电邮地址$|^電郵地址$", "电邮地址"),
            (r"确认电邮|確認電郵", "确认电邮"),
        ]:
            ok = await self._fill_by_placeholder(page, pat, email)
            if not ok:
                ok = await self._fill_enabled_field_by_label(page, pat, email)
            await _inc(ok, name)

        # 香港联络电话
        ok = await self._fill_by_placeholder(page, r"联络电话|聯絡電話|香港联络", phone)
        if not ok:
            ok = await self._fill_enabled_field_by_label(
                page, r"香港联络|聯絡電話|流动电话|流動電話", phone
            )
        await _inc(ok, "联络电话")

        # 通讯语言 → English
        await _inc(
            await self._select_ant_select_by_keywords(
                page, ["通訊語言", "通讯语言"], "English"
            ),
            "通讯语言=English",
        )

        # 按常见 id/name 再填一次（Vue 表单兜底）
        id_map = [
            ("#engSurName, input[name*='engSur' i], input[id*='engSur' i]", surname, "text"),
            ("#engOtherName, input[name*='engOther' i], input[id*='engOther' i]", given, "text"),
            ("#emailAddr, input[name*='email' i]:not([name*='confirm' i])", email, "text"),
            ("input[name*='confirm' i][name*='email' i], input[id*='confirmEmail' i]", email, "text"),
            ("#mobileNo, input[name*='mobile' i], input[id*='mobile' i], input[name*='phone' i]", phone, "text"),
            ("#idNo, input[name*='idNo' i], input[id*='idNum' i], input[name*='hkid' i]", applicant.get("id_number", ""), "text"),
        ]
        for selector, value, ftype in id_map:
            if not value:
                continue
            for sel in selector.split(", "):
                sel = sel.strip()
                if ftype == "text" and await self._fill_native_input(page, sel, str(value)):
                    filled += 1
                    break

        logger.info("用户资料已填写 %d 项", filled)

        if filled > 0:
            if await self._click_continue(page):
                await self._log_page(page, "用户资料继续后")
            else:
                logger.warning("用户资料填写后未能点击「继续」")

        return filled

    def _is_identity_proof_url(self, url: str) -> bool:
        return bool(re.search(r"registration/s04", url.lower()))

    async def _is_identity_proof_step(self, page: "Page") -> bool:
        if self._is_identity_proof_url(page.url):
            return True
        return bool(
            await page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText : '';
                    return /身分證明|身份证明/.test(t)
                        && /證明文件|证明文件/.test(t)
                        && /網上提交|网上提交|親身到公司註冊處|亲身到公司注册处/.test(t);
                }"""
            )
        )

    def _derive_identity_proof(self, data: dict[str, Any]) -> dict[str, Any]:
        """从 mock 解析身份证明选项（默认：中国身份证 + 网上提交 + 经核证真实副本）"""
        proof = data.get("identity_proof") or {}
        applicant = data.get("applicant") or {}
        id_type = str(proof.get("id_type") or applicant.get("id_type") or "PRC_ID").upper()
        # 无号码时保持空，避免用演示假号上生产；由材料视觉识别或客户补充写入
        id_number = str(proof.get("id_number") or applicant.get("id_number") or "").strip()

        type_labels = {
            "HKID": r"香港身分證|香港身份証|香港身份证",
            "PRC_ID": r"中華人民共和國身分證|中华人民共和国身份证|中國身分證|中国身份证",
            "PASSPORT": r"護照號碼|护照号码|護照|护照",
        }
        id_type_pat = type_labels.get(id_type, type_labels["PRC_ID"])
        if proof.get("id_type_label"):
            id_type_pat = re.escape(str(proof["id_type_label"]))

        submission = str(proof.get("submission_method") or "online").lower()
        if submission in ("in_person", "onsite", "person"):
            submission_pat = r"親身到公司註冊處|亲身到公司注册处|出示證明文件正本|出示证明文件正本"
        else:
            submission_pat = r"^網上提交$|^网上提交$|網上提交|网上提交"

        online_method = str(proof.get("online_document_method") or "certified_copy").lower()
        if online_method in ("digital", "digital_cert", "cert"):
            online_pat = r"使用數碼證書|使用数码证书|數碼證書|数码证书"
        else:
            online_pat = (
                r"經核證真實副本|经核证真实副本|身分證明文件的經核證|身份证明文件的经核证"
            )
        if proof.get("online_document_label"):
            online_pat = re.escape(str(proof["online_document_label"]))

        return {
            "id_type": id_type,
            "id_number": id_number,
            "id_type_pat": id_type_pat,
            "submission_pat": submission_pat,
            "online_pat": online_pat,
            "submission_method": submission,
            "online_method": online_method,
            "document_files": self._resolve_identity_document_files(proof),
        }

    def _resolve_identity_document_files(self, proof: dict[str, Any]) -> list[str]:
        """解析身份证照片路径：优先 mock 配置，否则在桌面「戴启乐资料*」中查找"""
        files: list[str] = []
        for item in proof.get("document_files") or []:
            p = Path(str(item)).expanduser()
            if p.is_file():
                files.append(str(p.resolve()))
        if files:
            return files

        dirs: list[Path] = []
        if proof.get("document_dir"):
            dirs.append(Path(str(proof["document_dir"])).expanduser())
        desktop = Path.home() / "Desktop"
        if desktop.is_dir():
            for d in sorted(desktop.glob("戴启乐资料*")):
                if d.is_dir():
                    dirs.append(d)

        preferred_names = (
            "身份证",
            "身份证正面",
            "身份证方面",
            "身份证反面",
            "身分證正面",
            "身分證反面",
        )
        for d in dirs:
            if not d.is_dir():
                continue
            for name in preferred_names:
                for ext in (".jpg", ".jpeg", ".png", ".pdf", ".JPG", ".PNG"):
                    cand = d / f"{name}{ext}"
                    if cand.is_file() and str(cand.resolve()) not in files:
                        files.append(str(cand.resolve()))
            # 已找到「身份证.jpg」这类主图则不再堆叠其它
            if any(Path(f).stem == "身份证" for f in files):
                files = [f for f in files if Path(f).stem == "身份证"]
                break
            if files:
                break

        if not files:
            logger.warning("未找到身份证照片（请检查桌面「戴启乐资料」文件夹）")
        else:
            logger.info("身份证明上传文件: %s", [Path(f).name for f in files])
        return files

    async def _upload_identity_documents(self, page: "Page", file_paths: list[str]) -> int:
        """选择经核证真实副本后上传身份证照片"""
        if not file_paths:
            return 0
        existing = [p for p in file_paths if Path(p).is_file()]
        if not existing:
            logger.warning("身份证照片文件不存在: %s", file_paths)
            return 0

        await page.wait_for_timeout(500)
        # 等待上传控件出现
        try:
            await page.wait_for_selector(
                "input[type='file'], .ant-upload, button:has-text('上載'), button:has-text('上传'), "
                "button:has-text('選擇'), button:has-text('选择'), a:has-text('上載')",
                timeout=8000,
            )
        except Exception:
            logger.debug("上传控件等待超时，继续尝试定位 input[type=file]")

        uploaded = 0
        file_inputs = page.locator("input[type='file']")
        count = await file_inputs.count()
        if count > 0:
            # 多个 input：正面/反面分别上传；单个 input：一次传全部（若支持 multiple）
            if count >= len(existing):
                for i, path in enumerate(existing):
                    try:
                        await file_inputs.nth(i).set_input_files(path)
                        uploaded += 1
                        logger.info("已上传身份证明文件[%d]: %s", i + 1, Path(path).name)
                        await page.wait_for_timeout(800)
                        await self._wait_spin_clear(page, timeout_ms=20000)
                    except Exception as e:
                        logger.warning("上传失败 [%s]: %s", Path(path).name, e)
            else:
                try:
                    await file_inputs.first.set_input_files(existing)
                    uploaded = len(existing)
                    logger.info(
                        "已批量上传身份证明文件: %s",
                        [Path(p).name for p in existing],
                    )
                    await page.wait_for_timeout(1000)
                    await self._wait_spin_clear(page, timeout_ms=30000)
                except Exception as e:
                    # 不支持 multiple 时逐个试
                    logger.debug("批量上传失败，改为逐个: %s", e)
                    for path in existing:
                        try:
                            await file_inputs.first.set_input_files(path)
                            uploaded += 1
                            logger.info("已上传: %s", Path(path).name)
                            await page.wait_for_timeout(800)
                            await self._wait_spin_clear(page, timeout_ms=20000)
                        except Exception as exc:
                            logger.warning("上传失败 [%s]: %s", Path(path).name, exc)
            return uploaded

        # 无隐藏 file input：点击上传按钮触发文件选择器
        upload_btns = [
            page.locator("button, a, span").filter(
                has_text=re.compile(r"上載|上传|選擇檔案|选择文件|Browse|Upload|附加", re.I)
            ).first,
            page.locator(".ant-upload button, .ant-btn").filter(
                has_text=re.compile(r"上載|上传|選擇|选择", re.I)
            ).first,
        ]
        for btn in upload_btns:
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            try:
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                chooser = await fc_info.value
                await chooser.set_files(existing if len(existing) == 1 else existing[:1])
                uploaded = 1
                logger.info("通过文件选择器上传: %s", Path(existing[0]).name)
                await page.wait_for_timeout(1000)
                await self._wait_spin_clear(page, timeout_ms=30000)
                # 若还有第二张，再点一次
                if len(existing) > 1:
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc2:
                            await btn.click()
                        chooser2 = await fc2.value
                        await chooser2.set_files(existing[1])
                        uploaded += 1
                        logger.info("通过文件选择器上传: %s", Path(existing[1]).name)
                        await page.wait_for_timeout(1000)
                        await self._wait_spin_clear(page, timeout_ms=30000)
                    except Exception as e:
                        logger.debug("第二张上传跳过: %s", e)
                return uploaded
            except Exception as e:
                logger.debug("文件选择器上传失败: %s", e)

        logger.warning("未找到可上传的文件控件")
        return 0

    async def _click_radio_by_text(self, page: "Page", text_pattern: str) -> bool:
        """按可见文案点击 radio / ant-radio-wrapper"""
        if await self._verify_option_selected(page, text_pattern, option_type="radio"):
            logger.info("已选中 radio: %s", text_pattern)
            return True
        if await self._select_radio_in_section(page, r".*", text_pattern):
            return True
        opt_re = re.compile(text_pattern, re.I)
        wrappers = page.locator(".ant-radio-wrapper, label").filter(has_text=opt_re)
        for i in range(await wrappers.count()):
            item = wrappers.nth(i)
            if not await item.is_visible():
                continue
            txt = (await item.inner_text()).strip()
            if not opt_re.search(txt):
                continue
            # 避免「網上提交」误点到整段说明
            if len(txt) > 80 and not re.search(r"^網上提交|^网上提交", txt):
                continue
            try:
                await item.scroll_into_view_if_needed()
                await item.click(timeout=3000)
                await page.wait_for_timeout(300)
                logger.info("已点击 radio: %s", txt[:40])
                return True
            except Exception:
                inp = item.locator("input[type='radio']").first
                if await inp.count() > 0:
                    await inp.check(force=True)
                    await page.wait_for_timeout(300)
                    logger.info("已 force 选中 radio: %s", txt[:40])
                    return True
        return False

    async def _fill_identity_proof_step(self, page: "Page", data: dict[str, Any]) -> int:
        """填写身份证明（s04）：证件类型 / 号码 / 证明文件提交方式"""
        if not await self._is_identity_proof_step(page):
            logger.warning("当前不在身份证明步骤, url=%s", page.url)
            return 0

        await self._wait_spin_clear(page, timeout_ms=15000)
        proof = self._derive_identity_proof(data)
        logger.info(
            "开始填写身份证明 (type=%s, submission=%s, url=%s)",
            proof["id_type"],
            proof["submission_method"],
            page.url[:120],
        )

        filled = 0

        if await self._click_radio_by_text(page, proof["id_type_pat"]):
            filled += 1
            await page.wait_for_timeout(400)

        # 选择证件类型后可能出现号码输入框
        id_ok = False
        for pat in (
            r"身分證號碼|身份证号码|護照號碼|护照号码|證件號碼|证件号码|身分證明|身份证明",
            r"號碼|号码|Number",
        ):
            if await self._fill_by_placeholder(page, pat, proof["id_number"]):
                id_ok = True
                break
            if await self._fill_enabled_field_by_label(page, pat, proof["id_number"]):
                id_ok = True
                break
        if not id_ok:
            for sel in (
                "input[name*='idNo' i]",
                "input[id*='idNo' i]",
                "input[name*='idNum' i]",
                "input[name*='passport' i]",
                "input[placeholder*='號碼' i]",
                "input[placeholder*='号码' i]",
            ):
                if await self._fill_native_input(page, sel, proof["id_number"]):
                    id_ok = True
                    break
        if id_ok:
            filled += 1
            logger.info("身份证明号码已填写")
        else:
            logger.debug("未找到身份证明号码输入框（部分类型可能无需填写）")

        if await self._click_radio_by_text(page, proof["submission_pat"]):
            filled += 1
            await page.wait_for_timeout(400)

        if proof["submission_method"] not in ("in_person", "onsite", "person"):
            if await self._click_radio_by_text(page, proof["online_pat"]):
                filled += 1
                logger.info("已选择网上提交子项: certified_copy/digital")
                await page.wait_for_timeout(500)
            else:
                logger.warning("未能选择网上提交子项（数码证书/经核证副本）")

            # 经核证真实副本 → 上传桌面身份证照片
            if proof.get("online_method") not in ("digital", "digital_cert", "cert"):
                n = await self._upload_identity_documents(
                    page, list(proof.get("document_files") or [])
                )
                if n:
                    filled += n
                else:
                    logger.warning("身份证明文件未上传成功")

        logger.info("身份证明已填写 %d 项", filled)
        if filled > 0:
            if await self._click_continue(page):
                await self._log_page(page, "身份证明继续后")
            else:
                logger.warning("身份证明填写后未能点击「继续」")
        return filled

    async def _fill_registration_form(self, page: "Page", data: dict[str, Any]) -> int:
        if await self._is_user_info_step(page):
            return await self._fill_user_info_step(page, data)
        if await self._is_identity_proof_step(page):
            return await self._fill_identity_proof_step(page, data)

        if not self._is_registration_page(page.url):
            logger.warning("跳过填表：当前不在 registration 页面")
            return 0

        applicant = data.get("applicant", {})
        username, password = derive_icris_credentials(data)
        field_map = [
            (["title", "salutation", "稱謂"], applicant.get("title", "Mr")),
            (["surname", "last", "英文姓氏"], applicant.get("name_en", "").split()[-1] if applicant.get("name_en") else ""),
            (["given", "first", "英文名字"], " ".join(applicant.get("name_en", "").split()[:-1]) if applicant.get("name_en") else ""),
            (["nameEn", "englishName", "英文"], applicant.get("name_en", "")),
            (["nameCh", "chineseName", "中文"], applicant.get("name_cn", "")),
            (["email", "電郵", "邮箱"], applicant.get("email", "")),
            (["phone", "telephone", "電話", "电话"], applicant.get("phone", "")),
            (["idNo", "idNumber", "identity", "身份"], applicant.get("id_number", "")),
            (["address", "地址"], applicant.get("address", "")),
            (["hint", "passwordHint"], data.get("password_hint", "")),
            (["securityAnswer", "answer"], data.get("security_answer", "")),
        ]

        filled = 0
        for keywords, value in field_map:
            if value and await self._fill_field(page, keywords, str(value)):
                filled += 1

        selects = page.locator("form select:not([disabled])")
        for i in range(await selects.count()):
            sel = selects.nth(i)
            name = (await sel.get_attribute("name") or "").lower()
            if "idtype" in name or "id_type" in name or "doctype" in name:
                try:
                    await sel.select_option(label=applicant.get("id_type", "HKID"))
                    filled += 1
                except Exception:
                    try:
                        await sel.select_option(index=1)
                        filled += 1
                    except Exception:
                        pass

        logger.info("已填写 %d 个注册表单字段", filled)
        return filled

    async def _click_continue(self, page: "Page") -> bool:
        """点击继续/下一步（多步骤导航，dry_run 也执行）"""
        if not self._is_registration_page(page.url):
            return False

        await self._wait_spin_clear(page, timeout_ms=15000)

        continue_pattern = re.compile(r"继\s*续|繼\s*續|Continue|Next|下一步", re.I)

        async def _try_click(btn, *, require_text: bool = True) -> bool:
            if await btn.count() == 0 or not await btn.is_visible():
                return False
            try:
                txt = (await btn.inner_text()).strip()
            except Exception:
                txt = (await btn.get_attribute("value") or "").strip()
            if require_text and txt and not continue_pattern.search(txt):
                return False
            try:
                if await btn.is_disabled():
                    logger.warning("继续按钮不可用: %s", txt or await btn.get_attribute("class"))
                    return False
            except Exception:
                pass
            current_url = page.url
            try:
                await btn.scroll_into_view_if_needed()
                try:
                    await btn.click(timeout=8000)
                except Exception:
                    await btn.click(force=True, timeout=8000)
                logger.info("已点击继续: %s", txt or "(submit)")
                if not await self._wait_after_continue(page, current_url):
                    return False
                if self._is_home_or_portal(page.url):
                    logger.error("点击继续后跳转到首页")
                    return False
                return True
            except Exception as exc:
                logger.debug("点击继续失败: %s", exc)
                return False

        # s02 页面继续按钮：button[type=submit].primary（文字「继 续」）
        submit_continue = page.locator(
            "button[type='submit'].primary.ant-btn, button[type='submit'].primary"
        ).last
        if await _try_click(submit_continue, require_text=False):
            return True

        continue_buttons = [
            "button.primary.ant-btn:has-text('继续')",
            "button.primary.ant-btn:has-text('继 续')",
            "button.primary.ant-btn:has-text('繼 續')",
            "button.ant-btn-danger.primary",
            "button[type='submit'].primary.ant-btn",
            "button[type='submit'].primary",
            "button.ant-btn-primary:has-text('继续')",
            "button.ant-btn-primary:has-text('继 续')",
            "button.ant-btn-primary:has-text('繼 續')",
            "button.ant-btn-primary:has-text('Continue')",
            "button:has-text('继 续')",
            "button:has-text('繼 續')",
            "button:has-text('继续')",
            "button:has-text('Continue')",
            "button:has-text('下一步')",
            "button:has-text('Next')",
            "input[type='submit'][value*='继续' i]",
            "input[type='submit'][value*='Continue' i]",
        ]

        scopes = [
            page.locator("#uam-content, .content-wrapper, .formSection, form").first,
            page.locator("body"),
        ]

        for scope in scopes:
            if await scope.count() == 0:
                continue
            for sel in continue_buttons:
                btn = scope.locator(sel).first
                if await btn.count() == 0:
                    btn = page.locator(sel).first
                if await _try_click(btn):
                    return True

        role_btn = page.get_by_role("button", name=continue_pattern)
        if await role_btn.count() > 0:
            btn = role_btn.last
            if await _try_click(btn):
                return not self._is_home_or_portal(page.url)

        ok = await page.evaluate(
            """() => {
                const re = /继\\s*续|繼\\s*續|Continue|Next/i;
                const btns = [...document.querySelectorAll('button, input[type=submit], input[type=button]')];
                const btn = btns.find(b => re.test((b.innerText || b.value || '').trim()) && !b.disabled);
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        if ok:
            current_url = page.url
            logger.info("JS 已点击继续")
            if not await self._wait_after_continue(page, current_url):
                return False
            return not self._is_home_or_portal(page.url)
        return False

    async def _wait_after_continue(self, page: "Page", previous_url: str) -> bool:
        """点击继续后等待步骤切换且 loading 结束"""
        try:
            await page.wait_for_function(
                """(prev) => {
                    const href = window.location.href;
                    if (href !== prev) return true;
                    const t = document.body ? document.body.innerText : '';
                    return /填写用户资料|填寫用戶資料|用户类别|用戶類別|账户资料|帳戶資料|步骤|步驟/i.test(t)
                        || /registration\\/s0[2-9]/i.test(href);
                }""",
                previous_url,
                timeout=30000,
            )
        except Exception:
            logger.debug("继续后步骤切换等待超时 url=%s", page.url)

        if not await self._wait_spin_clear(page, timeout_ms=_SPIN_TIMEOUT_MS):
            logger.error("继续后 loading 未结束，可能表单校验失败")
            return False
        await page.wait_for_timeout(_PAGE_PAUSE_MS)
        return True

    async def _click_account_profile_continue(self, page: "Page") -> bool:
        """账户资料填写完成后点击继续"""
        if not await self._is_account_profile_step(page):
            return False
        logger.info("账户资料填写完成，点击「继续」进入下一步…")
        return await self._click_continue(page)

    async def _click_next_if_exists(self, page: "Page") -> bool:
        """兼容旧调用，统一走 _click_continue"""
        return await self._click_continue(page)

    async def run(
        self,
        data: dict[str, Any],
        *,
        force_isolated_browser: bool = False,
    ) -> None:
        """执行注册流程：打开浏览器 → 验证码 → 条款 → 填写表单（按开关提交）"""
        try:
            from src.browser.launcher import import_async_playwright

            async_playwright = import_async_playwright()
        except RuntimeError:
            raise
        except ImportError as e:
            raise RuntimeError(
                "请先安装 Playwright: pip install playwright && playwright install chromium"
            ) from e

        keep_open = max(10, settings.browser_keep_open_seconds)

        async with async_playwright() as p:
            browser = await launch_browser(
                p, force_isolated=force_isolated_browser
            )
            via_cdp = (
                bool(settings.chrome_use_existing and browser.contexts)
                and not force_isolated_browser
            )
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
                await self._wait_spin_clear(page, timeout_ms=20000)
                await self._wait_for_account_profile_step(page, timeout_ms=45000)

                # Step 2: 账户资料（s02）— 填写后自动点继续
                if await self._is_account_profile_step(page):
                    logger.info("=== 填写账户资料步骤 ===")
                    await self._fill_user_profile_step(page, data)
                else:
                    logger.warning("条款通过后未进入账户资料页, url=%s", page.url)

                # Step 3: 用户资料（s03）— 填写后自动点继续
                await self._wait_for_user_info_form(page, timeout_ms=45000)
                if await self._is_user_info_step(page):
                    logger.info("=== 填写用户资料步骤 ===")
                    await self._fill_user_info_step(page, data)
                else:
                    logger.warning("账户资料继续后未进入用户资料页, url=%s", page.url)

                # Step 4: 身份证明（s04）
                await self._wait_spin_clear(page, timeout_ms=20000)
                if await self._is_identity_proof_step(page):
                    logger.info("=== 填写身份证明步骤 ===")
                    await self._fill_identity_proof_step(page, data)
                else:
                    logger.info("暂未进入身份证明页, url=%s", page.url)

                # Step 5+: 其余多步表单
                max_steps = 6
                for step in range(max_steps):
                    if self._is_home_or_portal(page.url):
                        logger.error("步骤 %d 检测到跳转首页，停止", step + 5)
                        break
                    if await self._is_identity_proof_step(page):
                        await self._fill_identity_proof_step(page, data)
                    elif await self._is_user_info_step(page):
                        await self._fill_user_info_step(page, data)
                    elif await self._is_account_profile_step(page):
                        await self._fill_user_profile_step(page, data)
                    else:
                        logger.info("处理注册表单步骤 %d", step + 5)
                        filled = await self._fill_registration_form(page, data)
                        if filled == 0 and step > 0:
                            break
                        if not await self._click_continue(page):
                            break
                    await self._wait_spin_clear(page, timeout_ms=20000)
                    if not await self._ensure_on_registration(page, f"步骤{step + 5}后"):
                        break

                submit_btns = page.locator(
                    "form input[type='submit'], form button[type='submit']"
                )
                if await submit_btns.count() > 0:
                    if self.allow_submit:
                        logger.warning(
                            "即将点击 ICRIS 最终提交（DRY_RUN=false, ICRIS_ALLOW_SUBMIT=true）"
                        )
                        await submit_btns.first.click(timeout=15000)
                        await self._wait_spin_clear(page, timeout_ms=30000)
                        logger.warning("已点击 ICRIS 最终提交按钮")
                    else:
                        logger.info(
                            "检测到提交按钮，未点击（dry_run=%s icris_allow_submit=%s）",
                            self.dry_run,
                            settings.icris_allow_submit,
                        )

                if self.allow_submit:
                    logger.info("注册表单填写完成（已按开关尝试提交）")
                else:
                    logger.info("注册表单填写完成（未提交）")

            except Exception as e:
                run_error = e
                logger.exception("注册流程异常: %s", e)
                screenshot_path = ""
                try:
                    from config.settings import PROJECT_ROOT

                    shot_dir = PROJECT_ROOT / "data" / "icris_failures"
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    from datetime import datetime, timezone

                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    shot_file = shot_dir / f"job_fail_{stamp}.png"
                    if not page.is_closed():
                        await page.screenshot(path=str(shot_file), full_page=True)
                        screenshot_path = str(shot_file)
                        logger.warning("ICRIS 失败截图已保存: %s", screenshot_path)
                except Exception as shot_err:
                    logger.warning("保存失败截图失败: %s", shot_err)
                if screenshot_path:
                    from src.browser.icris_errors import IcrisFlowError

                    run_error = IcrisFlowError(str(e), screenshot_path=screenshot_path)
                    run_error.__cause__ = e
            finally:
                logger.info("浏览器保持打开 %d 秒供检查…", keep_open)
                try:
                    await page.wait_for_timeout(keep_open * 1000)
                except Exception:
                    pass
                await close_browser_session(browser, external_cdp=via_cdp)

            if run_error:
                raise run_error
