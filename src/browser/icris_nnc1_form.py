"""ICRIS3EP 已激活账号：NNC1 公司填表（与 ICRIS3EF s01-s05 账户登记分离）。

流程索引（搜编号即可跳转对应函数）：

  NNC1-0    前置：登录 / 打开 NNC1 / 接受条款
            _login → _open_nnc1 → _accept_efiling_terms
  NNC1-1    輸入基本資料
            _fill_step1_basic_info（法团印章 → 储存及继续）
  NNC1-2    輸入公司資料
            _fill_step2_company_info（名称 → 注册办事处 → 股本 → 储存及继续）
  NNC1-3    輸入創辦成員/董事/公司秘書資料
            _fill_step3_member_info
  NNC1-3.1  创办成员兼董事（自然人）
  NNC1-3.2  董事同意书签署人
  NNC1-3.3  公司秘书（法人团体）
  NNC1-3.4  人员列表储存及继续（_click_save_and_continue）
  NNC1-3.5  创办成员表簽署人下拉选第一项 → 储存及继续
            _fill_step3_form_signatory
  NNC1-4    致商業登記署通知書：选「否」→ 储存及继续
            _fill_step4_br_notice
  NNC1-5    詳情概要：滚到底 → 點「繼續」
            _fill_step5_summary
  NNC1-6    簽署及提交：董事同意书 → 创办成员陈述书 → 前往提交 → 初步检查
            _fill_step6_director_consent_sign（停在初步检查，含通过则成功，不点继续）
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT, settings
from src.browser.icris_ui_common import (
    ICRIS_PAGE_IS_LOADING_JS,
    dismiss_cookie_banner,
    dismiss_google_translate,
    dismiss_portal_modals,
    is_cr_public_site,
    is_page_loading,
    is_simplified_chinese_active,
    page_language_state,
    wait_after_nav_loading,
    wait_portal_ready,
    wait_spin_clear,
)
from src.browser.launcher import create_browser_context, launch_browser
from src.email.imap_client import IcrisAccount
from src.materials.address_classify import (
    load_s03_district_options,
    stored_s03_address_fields,
)
from src.materials.countries import icris_country_select_candidates
from src.materials.id_type_classify import (
    nnc1_identity_fill_plan,
    normalize_stored_id_type,
    split_hkid_number,
)

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

class Nnc1CountryDropdownEmpty(RuntimeError):
    """步驟3 國家／地區 下拉没有选项，无法选中国。"""


def nnc1_address_radio_is_non_hk(text: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    return "非香港" in t or "非本地" in t


def nnc1_address_radio_is_hk(text: str) -> bool:
    """NNC1 点「香港地址／本港地址」，排除「非香港地址」。"""
    t = re.sub(r"\s+", "", text or "")
    if nnc1_address_radio_is_non_hk(t):
        return False
    return "香港地址" in t or "本港地址" in t


def nnc1_hk_district_label(text: str) -> bool:
    """香港地址「区」栏。不是「地区」（已是 Hong Kong），也不是国家。"""
    raw = (text or "").strip()
    compact = re.sub(r"\s+", "", raw)
    if re.search(r"地區|地区|國家|国家", compact):
        return False
    first = re.sub(r"\s+", "", raw.split("\n", 1)[0]).strip("*:：")
    return bool(re.fullmatch(r"區|区|District", first, re.I))


def nnc1_fold_district(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").replace("围", "圍"))


def nnc1_district_select_keys(region: str) -> list[str]:
    """入库 region → 下拉可匹配的原文 / value / 别名（含简繁围）。"""
    raw = (region or "").strip()
    if not raw:
        return []
    keys: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        s = (item or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        keys.append(s)

    add(raw)
    add(raw.replace("圍", "围"))
    add(raw.replace("围", "圍"))
    folded = nnc1_fold_district(raw)
    for row in load_s03_district_options():
        label = str(row.get("label") or "")
        value = str(row.get("value") or "")
        aliases = [str(a) for a in (row.get("aliases") or [])]
        blob = [label, value, *aliases]
        if any(nnc1_fold_district(x) == folded or x.strip() == raw for x in blob if x):
            add(label)
            add(value)
            for alias in aliases:
                add(alias)
            break
    return keys


def resolve_nnc1_person_address(
    person: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> dict[str, str]:
    """NNC1-3.1 住址与 s03 相同：只读入库字段。"""
    applicant = dict((data or {}).get("applicant") or {})
    stored = stored_s03_address_fields(person, applicant)
    if not any(
        stored.get(k) for k in ("flat", "building", "street", "region", "country")
    ):
        logger.warning(
            "NNC1 地址缺已入库字段 person_keys=%s",
            sorted((person or {}).keys())[:20],
        )
    return stored


def resolve_nnc1_step3_names(
    person: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """NNC1-3.1 姓名三栏，规则与 s03 相同。返回 (中文姓名, 英文姓氏, 英文名字)。"""
    from src.browser.icris_registration import (
        latin_english_given_surname,
        s03_skip_english_name,
    )

    payload = data if isinstance(data, dict) else {}
    applicant = payload.get("applicant") or {}
    if not isinstance(applicant, dict):
        applicant = {}
    row = person if isinstance(person, dict) else {}
    name_cn = "".join(
        ch for ch in str(row.get("name_cn") or "") if "\u4e00" <= ch <= "\u9fff"
    )
    name_en = str(row.get("name_en") or "").strip()
    raw_person = str(
        row.get("director_name") or applicant.get("director_name") or ""
    )
    surname_stored = str(
        row.get("surname_en") or applicant.get("surname_en") or ""
    ).strip()
    given_stored = str(
        row.get("given_en") or applicant.get("given_en") or ""
    ).strip()
    skip_en = s03_skip_english_name(raw_person, name_cn, name_en)
    if skip_en:
        surname, given = "", ""
    elif surname_stored or given_stored:
        surname, given = surname_stored, given_stored
    else:
        given, surname = latin_english_given_surname(name_en, name_cn)
    return name_cn, surname, given


_SIGNATORY_PLACEHOLDER_RE = re.compile(r"請選擇|请选择|^Select$", re.I)
_PAGE_VALIDATION_ERROR_RE = re.compile(
    r"请选择签署人|請選擇簽署人|请勾选空格|請勾選空格|不能为空|不能為空"
)


def is_signatory_placeholder(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or bool(_SIGNATORY_PLACEHOLDER_RE.search(t))


def first_real_signatory_index(option_texts: list[str]) -> int | None:
    """下拉选项中第一项真实簽署人（跳过请选择），按顺序不按姓名。"""
    for i, raw in enumerate(option_texts):
        if is_signatory_placeholder(raw):
            continue
        return i
    return None


# 与 _mark_signatory_select / 3.5 等待共用：找到可见簽署人下拉即可，不必等 option 灌完。
_NNC1_FIND_SIGNATORY_FN = r"""
function nnc1FindSignatoryControl() {
    const vis = (n) => {
        if (!n) return false;
        const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const afterLabel = (el) => {
        let n = el.nextElementSibling;
        while (n) {
            if (n.classList?.contains('ant-select') || n.tagName === 'SELECT') return n;
            const inner = n.querySelector('.ant-select, select');
            if (inner) return inner;
            n = n.nextElementSibling;
        }
        n = el.parentElement?.nextElementSibling || null;
        while (n) {
            if (n.classList?.contains('ant-select') || n.tagName === 'SELECT') return n;
            const inner = n.querySelector('.ant-select, select');
            if (inner) return inner;
            n = n.nextElementSibling;
        }
        return null;
    };
    const norm = (s) => (s || '').replace(/\s+/g, '').trim();
    const isSignatoryLabel = (t) => /^(簽署人|签署人)[:：]?$/.test(t) && t.length <= 8;
    for (const el of document.querySelectorAll('label, span, td, th, div, p')) {
        const t = norm(el.innerText || '');
        if (!isSignatoryLabel(t)) continue;
        const node = afterLabel(el);
        if (vis(node)) return node;
    }
    const visible = [...document.querySelectorAll('.ant-select, select')].filter(vis);
    if (visible.length >= 2) return visible[1];
    if (visible.length === 1) return visible[0];
    for (const el of document.querySelectorAll(
        'legend, label, span, div, th, p, td'
    )) {
        const raw = (el.innerText || '').trim();
        if (!/請選擇出任董事職位同意書的簽署人|选择出任董事职位同意书的签署人|請選擇創辦成員.*簽署人|请选择创办成员.*签署人/i.test(raw)) {
            continue;
        }
        const seen = new Set();
        const queue = [el, el.parentElement];
        while (queue.length) {
            const node = queue.shift();
            if (!node || seen.has(node)) continue;
            seen.add(node);
            if (node.classList?.contains('ant-select') && vis(node)) return node;
            if (node.tagName === 'SELECT' && vis(node)) return node;
            for (const child of node.children || []) queue.push(child);
            if (node.nextElementSibling) queue.push(node.nextElementSibling);
        }
    }
    return null;
}
"""


def prelim_check_passed(result_text: str) -> bool:
    """初步检查结果含「通过」才算成功（排除「不通过」）。"""
    t = result_text or ""
    if re.search(r"不通过|不通過|未能通过|未能通過", t):
        return False
    return bool(re.search(r"通过|通過", t))


class IcrisNnc1FormBot:
    """阶段 3：已激活账号登录 ICRIS3EP → NNC1 填表（不最终提交）。"""

    # ========== 通用工具 ==========

    def __init__(self) -> None:
        self.dry_run = settings.dry_run
        self._job_summary_shot_written = False
        self._job_screenshot_path = ""

    async def _write_job_screenshot(self, page) -> str:
        dest = (self._job_screenshot_path or "").strip()
        if not dest:
            return ""
        try:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=dest, full_page=True)
            self._job_summary_shot_written = True
            logger.info("NNC1 填表截图已写入: %s", dest)
            return dest
        except Exception as e:
            logger.warning("写入填表截图失败: %s", e)
            return ""

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
        await self._write_job_screenshot(page)
        return str(path)

    async def _page_validation_error_text(self, page) -> str:
        raw = await self._eval_in_frames(
            page,
            """() => {
                const texts = [];
                for (const el of document.querySelectorAll(
                    '.ant-alert-error, .ant-form-item-explain-error, '
                    + '.ant-message-error, .ant-notification-notice-error'
                )) {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const t = (el.innerText || '').replace(/\\s+/g, '').trim();
                    if (t) texts.push(t);
                }
                return texts.join(' ');
            }""",
        )
        blob = str(raw or "").strip()
        if blob:
            return blob[:200]
        body = await self._body_text(page)
        compact = re.sub(r"\s+", "", body)
        m = _PAGE_VALIDATION_ERROR_RE.search(compact)
        return m.group(0) if m else ""

    async def _raise_if_page_error(self, page) -> None:
        msg = await self._page_validation_error_text(page)
        if not msg:
            return
        await self._maybe_screenshot(page, "page_validation_error")
        raise RuntimeError(f"页面校验失败: {msg}")

    async def _wait_nnc1_step_ready(
        self,
        page,
        *,
        heading_js: str,
        label: str,
        footer: str = "save_continue",
        timeout_ms: int = 120000,
    ) -> None:
        """等 spinner 消失、DOM 加载完、正文出现该步标题（及底栏按钮）后再操作。

        footer: save_continue | continue | none
        """
        logger.info("等待 NNC1 %s: %s", label, page.url[:120])
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_function(
                "({ headingSrc, footer }) => {\n"
                f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    const body = document.body?.innerText || '';
                    if (!new RegExp(headingSrc, 'i').test(body)) return false;
                    if (footer === 'none') return true;
                    const savePat = /(储存|存储|儲存)及(继续|繼續)|Save\\s*(?:and|&)\\s*Continue/i;
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button], .btn'
                    )) {
                        const raw = (el.innerText || el.value || el.textContent || '').trim();
                        const compact = raw.replace(/\\s+/g, '');
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
                            continue;
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (footer === 'continue') {
                            if (savePat.test(compact) || savePat.test(raw)) continue;
                            if (/^(繼續|继续|Continue)$/i.test(compact)) return true;
                            continue;
                        }
                        if (savePat.test(compact) || savePat.test(raw)) return true;
                    }
                    return false;
                }""",
                arg={"headingSrc": heading_js, "footer": footer},
                timeout=timeout_ms,
            )
            logger.info("NNC1 %s 已就绪", label)
        except Exception as e:
            shot = re.sub(r"[^\w]+", "_", label).strip("_")[:40] or "step"
            await self._maybe_screenshot(page, f"{shot}_wait_fail")
            raise RuntimeError(f"NNC1 {label} 页面加载超时: {e}")
        await page.wait_for_timeout(200)

    async def _wait_form_signatory_dropdown_ready(self, page) -> None:
        """创办成员表簽署人页：等簽署人控件可见即可，不必空等 option 灌入。

        选项可能要点开下拉才出现；`_select_first_signatory` 会打开再选。
        """
        logger.info("等待创办成员表簽署人下拉")
        await wait_spin_clear(page, timeout_ms=20000)
        try:
            await page.wait_for_function(
                "() => {\n"
                + _NNC1_FIND_SIGNATORY_FN
                + f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    return !!nnc1FindSignatoryControl();
                }""",
                timeout=8000,
            )
            logger.info("创办成员表簽署人下拉已就绪")
        except Exception as e:
            logger.debug("簽署人下拉短暂等待结束，继续选第一项: %s", e)
        await page.wait_for_timeout(200)

    async def _wait_nnc1_save_continue_ready(self, page) -> None:
        """等待步骤底栏「储存/存储及继续」按钮渲染完成。"""
        logger.info("等待 NNC1「储存及继续」按钮: %s", page.url[:120])
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass

        await self._scroll_form_to_bottom(page)

        try:
            await page.wait_for_function(
                "() => {\n"
                f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    const body = document.body?.innerText || '';
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
            await wait_after_nav_loading(page, timeout_ms=90000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await self._raise_if_page_error(page)
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
            await wait_after_nav_loading(page, timeout_ms=90000)
            logger.info("已点击「%s」(JS)", clicked)
            await self._raise_if_page_error(page)
            return

        await self._maybe_screenshot(page, "save_continue_fail")
        raise RuntimeError("未找到「储存及继续」按钮")

    async def _click_nnc1_back(self, page) -> None:
        """底栏「返回」（不是刷新）。用于國家／地區 加载失败后回到步骤2。"""
        await self._scroll_form_to_bottom(page)
        await page.wait_for_timeout(300)
        candidates = page.locator(
            "button, a, input[type='button'], input[type='submit'], [role='button']"
        )
        count = await candidates.count()
        chosen = None
        chosen_label = ""
        for i in range(count):
            el = candidates.nth(i)
            if not await el.is_visible():
                continue
            raw = ((await el.inner_text()) or (await el.get_attribute("value")) or "").strip()
            compact = re.sub(r"\s+", "", raw)
            if not re.fullmatch(r"返回|上一步|Back", compact, re.I):
                continue
            disabled = await el.evaluate(
                "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
            )
            if disabled:
                continue
            chosen = el
            chosen_label = compact
        if chosen is None:
            await self._maybe_screenshot(page, "back_fail")
            raise RuntimeError("未找到「返回」按钮，无法回到步骤2")
        await chosen.scroll_into_view_if_needed()
        try:
            await chosen.click(timeout=15000)
        except Exception:
            await chosen.click(force=True, timeout=15000)
        logger.info("已点击「%s」返回上一步", chosen_label)
        await wait_after_nav_loading(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass

    async def _reenter_step3_from_step2(self, page) -> None:
        """國家／地區 仍空：返回步骤2（不 F5）再储存及继续进入步骤3。"""
        logger.warning("國家／地區 未加载，返回步骤2后重新进入步骤3")
        await self._click_nnc1_back(page)
        await self._wait_nnc1_step_ready(
            page,
            heading_js=r"輸入公司資料|输入公司资料|公司英文名称|公司英文名稱",
            label="2 返回后",
            footer="save_continue",
        )
        await self._click_save_and_continue(page)
        await self._wait_step3_shell_ready(page)

    def _is_continue_only_label(self, raw: str) -> bool:
        compact = re.sub(r"\s+", "", raw or "")
        if NNC1_SAVE_CONTINUE_PAT.search(compact) or NNC1_SAVE_CONTINUE_PAT.search(raw or ""):
            return False
        return bool(re.fullmatch(r"繼續|继续|Continue", compact, re.I))

    async def _find_continue_only_button(self, page) -> Any | None:
        """底栏「繼續」——不含「储存及继续」。"""
        candidates = page.locator(
            "button, a, input[type='button'], input[type='submit'], [role='button']"
        )
        count = await candidates.count()
        for i in range(count):
            el = candidates.nth(i)
            if not await el.is_visible():
                continue
            raw = ((await el.inner_text()) or (await el.get_attribute("value")) or "").strip()
            if not self._is_continue_only_label(raw):
                continue
            disabled = await el.evaluate(
                "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
            )
            if disabled:
                continue
            return el
        return None

    async def _click_continue_only(self, page) -> None:
        """点击「繼續」（步骤5 詳情概要），等加载后再结束。不提交步骤6。"""
        await self._scroll_form_to_bottom(page)
        await page.wait_for_timeout(400)
        btn = await self._find_continue_only_button(page)
        if btn is not None:
            current_url = page.url
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            try:
                await btn.click(timeout=15000)
            except Exception:
                await btn.click(force=True, timeout=15000)
            label = ((await btn.inner_text()) or (await btn.get_attribute("value")) or "").strip()
            logger.info("已点击「%s」", label or "繼續")
            try:
                await page.wait_for_function(
                    "(url) => window.location.href !== url",
                    current_url,
                    timeout=30000,
                )
            except Exception:
                pass
            await wait_after_nav_loading(page, timeout_ms=90000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await self._raise_if_page_error(page)
            return

        clicked = await page.evaluate(
            """() => {
                const savePat = /(储存|存储|儲存)及(继续|繼續)|Save\\s*(?:and|&)\\s*Continue/i;
                for (const el of document.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], [role=button], .btn'
                )) {
                    const raw = (el.innerText || el.value || el.textContent || '').trim();
                    const compact = raw.replace(/\\s+/g, '');
                    if (savePat.test(compact) || savePat.test(raw)) continue;
                    if (!/^(繼續|继续|Continue)$/i.test(compact)) continue;
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
            await wait_after_nav_loading(page, timeout_ms=90000)
            logger.info("已点击「%s」(JS)", clicked)
            await self._raise_if_page_error(page)
            return

        await self._maybe_screenshot(page, "continue_fail")
        raise RuntimeError("未找到「繼續」按钮")

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
            try:
                await page.wait_for_selector(
                    ".ant-select-item-option",
                    timeout=15000,
                )
            except Exception:
                pass
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

    async def _wait_labeled_dropdown_options(
        self,
        page,
        label_patterns: list[str],
        *,
        min_options: int = 8,
        timeout_ms: int = 40000,
    ) -> bool:
        """等标签旁 native select / ant-select 出现真实选项（不只請選擇）。"""
        await wait_spin_clear(page, timeout_ms=30000)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            n = await page.evaluate(
                """({ labels }) => {
                    const placeholder = /請選擇|请选择|^Select$|^--+/i;
                    const norm = s => (s || '').replace(/[\\s/／:*：]/g, '');
                    const kws = labels.map(norm).filter(Boolean);
                    let nativeN = 0;
                    let antN = 0;
                    const titleEls = [...document.querySelectorAll(
                        '.rowTitle, th, label, .ant-form-item-label, .control-label, span, div'
                    )];
                    for (const title of titleEls) {
                        const tt = norm((title.innerText || '').trim());
                        if (tt.length > 60) continue;
                        if (!kws.some(k => k && tt.includes(k))) continue;
                        const containers = [
                            title.closest('tr'),
                            title.closest('.ant-row, .row, .form-group, fieldset, .ant-form-item'),
                            title.parentElement,
                        ].filter(Boolean);
                        for (const container of containers) {
                            const sel = container.querySelector('select');
                            if (sel) {
                                let c = 0;
                                for (const o of sel.options) {
                                    const tx = (o.textContent || '').trim();
                                    if (tx && !placeholder.test(tx)) c += 1;
                                }
                                nativeN = Math.max(nativeN, c);
                            }
                            const ant = container.querySelector('.ant-select');
                            if (ant && !ant.classList.contains('ant-select-disabled')) {
                                const open = ant.classList.contains('ant-select-open');
                                if (!open) {
                                    (ant.querySelector('.ant-select-selector') || ant).click();
                                }
                            }
                        }
                    }
                    for (const el of document.querySelectorAll(
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden) '
                        + '.ant-select-item-option, .ant-select-item-option'
                    )) {
                        const t = (el.innerText || '').trim();
                        if (t && !placeholder.test(t)) antN += 1;
                    }
                    return Math.max(nativeN, antN);
                }""",
                {"labels": label_patterns},
            )
            if int(n or 0) >= min_options:
                logger.info("下拉选项已加载 [%s] count=%s", label_patterns[0], n)
                return True
            await wait_spin_clear(page, timeout_ms=5000)
            await page.wait_for_timeout(400)
        logger.warning("下拉选项等待超时 [%s]", label_patterns[0])
        return False

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

    async def _ensure_checkbox_by_patterns(
        self, page, patterns: list[str], *, scope_pat: str = ""
    ) -> bool:
        """按文案勾选 checkbox（可限定在身分等区域）。"""
        hit = await page.evaluate(
            """({ patterns, scopePat }) => {
                const pats = patterns.map(p => (p || '').replace(/\\s+/g, ''));
                const scopeRe = scopePat ? new RegExp(scopePat, 'i') : null;
                let roots = [document];
                if (scopeRe) {
                    roots = [];
                    for (const el of document.querySelectorAll(
                        'legend, label, span, div, th, .rowTitle, h3, h4'
                    )) {
                        const t = (el.innerText || '').replace(/\\s+/g, '').trim();
                        if (!scopeRe.test(t)) continue;
                        const root = el.closest('fieldset, section, form, .ant-form, div')
                            || el.parentElement?.parentElement;
                        if (root) roots.push(root);
                    }
                    if (!roots.length) roots = [document];
                }
                const tryCheck = (cb) => {
                    if (!cb || cb.type !== 'checkbox') return false;
                    const r = cb.getBoundingClientRect();
                    if (r.width <= 0 && r.height <= 0) return false;
                    if (!cb.checked) cb.click();
                    return cb.checked;
                };
                const labelMatch = (t) => {
                    for (const p of pats) {
                        if (!p || !t.includes(p)) continue;
                        // 公司秘書：只接受短标签，避免点到「董事」帮助文案
                        if (/公司秘書|公司秘书/.test(p)) {
                            if (t.length > 24) continue;
                            if (/^董事/.test(t)) continue;
                            if (!(t === p || t.startsWith(p))) continue;
                            return true;
                        }
                        // 董事：短标签优先，跳过内含公司秘書的长说明
                        if (p === '董事') {
                            if (t.length > 12) continue;
                            if (/公司秘書|公司秘书/.test(t)) continue;
                            if (!(t === p || t.startsWith(p))) continue;
                            return true;
                        }
                        // 其他：短文案或以前缀匹配
                        if (t.length <= 40 && (t === p || t.startsWith(p) || t.includes(p))) {
                            return true;
                        }
                    }
                    return false;
                };
                for (const root of roots) {
                    for (const label of root.querySelectorAll(
                        '.ant-checkbox-wrapper, label'
                    )) {
                        const t = (label.innerText || '').replace(/\\s+/g, '');
                        if (!labelMatch(t)) continue;
                        const inp = label.querySelector('input[type=checkbox]');
                        if (inp && tryCheck(inp)) return t.slice(0, 60);
                        label.click();
                        return t.slice(0, 60);
                    }
                    for (const cb of root.querySelectorAll('input[type=checkbox]')) {
                        const ctx = (
                            cb.closest('label, .ant-checkbox-wrapper')?.innerText
                            || cb.closest('tr, div, li, fieldset, span')?.innerText
                            || ''
                        ).replace(/\\s+/g, '');
                        if (!labelMatch(ctx)) continue;
                        if (tryCheck(cb)) return ctx.slice(0, 60);
                    }
                }
                return '';
            }""",
            {"patterns": patterns, "scopePat": scope_pat},
        )
        if hit:
            logger.info("已勾选: %s → %s", patterns[0], hit)
            await page.wait_for_timeout(400)
            return True
        logger.warning("未勾选: %s", patterns[0])
        # Playwright 兜底
        pat = re.compile(patterns[0], re.I)
        wrappers = page.locator(".ant-checkbox-wrapper, label.ant-checkbox-wrapper")
        count = await wrappers.count()
        for i in range(count):
            item = wrappers.nth(i)
            if not await item.is_visible():
                continue
            txt = re.sub(r"\s+", "", (await item.inner_text() or ""))
            if len(txt) > 40 or not pat.search(txt):
                continue
            if patterns[0] in ("董事",) and "公司秘書" in txt:
                continue
            try:
                await item.scroll_into_view_if_needed()
                cls = await item.get_attribute("class") or ""
                if "ant-checkbox-wrapper-checked" in cls:
                    logger.info("已勾选(Playwright): %s", patterns[0])
                    return True
                await item.click(force=True, timeout=5000)
                await page.wait_for_timeout(400)
                inp = item.locator("input[type=checkbox]").first
                if await inp.count() > 0:
                    await inp.check(force=True)
                logger.info("已勾选(Playwright): %s", patterns[0])
                return True
            except Exception:
                pass
        return False

    # ========== NNC1-0 前置：登录 / 打开 NNC1 / 接受条款 ==========

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

    async def _after_nnc1_menu_click(self, page, current_url: str) -> None:
        """点完 NNC1 后等离开仪表盘；条款页由 _accept_efiling_terms 接着等。"""
        url = (page.url or "").lower()
        already = "e-filing" in url or "/nnc1/" in url or "nnc1init" in url
        if not already and page.url == current_url:
            try:
                await page.wait_for_function(
                    "(url) => window.location.href !== url",
                    current_url,
                    timeout=15000,
                )
            except Exception:
                pass
        await wait_spin_clear(page, timeout_ms=15000)

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
                    await target.click(force=True, timeout=10000)
                except Exception:
                    await loc.evaluate("el => (el.closest('a') || el).click()")
                await self._after_nnc1_menu_click(page, current_url)
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
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    continue
                if box["y"] < 85 or box["y"] > 480:
                    continue
                try:
                    await page.mouse.move(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                except Exception:
                    continue
                if click:
                    click_target = local.locator("xpath=ancestor-or-self::a[1]").first
                    try:
                        if await click_target.count() > 0:
                            await click_target.click(force=True, timeout=5000)
                        else:
                            await local.click(force=True, timeout=5000)
                    except Exception:
                        pass
                if not await self._wait_nnc1_dropdown_item(page, timeout_ms=4000):
                    continue
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
            if click:
                await self._hdr_menu_js_action(
                    page, label_re=r"^本地公司$", action="click"
                )
            if await self._wait_nnc1_dropdown_item(page, timeout_ms=4000):
                logger.info(
                    "已展开「本地公司」子菜单 (JS y=%.0f, click=%s)",
                    info["y"],
                    click,
                )
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
            await self._after_nnc1_menu_click(page, current_url)
            logger.info("已进入 NNC1 入口: %s → %s", text[:60], page.url[:120])
            return True

        current_url = page.url
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
            await self._after_nnc1_menu_click(page, current_url)
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
            await self._after_nnc1_menu_click(page, current_url)
            logger.info(
                "已进入 NNC1 入口 (JS 坐标): %s → %s",
                info.get("text"),
                page.url[:120],
            )
            return True

        return False

    async def _open_nnc1_wide_cascade(self, page) -> bool:
        """宽屏三级菜单：成立公司(已展开) → hover 本地公司 → 股份有限公司(表格NNC1)。"""
        try:
            if not await self._expand_local_company_submenu(page, click=False):
                await self._expand_local_company_submenu(page, click=True)
            return await self._click_nnc1_menu_label(page)
        except Exception as e:
            logger.warning("宽屏 NNC1 菜单级联失败: %s", e)
            return False

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
        await wait_spin_clear(page, timeout_ms=15000)

        wide = await self._is_wide_top_nav(page)
        if not wide:
            await self._open_main_nav(page)

        nav_targets = await self._find_incorporation_navs(page, wide=wide)
        if not nav_targets:
            raise RuntimeError("未找到顶栏「成立公司」菜单")

        for idx, nav in enumerate(nav_targets):
            if not wide:
                await nav.scroll_into_view_if_needed()
            box = await nav.bounding_box()
            if wide:
                if box:
                    await page.mouse.move(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                logger.info(
                    "顶栏展开 成立公司 #%d (%.0f, %.0f)",
                    idx + 1,
                    box["x"] if box else -1,
                    box["y"] if box else -1,
                )
                if not await self._wait_hdr_dropdown(page, timeout_ms=5000):
                    if box:
                        await page.mouse.move(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                    await self._wait_hdr_dropdown(page, timeout_ms=4000)
                if await self._open_nnc1_wide_cascade(page):
                    return
            else:
                await nav.click()
                await self._wait_hdr_dropdown(page, timeout_ms=4000)
                await self._expand_local_company_submenu(page)
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
            await self._wait_hdr_dropdown(page, timeout_ms=4000)
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
            current_url = page.url
            try:
                await link.click(timeout=15000)
            except Exception:
                await link.click(force=True, timeout=15000)
            await self._after_nnc1_menu_click(page, current_url)
            logger.info("已进入 NNC1 入口: %s → %s", text[:60], page.url[:120])
            return True

        current_url = page.url
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
            await self._after_nnc1_menu_click(page, current_url)
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
            await page.wait_for_function(
                "() => {\n"
                f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    const body = document.body ? document.body.innerText : '';
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
            await wait_after_nav_loading(page, timeout_ms=60000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
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
            await wait_after_nav_loading(page, timeout_ms=60000)
            logger.info("已点击 e-filing 接受 (JS)，URL: %s", page.url[:120])
            await self._wait_after_accept(page)
            return

        accept_btn = page.get_by_text("接受", exact=True).last
        if await accept_btn.count() > 0 and await accept_btn.is_visible():
            await accept_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await accept_btn.click(force=True, timeout=15000)
            await wait_after_nav_loading(page, timeout_ms=60000)
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
        await page.wait_for_timeout(300)

    # ========== NNC1-1 輸入基本資料 ==========

    async def _wait_nnc1_form_ready(self, page) -> None:
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_function(
                "() => {\n"
                f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    const body = document.body?.innerText || '';
                    return /输入基本资料|輸入基本資料|选择语言|選擇語言|法团印章|法團印章/.test(body);
                }""",
                timeout=90000,
            )
        except Exception as e:
            logger.warning("NNC1 步骤1 页面检测超时: %s", e)
        logger.info("NNC1 表单就绪: %s", page.url[:120])

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

    async def _fill_step1_basic_info(self, page, data: dict[str, Any]) -> None:
        """步骤1：输入基本资料 — 勾选法团印章 → 存储及继续。"""
        logger.info("NNC1 步骤1: 输入基本资料")
        # --- NNC1-1.1 等待填表页 ---
        await self._wait_nnc1_form_ready(page)
        # --- NNC1-1.2 勾选法团印章 ---
        await self._check_common_seal_checkbox(page)
        # --- NNC1-1.3 储存及继续 ---
        await self._click_save_and_continue(page)

    # ========== NNC1-2 輸入公司資料 ==========

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
                const enBad = Boolean(nameEn) && (
                    !en || /^[\\d,]+$/.test(en) || (en !== nameEn && /^\\d/.test(en))
                );
                const cnBad = Boolean(nameCn) && (!cn || /^[\\d,]+$/.test(cn));
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
                "() => {\n"
                f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    const t = document.body?.innerText || '';
                    return /输入公司资料|輸入公司資料|公司英文名称|公司英文名稱/.test(t);
                }""",
                timeout=90000,
            )
        except Exception:
            logger.warning("步骤2 页面文案检测超时，继续填表")

        # --- NNC1-2.1 公司中英文名称 ---
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
        # --- NNC1-2.2 注册办事处 ---
        await self._fill_registered_office_hk(page, office)
        # --- NNC1-2.3 股本 ---
        await self._fill_share_capital_table(page, sc)

        # --- NNC1-2.4 核对名称（防止股本填写误改公司名称：若英文名变成纯数字则重填）---
        await self._ensure_company_names(page, name_en, name_cn)

        # --- NNC1-2.5 储存及继续 ---
        await self._click_save_and_continue(page)

    # ========== NNC1-3.1 创办成员兼董事（自然人） ==========

    def _split_english_name(self, name_en: str) -> tuple[str, str]:
        """英文姓名 → (英文姓氏, 英文名字)，如 YAO Xiaojia → (YAO, Xiaojia)。"""
        parts = [p for p in (name_en or "").split() if p]
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], parts[0]
        return parts[0], " ".join(parts[1:])

    def _resolve_step3_person(self, data: dict[str, Any]) -> dict[str, Any]:
        """步骤3 人员：优先 founder_members，其次 directors，最后 applicant。"""
        founders = list(data.get("founder_members") or [])
        directors = list(data.get("directors") or [])
        applicant = dict(data.get("applicant") or {})
        if founders:
            return dict(founders[0])
        if directors:
            return dict(directors[0])
        return applicant

    async def _fill_step3_name_field(self, page, label_re: str, value: str) -> bool:
        """步骤3 姓名字段（textarea），按标签精确匹配。"""
        if not value:
            return False
        hit = await page.evaluate(
            """({ labelRe, value }) => {
                const re = new RegExp(labelRe, 'i');
                const norm = s => (s || '').replace(/\\s+/g, '').trim();
                for (const el of document.querySelectorAll(
                    'label, .rowTitle, span, div, td, th'
                )) {
                    const t = norm(el.innerText || '');
                    if (!t || t.length > 30) continue;
                    if (!re.test(t)) continue;
                    if (/前用姓名|別名|别名/.test(t)) continue;
                    let inp = el.querySelector('textarea, input:not([type=hidden]):not([type=checkbox])');
                    if (!inp) {
                        const row = el.closest('tr,.ant-row,.ant-form-item,fieldset,div')
                            || el.parentElement;
                        inp = row?.querySelector(
                            'textarea, input:not([type=hidden]):not([type=checkbox])'
                        );
                    }
                    if (!inp || inp.disabled) continue;
                    const r = inp.getBoundingClientRect();
                    if (r.width <= 0 && r.height <= 0) continue;
                    inp.focus();
                    inp.value = value;
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                    return t;
                }
                return '';
            }""",
            {"labelRe": label_re, "value": value},
        )
        if hit:
            logger.info("已填写姓名 [%s]: %s", label_re, value[:60])
            return True
        # Playwright 兜底
        pat = re.compile(label_re, re.I)
        labels = page.locator("label, .rowTitle, span, div").filter(has_text=pat)
        for i in range(await labels.count()):
            lab = labels.nth(i)
            txt = re.sub(r"\s+", "", (await lab.inner_text() or ""))
            if len(txt) > 30 or re.search(r"前用姓名|別名|别名", txt):
                continue
            if not pat.search(txt):
                continue
            inp = lab.locator(
                "xpath=ancestor-or-self::*[1]//textarea | following::textarea[1]"
            ).first
            if await inp.count() > 0:
                await inp.fill(value)
                logger.info("已填写姓名(Playwright) [%s]: %s", label_re, value[:60])
                return True
        logger.warning("未找到姓名字段: %s", label_re)
        return False

    async def _fill_step3_person_names(
        self, page, person: dict[str, Any], data: dict[str, Any] | None = None
    ) -> None:
        """与 s03 相同：有值才填；仅中文则不填英文；中英都有则三栏都填。"""
        name_cn, surname, given = resolve_nnc1_step3_names(person, data)
        if name_cn:
            await self._fill_step3_name_field(page, r"^中文姓名$|^中文名稱$", name_cn)
        if surname:
            await self._fill_step3_name_field(page, r"^英文姓氏$|^英文姓$", surname)
        if given:
            await self._fill_step3_name_field(page, r"^英文名字$|^英文名$", given)
        logger.info(
            "NNC1-3.1 姓名 cn=%s surname=%s given=%s",
            name_cn[:20],
            surname[:20],
            given[:20],
        )

    async def _wait_step3_shell_ready(self, page) -> None:
        """等待步骤3：类型/身分区块渲染完成。"""
        logger.info("等待 NNC1 步骤3 页面加载: %s", page.url[:120])
        await wait_spin_clear(page, timeout_ms=180000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass

        try:
            await page.wait_for_function(
                "() => {\n"
                f"                    if ({ICRIS_PAGE_IS_LOADING_JS}) return false;\n"
                """                    const body = document.body?.innerText || '';
                    if (!/步驟\\s*3|步骤\\s*3|輸入創辦成員|输入创办成员/.test(body)) {
                        return false;
                    }
                    let natural = false;
                    for (const r of document.querySelectorAll('input[type=radio]')) {
                        const ctx = (r.closest('.ant-radio-wrapper, label')?.innerText || '')
                            .replace(/\\s+/g, '');
                        if (!ctx.includes('自然人') || ctx.length > 35) continue;
                        const box = r.getBoundingClientRect();
                        if (box.width > 0 && box.height > 0) natural = true;
                    }
                    let founder = false;
                    for (const w of document.querySelectorAll('.ant-checkbox-wrapper, label')) {
                        const t = (w.innerText || '').replace(/\\s+/g, '');
                        if (t.length > 40) continue;
                        if (!t.includes('創辦成員') && !t.includes('创办成员')) continue;
                        const r = w.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) founder = true;
                    }
                    return natural && founder;
                }""",
                timeout=180000,
            )
            logger.info("NNC1 步骤3 身分区块已就绪")
        except Exception as e:
            await self._maybe_screenshot(page, "step3_wait_shell_fail")
            raise RuntimeError(f"步骤3 页面加载超时: {e}")

    async def _wait_step3_form_expanded(self, page) -> None:
        """勾选身分后等待姓名/股本等详情表单展开。"""
        logger.info("等待 NNC1 步骤3 人员详情表单")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const nameRe = /中文姓名|英文姓氏|中文名稱|英文名字/;
                    for (const el of document.querySelectorAll(
                        'label, .rowTitle, span, div, th, td'
                    )) {
                        const t = (el.innerText || '').trim();
                        if (!nameRe.test(t) || t.length > 50) continue;
                        const row = el.closest('tr,.ant-row,.ant-form-item,fieldset,div')
                            || el.parentElement;
                        const inp = row?.querySelector(
                            'textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio])'
                        );
                        if (!inp) continue;
                        const r = inp.getBoundingClientRect();
                        if (r.width > 30 && r.height > 10) return true;
                    }
                    for (const tb of document.querySelectorAll('table')) {
                        const head = (tb.innerText || '').slice(0, 300);
                        if (/認購的股本|认购的股本|建議向該成員|建议向该成员/.test(head)) {
                            return true;
                        }
                    }
                    return false;
                }""",
                timeout=120000,
            )
            logger.info("NNC1 步骤3 人员详情已展开")
        except Exception as e:
            await self._maybe_screenshot(page, "step3_wait_expand_fail")
            raise RuntimeError(f"步骤3 人员表单展开超时: {e}")
        await page.wait_for_timeout(400)

    def _capacity_row_js_helpers(self) -> str:
        """身分行定位与短标签分类的共用 JS 片段。"""
        return r"""
                const norm = s => (s || '').replace(/\s+/g, '').trim();
                const findCapacityRoot = () => {
                    for (const el of document.querySelectorAll(
                        'th, td, label, span, div, .rowTitle, legend'
                    )) {
                        const t = norm(el.innerText || '');
                        if (t !== '身分' && t !== '身份') continue;
                        const row = el.closest('tr')
                            || el.closest('.ant-row, .ant-form-item, fieldset')
                            || el.parentElement;
                        if (row) return row;
                    }
                    for (const el of document.querySelectorAll('tr, .ant-row, div, fieldset')) {
                        const t = norm(el.innerText || '');
                        if (t.length > 200) continue;
                        if (/創辦成員|创办成员/.test(t)
                            && /董事/.test(t)
                            && /公司秘書|公司秘书/.test(t)) {
                            return el;
                        }
                    }
                    return document.body;
                };
                const labelOf = (wrap) => {
                    const stripHelp = (s) => (s || '')
                        .replace(/[\u200b-\u200d\ufeff]/g, '')
                        .replace(/了解更多[\s\S]*$/i, '')
                        .replace(/Learn\s*more[\s\S]*$/i, '');
                    const parts = [];
                    for (const child of wrap.childNodes) {
                        if (child.nodeType === 3) {
                            parts.push(child.textContent || '');
                            continue;
                        }
                        if (child.nodeType !== 1) continue;
                        if (child.matches?.('.ant-checkbox, input')
                            || child.querySelector?.('input[type=checkbox]')) {
                            continue;
                        }
                        const tx = (child.innerText || '').trim();
                        if (/了解更多|Learn\s*more/i.test(tx)) {
                            // 同一节点里可能是「董事了解更多…」：只保留帮助文案前的短标签
                            const before = stripHelp(tx);
                            if (before) parts.push(before);
                            continue;
                        }
                        if (child.matches?.('.anticon, a, button, [role=tooltip]')) continue;
                        parts.push(tx);
                    }
                    let t = stripHelp(norm(parts.join('')));
                    if (t) return t;
                    const clone = wrap.cloneNode(true);
                    clone.querySelectorAll(
                        '.ant-checkbox, input, .anticon, a, button, [role=tooltip],'
                        + ' .ant-tooltip, .ant-popover'
                    ).forEach(n => n.remove());
                    t = stripHelp(norm(clone.innerText || ''));
                    if (t) return t;
                    return stripHelp(norm(wrap.innerText || '')).slice(0, 24);
                };
                const classify = (t) => {
                    // 短标签（已剥「了解更多」）；董事帮助文案含「公司秘書」不可再匹配全文
                    const clean = (t || '').replace(/[\u200b-\u200d\ufeff]/g, '');
                    if (clean === '公司秘書' || clean === '公司秘书') return 'secretary';
                    if (clean === '創辦成員' || clean === '创办成员') return 'founder';
                    if (clean === '董事') return 'director';
                    if (/^(公司秘書|公司秘书)/.test(clean) && clean.length <= 12) {
                        return 'secretary';
                    }
                    if (/^(創辦成員|创办成员)/.test(clean) && clean.length <= 12) {
                        return 'founder';
                    }
                    if (/^董事/.test(clean) && clean.length <= 8) return 'director';
                    return '';
                };
                const collectRoles = (root) => {
                    const wraps = [...root.querySelectorAll(
                        '.ant-checkbox-wrapper, label'
                    )].filter(w => w.querySelector('input[type=checkbox]'));
                    const byRole = { secretary: null, director: null, founder: null };
                    for (const w of wraps) {
                        const role = classify(labelOf(w));
                        if (role && !byRole[role]) byRole[role] = w;
                    }
                    return { wraps, byRole, labels: wraps.map(labelOf).slice(0, 10) };
                };
                const setChecked = (wrap, want) => {
                    if (!wrap) return !want;
                    const inp = wrap.querySelector('input[type=checkbox]');
                    if (!inp) return false;
                    if (!!inp.checked === want) return true;
                    wrap.click();
                    if (!!inp.checked === want) return true;
                    inp.click();
                    return !!inp.checked === want;
                };
                const readState = (byRole) => ({
                    secretary: !!(byRole.secretary?.querySelector('input')?.checked),
                    director: !!(byRole.director?.querySelector('input')?.checked),
                    founder: !!(byRole.founder?.querySelector('input')?.checked),
                });
        """

    async def _set_capacity_role(self, page, role: str, want: bool) -> dict:
        """在身分行内设置单个角色勾选状态（founder/director/secretary）。"""
        helpers = self._capacity_row_js_helpers()
        return await page.evaluate(
            f"""({{ role, want }}) => {{
                {helpers}
                const root = findCapacityRoot();
                const {{ byRole, labels }} = collectRoles(root);
                const wrap = byRole[role];
                if (!wrap) {{
                    return {{ ok: false, reason: 'missing_' + role, labels }};
                }}
                const ok = setChecked(wrap, want);
                return {{ ok, labels, state: readState(byRole) }};
            }}""",
            {"role": role, "want": want},
        )

    async def _click_capacity_role_playwright(
        self, page, texts: tuple[str, ...], *, want_checked: bool
    ) -> bool:
        """Playwright 在身分行内按短文案勾/取消。"""
        capacity = page.locator("tr, .ant-row, fieldset, div").filter(
            has_text=re.compile(r"身分|身份")
        ).filter(
            has_text=re.compile(r"創辦成員|创办成员|公司秘書|公司秘书")
        ).first
        if await capacity.count() == 0:
            return False
        for text in texts:
            loc = capacity.get_by_text(text, exact=True)
            if await loc.count() == 0:
                continue
            wrap = loc.first.locator(
                "xpath=ancestor::*[contains(@class,'ant-checkbox-wrapper')][1]"
            ).first
            target = wrap if await wrap.count() > 0 else loc.first
            await target.scroll_into_view_if_needed()
            cls = ""
            if await wrap.count() > 0:
                cls = await wrap.get_attribute("class") or ""
            is_checked = "checked" in cls
            if is_checked == want_checked:
                return True
            await target.click(force=True, timeout=8000)
            return True
        return False

    async def _read_capacity_state(self, page) -> dict:
        helpers = self._capacity_row_js_helpers()
        return await page.evaluate(
            f"""() => {{
                {helpers}
                const root = findCapacityRoot();
                const {{ byRole, labels }} = collectRoles(root);
                return {{ ...readState(byRole), labels }};
            }}"""
        )

    async def _check_founder_and_director_only(self, page) -> None:
        """阶段A身分：勾「创办成员+董事」，确保公司秘书未勾。

        创办成员勾选会触发表单展开/载入，必须等 spin 后再勾董事。
        """
        # 1) 创办成员
        r1 = await self._set_capacity_role(page, "founder", True)
        if not r1 or not r1.get("ok"):
            if not await self._click_capacity_role_playwright(
                page, ("創辦成員", "创办成员"), want_checked=True
            ):
                await self._maybe_screenshot(page, "step3_founder_director_check_fail")
                raise RuntimeError(
                    f"未能勾选创办成员: {(r1 or {}).get('reason')} labels={(r1 or {}).get('labels')}"
                )
        await wait_spin_clear(page, timeout_ms=60000)
        await page.wait_for_timeout(400)

        # 2) 董事（表单展开后身分行仍在）
        r2 = await self._set_capacity_role(page, "director", True)
        if not r2 or not r2.get("ok"):
            if not await self._click_capacity_role_playwright(
                page, ("董事",), want_checked=True
            ):
                await self._maybe_screenshot(page, "step3_founder_director_check_fail")
                raise RuntimeError(
                    f"未能勾选董事: {(r2 or {}).get('reason')} labels={(r2 or {}).get('labels')}"
                )
        await wait_spin_clear(page, timeout_ms=60000)
        await page.wait_for_timeout(300)

        # 3) 确保公司秘书未勾
        r3 = await self._set_capacity_role(page, "secretary", False)
        if r3 and not r3.get("ok") and r3.get("reason") != "missing_secretary":
            await self._click_capacity_role_playwright(
                page, ("公司秘書", "公司秘书"), want_checked=False
            )
        await wait_spin_clear(page, timeout_ms=30000)

        state = await self._read_capacity_state(page)
        if not state.get("founder") or not state.get("director"):
            await self._maybe_screenshot(page, "step3_founder_director_check_fail")
            raise RuntimeError(
                f"阶段A身分校验失败：创办成员={state.get('founder')} "
                f"董事={state.get('director')} labels={state.get('labels')}"
            )
        if state.get("secretary"):
            await self._maybe_screenshot(page, "step3_founder_director_check_fail")
            raise RuntimeError("阶段A身分校验失败：公司秘书不应勾选")
        logger.info("阶段A身分校验通过: 创办成员+董事, 公司秘书未勾")
        await page.wait_for_timeout(400)

    async def _ensure_step3_identity_checkboxes(self, page) -> None:
        """阶段A：勾选创办成员 + 董事（精确身分行，带重试）。"""
        last_err: Exception | None = None
        for _ in range(3):
            try:
                await self._check_founder_and_director_only(page)
                await wait_spin_clear(page, timeout_ms=60000)
                await page.wait_for_timeout(500)
                return
            except Exception as e:
                last_err = e
                logger.warning("阶段A身分勾选重试: %s", e)
                await wait_spin_clear(page, timeout_ms=30000)
                await page.wait_for_timeout(800)
        await self._maybe_screenshot(page, "step3_founder_director_check_fail")
        raise RuntimeError(f"未能勾选身分创办成员+董事: {last_err}")

    async def _select_natural_person(self, page) -> bool:
        """选择類型：自然人。"""
        for attempt in range(3):
            if await self._select_radio_by_patterns(page, ["自然人"]):
                return True
            await wait_spin_clear(page, timeout_ms=30000)
            await page.wait_for_timeout(800)
        loc = page.locator(".ant-radio-wrapper, label").filter(
            has_text=re.compile(r"自然人")
        )
        count = await loc.count()
        for i in range(count):
            item = loc.nth(i)
            txt = re.sub(r"\s+", "", (await item.inner_text() or ""))
            if len(txt) > 25 or "法人" in txt:
                continue
            try:
                await item.scroll_into_view_if_needed()
                await item.click(force=True, timeout=5000)
                logger.info("已选择自然人 (Playwright)")
                return True
            except Exception:
                pass
        return False

    async def _select_first_option_in_cell(self, page, cell_loc) -> bool:
        """单元格内下拉框选第一项（股份的類別）。"""
        if await cell_loc.count() == 0:
            return False
        sel = cell_loc.locator("select").first
        if await sel.count() > 0:
            opts = sel.locator("option")
            count = await opts.count()
            for i in range(count):
                val = (await opts.nth(i).get_attribute("value") or "").strip()
                text = (await opts.nth(i).inner_text() or "").strip()
                if val or text:
                    try:
                        await sel.select_option(index=i)
                        logger.info("已选下拉第一项: %s", text[:40])
                        await page.wait_for_timeout(300)
                        return True
                    except Exception:
                        pass
            return False
        ant = cell_loc.locator(".ant-select").first
        if await ant.count() > 0:
            trigger = ant.locator(".ant-select-selector").first
            await trigger.click(timeout=5000)
            opt = page.locator(".ant-select-item-option").first
            if await opt.count() > 0:
                label = (await opt.inner_text() or "").strip()
                await opt.click(timeout=5000)
                logger.info("已选 ant 下拉第一项: %s", label[:40])
                await page.wait_for_timeout(300)
                return True
            await page.keyboard.press("Escape")
        return False

    async def _fill_member_subscribed_capital(self, page, sc: dict[str, Any]) -> None:
        """步骤3：認購的股本 — 类别选第一项，總數/總款額填注册资本。"""
        await self._scroll_to_section(
            page,
            ["認購的股本", "认购的股本", "建議向該成員發行的股份數目"],
        )
        await page.wait_for_timeout(400)

        amount = str(sc.get("subscribed") or sc.get("paid_up") or "")
        shares = str(sc.get("total_shares") or amount)
        currency = str(sc.get("currency") or "HKD").strip() or "HKD"
        if not shares and not amount:
            logger.warning("认购股本数据为空，跳过")
            return

        table = page.locator("table").filter(
            has_text=re.compile(r"建議向該成員發行的股份數目|建议向该成员发行的股份数目")
        ).first
        if await table.count() == 0:
            table = page.locator("table").filter(
                has_text=re.compile(r"認購的股本|认购的股本|股份的類別")
            ).first
        if await table.count() == 0:
            logger.warning("未找到认购股本表")
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
            elif h in ("總數", "总数"):
                col_map["total"] = idx
            elif re.search(r"貨幣單位|货币单位", h):
                col_map["currency"] = idx
            elif re.search(r"總款額|总款额", h):
                col_map["amount"] = idx

        data_row = table.locator("tr").filter(
            has=page.locator(
                "td input:not([type=hidden]), td textarea, td select, td .ant-select"
            )
        ).first
        if await data_row.count() == 0:
            logger.warning("认购股本表无数据行")
            return

        filled: list[str] = []

        class_idx = col_map.get("class", 0)
        class_cell = data_row.locator("td").nth(class_idx)
        if await self._select_first_option_in_cell(page, class_cell):
            filled.append("class")

        total_idx = col_map.get("total", 1)
        total_cell = data_row.locator("td").nth(total_idx)
        total_inp = total_cell.locator(
            "input:not([type=hidden]):not([type=checkbox]), textarea"
        ).last
        if shares and await total_inp.count() > 0:
            if await self._fill_share_table_input(page, total_inp, shares):
                filled.append("total")

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
                    logger.debug("认购股本货币 select 失败: %s", e)
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

        amt_idx = col_map.get("amount", 3)
        amt_cell = data_row.locator("td").nth(amt_idx)
        amt_inp = amt_cell.locator(
            "input:not([type=hidden]):not([type=checkbox]), textarea"
        ).last
        if amount and await amt_inp.count() > 0:
            if await self._fill_share_table_input(page, amt_inp, amount):
                filled.append("amount")

        if filled:
            logger.info(
                "已填写认购股本: shares=%s amount=%s currency=%s detail=%s",
                shares,
                amount,
                currency,
                filled,
            )
        else:
            logger.warning("认购股本填写失败")

    def _normalize_id_type(self, raw: str, id_number: str = "") -> str:
        """证件类型：库里已有则尊重；空类型才弱兜底。"""
        return normalize_stored_id_type(raw, id_number)

    def _resolve_person_address(self, person: dict[str, Any], data: dict[str, Any]) -> dict[str, str]:
        return resolve_nnc1_person_address(person, data)

    def _resolve_person_id(self, person: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
        identity = dict(data.get("identity_proof") or {})
        applicant = dict(data.get("applicant") or {})
        id_number = str(
            person.get("id_number")
            or identity.get("id_number")
            or applicant.get("id_number")
            or ""
        ).strip()
        id_type = str(
            person.get("id_type")
            or identity.get("id_type")
            or applicant.get("id_type")
            or ""
        )
        return id_number, self._normalize_id_type(id_type, id_number)

    def _resolve_issuing_country(
        self, person: dict[str, Any], data: dict[str, Any]
    ) -> str:
        from src.materials.countries import normalize_issuing_iso

        identity = dict(data.get("identity_proof") or {})
        applicant = dict(data.get("applicant") or {})
        raw = str(
            person.get("issuing_country")
            or identity.get("issuing_country")
            or applicant.get("issuing_country")
            or ""
        )
        return normalize_issuing_iso(raw)

    async def _locate_address_block(
        self,
        page,
        heading_re: str,
        *,
        exclude_re: str = "",
        block_key: str = "addr",
    ):
        """定位地址区块根节点（含 radio + textarea），并打标 data-nnc1-addr-block。"""
        key = await page.evaluate(
            """({ headingRe, excludeRe, blockKey }) => {
                document.querySelectorAll('[data-nnc1-addr-block]').forEach(el => {
                    el.removeAttribute('data-nnc1-addr-block');
                });
                const re = new RegExp(headingRe, 'i');
                const ex = excludeRe ? new RegExp(excludeRe, 'i') : null;
                const candidates = [...document.querySelectorAll(
                    'legend,.rowTitle,label,span,div,th,h3,h4,p,td'
                )];
                for (const el of candidates) {
                    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 120) continue;
                    if (!re.test(t)) continue;
                    if (ex && ex.test(t)) continue;
                    let node = el;
                    for (let d = 0; d < 25 && node; d++) {
                        const radios = node.querySelectorAll('input[type=radio]');
                        const fields = [...node.querySelectorAll(
                            'textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio])'
                        )].filter(inp => {
                            const r = inp.getBoundingClientRect();
                            return r.width > 20 && r.height > 10;
                        });
                        if (radios.length >= 1 && fields.length >= 2) {
                            node.setAttribute('data-nnc1-addr-block', blockKey);
                            node.scrollIntoView({ block: 'center' });
                            return blockKey;
                        }
                        node = node.parentElement;
                    }
                }
                return '';
            }""",
            {
                "headingRe": heading_re,
                "excludeRe": exclude_re,
                "blockKey": block_key,
            },
        )
        if not key:
            return None
        loc = page.locator(f"[data-nnc1-addr-block='{block_key}']").first
        if await loc.count() > 0:
            return loc
        return None

    async def _click_address_radio_in_block(self, block, *, want_hk: bool) -> bool:
        """块内点「香港地址」或「非香港地址」。点香港时排除含「非香港」的项。"""
        label = "香港地址" if want_hk else "非香港地址"
        candidates = block.locator("label, .ant-radio-wrapper, span.ant-radio + span")
        count = await candidates.count()
        for i in range(count):
            item = candidates.nth(i)
            if not await item.is_visible():
                continue
            text = re.sub(r"\s+", "", (await item.inner_text() or ""))
            if len(text) > 40:
                continue
            ok = (
                nnc1_address_radio_is_hk(text)
                if want_hk
                else nnc1_address_radio_is_non_hk(text)
            )
            if not ok:
                continue
            try:
                await item.scroll_into_view_if_needed()
                await item.click(timeout=5000)
                logger.info("已选%s: %s", label, text[:30])
                await block.page.wait_for_timeout(300)
                return True
            except Exception:
                inp = item.locator("input[type=radio]").first
                if await inp.count() > 0:
                    await inp.check(force=True)
                    logger.info("已 force 选%s: %s", label, text[:30])
                    return True
        hit = await block.evaluate(
            """(root, wantHk) => {
                for (const w of root.querySelectorAll('.ant-radio-wrapper, label')) {
                    const t = (w.innerText || '').replace(/\\s+/g, '');
                    if (t.length > 40) continue;
                    const nonHk = t.includes('非香港') || t.includes('非本地');
                    if (wantHk) {
                        if (nonHk) continue;
                        if (!t.includes('香港地址') && !t.includes('本港地址')) continue;
                    } else if (!nonHk) {
                        continue;
                    }
                    w.click();
                    const inp = w.querySelector('input[type=radio]');
                    if (inp) {
                        inp.checked = true;
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    return t.slice(0, 30);
                }
                return '';
            }""",
            want_hk,
        )
        if hit:
            logger.info("已选%s (JS): %s", label, hit)
            return True
        return False

    async def _select_hk_in_block(self, block) -> bool:
        return await self._click_address_radio_in_block(block, want_hk=True)

    async def _select_non_hk_in_block(self, block) -> bool:
        return await self._click_address_radio_in_block(block, want_hk=False)

    async def _fill_textarea_in_block(
        self, block, label_re: str, value: str
    ) -> bool:
        if not value:
            return False
        pat = re.compile(label_re, re.I)
        labels = block.locator("label, th, .rowTitle, span, div, td")
        count = await labels.count()
        for i in range(count):
            lab = labels.nth(i)
            if not await lab.is_visible():
                continue
            txt = ((await lab.inner_text()) or "").strip()
            if len(txt) > 90 or not pat.search(txt):
                continue
            inp = lab.locator(
                "xpath=ancestor-or-self::*[1]//textarea[1] | following::textarea[1] | "
                "ancestor-or-self::*[1]//input[not(@type='hidden') and not(@type='checkbox')"
                " and not(@type='radio')][1] | "
                "following::input[not(@type='hidden') and not(@type='checkbox')"
                " and not(@type='radio')][1]"
            ).first
            if await inp.count() > 0 and await inp.is_visible():
                await inp.scroll_into_view_if_needed()
                await inp.fill(value)
                logger.info("已填写地址 [%s]: %s", label_re, value[:60])
                return True
        return False

    async def _fill_address_fields_by_order(
        self, block, addr: dict[str, str]
    ) -> int:
        """兜底：只填街道(第3项)、区省市(第4项)，跳过室/楼/座与大廈。"""
        textareas = block.locator("textarea")
        n = await textareas.count()
        targets = [
            (2, addr.get("street", "")),
            (3, addr.get("region", "")),
        ]
        filled = 0
        for idx, val in targets:
            if not val or idx >= n:
                continue
            ta = textareas.nth(idx)
            if not await ta.is_visible():
                continue
            cur = (await ta.input_value() or "").strip()
            if cur and cur == val:
                filled += 1
                continue
            if not cur:
                await ta.scroll_into_view_if_needed()
                await ta.fill(val)
                logger.info("已填写地址(顺序 #%d): %s", idx + 1, val[:60])
                filled += 1
        return filled

    async def _count_country_region_options(self, block) -> int:
        n = await block.evaluate(
            """(root) => {
                const placeholder = /請選擇|请选择|^Select$|^--+/i;
                const isCountry = (t) => {
                    const s = (t || '').replace(/\\s+/g, '');
                    return /國家\\/?地區|国家\\/?地区|國家|国家/.test(s) && s.length <= 40;
                };
                let nativeN = 0;
                let antN = 0;
                let sel = null;
                for (const el of root.querySelectorAll(
                    'label, th, .rowTitle, span, div, td'
                )) {
                    const t = (el.innerText || '').trim();
                    if (!isCountry(t)) continue;
                    const row = el.closest('tr,.ant-row,.ant-form-item,fieldset,div')
                        || el.parentElement;
                    sel = row?.querySelector('select') || sel;
                }
                if (!sel) sel = root.querySelector('select');
                if (sel) {
                    for (const o of sel.options) {
                        const tx = (o.textContent || '').trim();
                        if (!tx || placeholder.test(tx)) continue;
                        if (/中国|中國|China|中華人民共和國/.test(tx)) return 999;
                        nativeN += 1;
                    }
                }
                for (const item of document.querySelectorAll(
                    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) '
                    + '.ant-select-item-option'
                )) {
                    const ot = (item.innerText || '').trim();
                    if (!ot || placeholder.test(ot)) continue;
                    if (/中国|中國|China|中華人民共和國/.test(ot)) return 999;
                    antN += 1;
                }
                return Math.max(nativeN, antN);
            }"""
        )
        return int(n or 0)

    async def _open_country_region_dropdown(self, block) -> bool:
        return bool(
            await block.evaluate(
                """(root) => {
                    const isCountry = (t) => {
                        const s = (t || '').replace(/\\s+/g, '');
                        return /國家|国家/.test(s) && s.length <= 40;
                    };
                    for (const el of root.querySelectorAll(
                        'label, th, .rowTitle, span, div, td'
                    )) {
                        const t = (el.innerText || '').trim();
                        if (!isCountry(t)) continue;
                        const row = el.closest('tr,.ant-row,.ant-form-item,fieldset,div')
                            || el.parentElement;
                        const ant = row?.querySelector('.ant-select');
                        if (!ant || ant.classList.contains('ant-select-disabled')) continue;
                        const r = ant.getBoundingClientRect();
                        if (r.width <= 0) continue;
                        if (!ant.classList.contains('ant-select-open')) {
                            (ant.querySelector('.ant-select-selector') || ant).click();
                        }
                        return true;
                    }
                    return false;
                }"""
            )
        )

    async def _wait_country_region_options(self, page, block) -> bool:
        """等地址区块「國家／地區」下拉出现真实国家项（不只請選擇）。"""
        await wait_spin_clear(page, timeout_ms=30000)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            spinning = await is_page_loading(page)
            if spinning:
                await wait_spin_clear(page, timeout_ms=10000)
            n = await self._count_country_region_options(block)
            if n >= 8:
                logger.info("國家／地區 选项已加载 count=%s", n)
                return True
            await self._open_country_region_dropdown(block)
            await page.wait_for_timeout(400)
            n = await self._count_country_region_options(block)
            if n >= 8:
                logger.info("國家／地區 选项已加载 count=%s", n)
                return True
            await page.wait_for_timeout(500)
        n = await self._count_country_region_options(block)
        logger.warning("國家／地區 选项不足 count=%s", n)
        return n >= 8

    async def _pick_country_in_block(
        self, page, block, option_names: list[str]
    ) -> bool:
        opts = [n for n in option_names if (n or "").strip()]
        if not opts:
            return False
        hit = await block.evaluate(
            """(root, opts) => {
                const isCountry = (t) => /國家|国家/.test((t || '').replace(/\\s+/g, ''));
                for (const el of root.querySelectorAll(
                    'label, th, .rowTitle, span, div, td'
                )) {
                    const t = (el.innerText || '').trim();
                    if (!isCountry(t) || t.length > 40) continue;
                    const row = el.closest('tr,.ant-row,.ant-form-item,fieldset,div') || el.parentElement;
                    const sel = row?.querySelector('select');
                    if (sel) {
                        for (const o of sel.options) {
                            const tx = (o.textContent || '') + ' ' + (o.value || '');
                            if (opts.some(x => tx.includes(x))) {
                                sel.value = o.value;
                                sel.dispatchEvent(new Event('input', { bubbles: true }));
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                return (o.textContent || '').trim().slice(0, 20);
                            }
                        }
                    }
                    const ant = row?.querySelector('.ant-select');
                    if (ant) {
                        (ant.querySelector('.ant-select-selector') || ant).click();
                        return '__ant__';
                    }
                }
                const sel = root.querySelector('select');
                if (sel) {
                    for (const o of sel.options) {
                        const tx = (o.textContent || '') + ' ' + (o.value || '');
                        if (opts.some(x => tx.includes(x))) {
                            sel.value = o.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return (o.textContent || '').trim().slice(0, 20);
                        }
                    }
                }
                return '';
            }""",
            opts,
        )
        if hit == "__ant__":
            try:
                await page.wait_for_selector(
                    ".ant-select-item-option",
                    timeout=15000,
                )
            except Exception:
                pass
            for opt_text in opts:
                opt = page.locator(".ant-select-item-option").filter(
                    has_text=re.compile(re.escape(opt_text), re.I)
                ).first
                if await opt.count() > 0:
                    await opt.click(timeout=5000)
                    logger.info("已选国家/地区 (ant): %s", opt_text)
                    await page.wait_for_timeout(400)
                    return True
            await page.keyboard.press("Escape")
            return False
        if hit:
            logger.info("已选国家/地区: %s", hit)
            return True
        return False

    async def _select_country_in_block(self, page, block, iso: str = "") -> bool:
        """选國家／地區：按住址 ISO 对应 ICRIS 原文。香港不硬选中国。"""
        from src.materials.countries import icris_country_select_candidates

        code = (iso or "").strip().upper()
        if code == "HKG":
            names = icris_country_select_candidates("HKG")
            logger.info("香港住址不默认中国，尝试选项 %s", names[:4])
        else:
            names = icris_country_select_candidates(code or "CHN")

        async def _try_pick() -> bool:
            await self._wait_country_region_options(page, block)
            return await self._pick_country_in_block(page, block, names)

        if await _try_pick():
            return True
        if code == "HKG":
            logger.warning("國家／地區 香港不在下拉中，跳过（不选中国）")
            return False
        logger.warning("國家／地區 选 %s 失败，再点非香港以重新加载", code or "CHN")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(400)
        await self._select_non_hk_in_block(block)
        await page.wait_for_timeout(800)
        if await _try_pick():
            return True
        logger.warning("國家／地區 仍失败，关闭后重开下拉")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(400)
        return await _try_pick()

    async def _fill_nnc1_address_section(
        self,
        page,
        heading_re: str,
        addr: dict[str, str],
        *,
        exclude_re: str = "",
        block_key: str = "addr",
        scroll_keywords: list[str] | None = None,
    ) -> bool:
        """在指定标题的地址区块：按入库 address_is_hk 点香港/非香港，再填原栏位。"""
        is_hk = str(addr.get("address_is_hk") or "") == "1"
        if scroll_keywords:
            await self._scroll_to_section(page, scroll_keywords)
        await page.wait_for_timeout(300)
        block = await self._locate_address_block(
            page,
            heading_re,
            exclude_re=exclude_re,
            block_key=block_key,
        )
        if block is None:
            logger.warning("未找到地址区块: %s", heading_re)
            return False
        if is_hk:
            if not await self._select_hk_in_block(block):
                logger.warning("地址区块未选香港地址: %s", heading_re)
        elif not await self._select_non_hk_in_block(block):
            logger.warning("地址区块未选非香港: %s", heading_re)
        await wait_spin_clear(page, timeout_ms=30000)
        filled = 0
        if is_hk:
            if await self._fill_textarea_in_block(
                block, r"室.*樓|室.*楼|室／樓|Flat.*Floor", addr.get("flat", "")
            ):
                filled += 1
            if await self._fill_textarea_in_block(
                block, r"大廈|大厦|Building", addr.get("building", "")
            ):
                filled += 1
            if await self._fill_textarea_in_block(
                block, r"街道.*屋苑|街道.*地段|街道.*村", addr.get("street", "")
            ):
                filled += 1
            region = addr.get("region", "")
            if region:
                if await self._select_district_in_block(page, block, region):
                    filled += 1
                else:
                    logger.warning(
                        "地址区块 [%s] 香港地址「区」未选中 region=%s",
                        block_key,
                        region[:40],
                    )
            else:
                logger.warning(
                    "地址区块 [%s] 缺已入库香港区/邮递区号",
                    block_key,
                )
            logger.info(
                "地址区块 [%s] 香港地址填写完成 filled=%s flat=%s street=%s region=%s",
                block_key,
                filled,
                (addr.get("flat") or "")[:40],
                (addr.get("street") or "")[:50],
                (addr.get("region") or "")[:40],
            )
            return filled > 0

        await self._wait_country_region_options(page, block)
        if await self._fill_textarea_in_block(
            block, r"街道.*屋苑|街道.*地段|街道.*村", addr.get("street", "")
        ):
            filled += 1
        if await self._fill_textarea_in_block(
            block, r"區.*市.*省|郵遞區號|州", addr.get("region", "")
        ):
            filled += 1
        order_filled = await self._fill_address_fields_by_order(block, addr)
        filled += order_filled
        iso = str(addr.get("country") or "").strip()
        if not iso:
            logger.warning("地址区块 [%s] 缺 address_country，不现场猜国家", block_key)
        elif await self._select_country_in_block(page, block, iso):
            filled += 1
        elif iso == "HKG":
            logger.info("地址区块 [%s] 香港住址不选中國", block_key)
        else:
            raise Nnc1CountryDropdownEmpty(
                f"地址区块 [{block_key}] 未能选择國家／地區={iso}"
            )
        logger.info(
            "地址区块 [%s] 填写完成 filled=%s street=%s region=%s",
            block_key,
            filled,
            (addr.get("street") or "")[:50],
            (addr.get("region") or "")[:40],
        )
        return filled > 0

    async def _select_district_in_block(self, page, block, region: str) -> bool:
        """香港地址「区」下拉：同 s03 本地地址邮区，不点「地区」。"""
        keys = nnc1_district_select_keys(region)
        if not keys:
            return False
        located = await block.evaluate(
            """(root) => {
                root.querySelectorAll('[data-nnc1-hk-district]').forEach(el => {
                    el.removeAttribute('data-nnc1-hk-district');
                });
                const inside = (node) => node && root.contains(node);
                const isDist = (t) => {
                    const raw = (t || '').trim();
                    const compact = raw.replace(/\\s+/g, '');
                    if (/地區|地区|國家|国家/.test(compact)) return false;
                    const first = raw.split('\\n')[0]
                        .replace(/\\s+/g, '')
                        .replace(/[*:：]/g, '');
                    return /^(區|区|District)$/i.test(first);
                };
                const optionInfo = (sel) => {
                    const opts = [...sel.options].map(o => ({
                        t: (o.textContent || '').trim(),
                        v: (o.value || '').trim(),
                    })).filter(o => o.t && !/請選擇|请选择|^Select$|^--+$/i.test(o.t));
                    return { n: opts.length, sample: opts.slice(0, 12) };
                };
                const findSelect = (labelEl) => {
                    const own = labelEl.querySelector('select');
                    if (own) return own;
                    const forId = labelEl.getAttribute('for');
                    if (forId) {
                        const byFor = root.querySelector(
                            '#' + CSS.escape(forId)
                        );
                        if (byFor && byFor.tagName === 'SELECT') return byFor;
                    }
                    let p = labelEl.parentElement;
                    for (let d = 0; d < 6 && p && inside(p); d++) {
                        const sels = [...p.querySelectorAll('select')];
                        if (sels.length === 1) return sels[0];
                        if (sels.length > 1) {
                            const after = sels.find(s =>
                                (labelEl.compareDocumentPosition(s)
                                    & Node.DOCUMENT_POSITION_FOLLOWING)
                            );
                            if (after) return after;
                        }
                        const ant = p.querySelector('.ant-select');
                        if (ant) return ant;
                        p = p.parentElement;
                    }
                    return null;
                };
                for (const el of root.querySelectorAll(
                    'label, th, .rowTitle, span, div, td'
                )) {
                    const t = (el.innerText || '').trim();
                    if (!isDist(t)) continue;
                    const found = findSelect(el);
                    if (!found) continue;
                    if (found.classList && found.classList.contains('ant-select')) {
                        found.setAttribute('data-nnc1-hk-district', 'ant');
                        return { mode: 'ant' };
                    }
                    found.setAttribute('data-nnc1-hk-district', 'native');
                    const info = optionInfo(found);
                    return { mode: 'native', n: info.n, sample: info.sample };
                }
                return { mode: 'none' };
            }"""
        )
        mode = (located or {}).get("mode") or "none"
        logger.info(
            "香港地址「区」定位 mode=%s n=%s region=%s sample=%s",
            mode,
            (located or {}).get("n"),
            region[:40],
            (located or {}).get("sample"),
        )
        if mode == "native":
            sel = block.locator("select[data-nnc1-hk-district='native']").first
            deadline = time.monotonic() + 20
            last_n = int((located or {}).get("n") or 0)
            last_sample = (located or {}).get("sample") or []
            while time.monotonic() < deadline:
                info = await sel.evaluate(
                    """(el) => {
                        const opts = [...el.options].map(o => ({
                            t: (o.textContent || '').trim(),
                            v: (o.value || '').trim(),
                        })).filter(o =>
                            o.t && !/請選擇|请选择|^Select$|^--+$/i.test(o.t)
                        );
                        return { n: opts.length, sample: opts.slice(0, 12) };
                    }"""
                )
                last_n = int(info.get("n") or 0)
                last_sample = info.get("sample") or []
                if last_n >= 8:
                    break
                try:
                    await sel.click(timeout=1000)
                except Exception:
                    pass
                await page.wait_for_timeout(400)
            picked = await sel.evaluate(
                """(el, keys) => {
                    const fold = s => (s || '').replace(/\\s+/g, '').replace(/围/g, '圍');
                    const folded = keys.map(fold).filter(Boolean);
                    const lower = keys.map(k => (k || '').toLowerCase());
                    const opt = [...el.options].find(o => {
                        const t = (o.textContent || '').trim();
                        const v = (o.value || '').trim();
                        const ft = fold(t);
                        return folded.some(k => k && (ft === k || ft.includes(k)))
                            || lower.some(k => k && v.toLowerCase() === k);
                    });
                    if (!opt) return '';
                    el.value = opt.value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return (opt.textContent || '').trim().slice(0, 40);
                }""",
                keys,
            )
            if not picked:
                for key in keys:
                    try:
                        await sel.select_option(label=key, timeout=800)
                        picked = key
                        break
                    except Exception:
                        try:
                            await sel.select_option(value=key, timeout=500)
                            picked = key
                            break
                        except Exception:
                            pass
            if picked:
                logger.info("已选区/邮区: %s", picked)
                await page.wait_for_timeout(300)
                return True
            logger.warning(
                "香港地址「区」原生下拉未匹配 region=%s n=%s sample=%s keys=%s",
                region[:40],
                last_n,
                last_sample,
                keys[:8],
            )
            return False

        if mode != "ant":
            logger.warning("未找到香港地址「区」下拉 region=%s located=%s", region[:40], located)
            return False

        ant = block.locator("[data-nnc1-hk-district='ant']").first
        try:
            trigger = ant.locator(".ant-select-selector").first
            if await trigger.count() > 0:
                await trigger.click(timeout=3000)
            else:
                await ant.click(timeout=3000)
        except Exception:
            pass
        opt_re = re.compile("|".join(re.escape(k) for k in keys[:6] if k), re.I)
        for attempt in range(3):
            try:
                dd = page.locator(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden)"
                ).last
                try:
                    await dd.wait_for(state="visible", timeout=8000)
                except Exception:
                    pass
                search = page.locator(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden) input"
                ).last
                if await search.count() > 0 and await search.is_visible():
                    await search.fill("")
                    await search.type(region, delay=35)
                    await page.wait_for_timeout(400)
                opt = page.locator(
                    ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
                    ".ant-select-item-option"
                ).filter(has_text=opt_re).first
                if await opt.count() > 0:
                    await opt.click(force=True, timeout=3000)
                    logger.info("已选区/邮区 (ant): %s", region)
                    await page.wait_for_timeout(400)
                    return True
            except Exception as exc:
                logger.debug("区下拉选项点击失败 (尝试 %d): %s", attempt + 1, exc)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(200)
        logger.warning("香港地址「区」下拉未选中 region=%s", region[:40])
        return False

    async def _click_copy_founder_address(self, page) -> bool:
        """通常住址：点该区块内「複製創辦成員的地址」（不要点通讯地址上同一文案）。"""
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await self._scroll_to_section(
            page, ["通常住址（適用於董事）", "通常住址", "通常住址將不會供公眾查閱"]
        )
        try:
            clicked = await page.evaluate(
                """() => {
                    const btnPat = /複製創辦成員的地址|复制创办成员的地址/;
                    const allBtns = [...document.querySelectorAll(
                        'button, a, input[type=button], [role=button], .btn, .ant-btn'
                    )].filter((el) => {
                        const t = (el.innerText || el.value || '').replace(/\\s+/g, '');
                        return btnPat.test(t);
                    });
                    let heading = null;
                    const headingSel = 'legend,h3,h4,th,.rowTitle,label,span,div,p,td';
                    for (const el of document.querySelectorAll(headingSel)) {
                        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!t || t.length > 40) continue;
                        if (/通常住址（適用於董事）|通常住址\\(適用於董事\\)|通常住址.*適用於董事/.test(t)) {
                            heading = el;
                            break;
                        }
                    }
                    if (!heading) {
                        for (const el of document.querySelectorAll(headingSel)) {
                            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (!t || t.length > 24) continue;
                            if (/^[註注]/.test(t)) continue;
                            if (t === '通常住址' || t.startsWith('通常住址（') || t.startsWith('通常住址(')) {
                                heading = el;
                                break;
                            }
                        }
                    }
                    let target = null;
                    if (heading) {
                        heading.scrollIntoView({ block: 'center' });
                        const hy = heading.getBoundingClientRect().top + window.scrollY;
                        const below = allBtns
                            .map((el) => ({
                                el,
                                y: el.getBoundingClientRect().top + window.scrollY,
                            }))
                            .filter((x) => x.y >= hy - 8)
                            .sort((a, b) => a.y - b.y);
                        target = below[0] && below[0].el;
                    }
                    if (!target && allBtns.length) {
                        target = allBtns[allBtns.length - 1];
                    }
                    if (!target) return '';
                    target.scrollIntoView({ block: 'center' });
                    target.click();
                    return (target.innerText || target.value || '')
                        .replace(/\\s+/g, '')
                        .slice(0, 40);
                }"""
            )
            if clicked:
                logger.info("已点击通常住址「複製創辦成員的地址」: %s", clicked)
                await page.wait_for_timeout(1000)
                return True
        except Exception as exc:
            logger.warning("点击通常住址复制按钮失败: %s", exc)
        logger.warning("未找到通常住址「複製創辦成員的地址」按钮")
        return False

    _NNC1_ID_NONE = "無"
    _NNC1_FILL_LABELED_JS = """({ labelRe, main, check, take }) => {
        const setNative = (el, v) => {
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            el.focus();
            setter.call(el, v);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        const vis = (el) => {
            if (!el || el.disabled) return false;
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            if (['hidden','radio','checkbox','file','submit','button'].includes(t))
                return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const textInputs = (root) => [...(root || document).querySelectorAll(
            'input:not([disabled])'
        )].filter(vis);
        const re = new RegExp(labelRe);
        const skip = /請輸入|请输入|如沒有|如没有|部分號碼|部分号码/;
        const markers = [...document.querySelectorAll(
            'label, span, div, td, th, p, li'
        )];
        const candidates = [];
        for (const el of markers) {
            const t = (el.innerText || '').replace(/\\s+/g, '');
            if (!t || t.length > 22) continue;
            if (skip.test(t)) continue;
            if (!re.test(t)) continue;
            candidates.push({ el, len: t.length });
        }
        candidates.sort((a, b) => a.len - b.len);
        const nTake = Math.max(1, Number(take) || 1);
        let pair = null;
        for (const { el } of candidates) {
            const lr = el.getBoundingClientRect();
            const nearby = textInputs(document.body).filter((inp) => {
                const rel = el.compareDocumentPosition(inp);
                const following = (rel & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
                const contained = (rel & Node.DOCUMENT_POSITION_CONTAINED_BY) !== 0;
                if (!following && !contained) return false;
                const r = inp.getBoundingClientRect();
                const midL = (lr.top + lr.bottom) / 2;
                const midI = (r.top + r.bottom) / 2;
                return Math.abs(midI - midL) < 48
                    || (r.top >= lr.top - 16 && r.top <= lr.bottom + 130);
            });
            if (nearby.length >= 1) {
                pair = nearby.slice(0, nTake);
                break;
            }
        }
        if (!pair || !pair.length) return { ok: false, value: '' };
        setNative(pair[0], main);
        if (check && pair[1]) setNative(pair[1], check);
        const ok = pair[0].value === main && (
            !check || !pair[1] || pair[1].value === check
        );
        return { ok, value: pair[0].value };
    }"""

    async def _fill_nnc1_labeled_inputs(
        self,
        page,
        *,
        label_re: str,
        main: str,
        check: str = "",
        max_inputs: int = 4,
        label_texts: list[str] | None = None,
    ) -> bool:
        """短标题同行输入框：Playwright fill 优先，JS 兜底。"""
        main = (main or "").strip()
        check = (check or "").strip()
        if not main:
            return False
        texts = [t for t in (label_texts or []) if t]
        take = 2 if check else 1
        if texts and await self._fill_nnc1_by_playwright(
            page, texts, main, check, take
        ):
            return True
        arg = {
            "labelRe": label_re,
            "main": main,
            "check": check,
            "take": take,
        }
        last: Any = None
        for frame in page.frames:
            try:
                last = await frame.evaluate(self._NNC1_FILL_LABELED_JS, arg)
            except Exception:
                continue
            if isinstance(last, dict) and last.get("ok"):
                return True
        return bool(isinstance(last, dict) and last.get("ok"))

    async def _fill_nnc1_by_playwright(
        self,
        page,
        texts: list[str],
        main: str,
        check: str,
        take: int,
    ) -> bool:
        """用可见文案定位标签，再填同一行右侧的 input（避开祖先 contains）。"""
        for frame in page.frames:
            for text in texts:
                loc = frame.get_by_text(text, exact=True)
                try:
                    n = await loc.count()
                except Exception:
                    continue
                if n < 1:
                    loc = frame.get_by_text(re.compile(rf"^{re.escape(text)}\s*\*?$"))
                    try:
                        n = await loc.count()
                    except Exception:
                        n = 0
                if n < 1:
                    continue
                label = loc.first
                try:
                    await label.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                try:
                    lb = await label.bounding_box()
                except Exception:
                    lb = None
                if not lb:
                    continue
                inputs = frame.locator(
                    "input:visible:not([type=hidden]):not([type=radio])"
                    ":not([type=checkbox]):not([type=file]):not([type=button])"
                    ":not([type=submit])"
                )
                try:
                    icount = await inputs.count()
                except Exception:
                    continue
                hits: list[int] = []
                for i in range(icount):
                    try:
                        box = await inputs.nth(i).bounding_box()
                    except Exception:
                        box = None
                    if not box:
                        continue
                    mid_l = lb["y"] + lb["height"] / 2
                    mid_i = box["y"] + box["height"] / 2
                    same_row = abs(mid_i - mid_l) < 52
                    just_under = box["y"] >= lb["y"] - 16 and box["y"] <= lb["y"] + lb["height"] + 130
                    if not same_row and not just_under:
                        continue
                    if box["x"] + box["width"] < lb["x"] - 12:
                        continue
                    hits.append(i)
                if not hits:
                    continue
                first = inputs.nth(hits[0])
                try:
                    await first.click(timeout=5000)
                    await first.fill(main)
                    if check and take >= 2 and len(hits) >= 2:
                        second = inputs.nth(hits[1])
                        await second.click(timeout=5000)
                        await second.fill(check)
                    elif check and len(hits) < 2:
                        await first.fill(f"{main}({check})")
                    val = (await first.input_value()).strip()
                    if val == main or (check and val.replace("（", "(").replace("）", ")") in (
                        f"{main}({check})",
                        main,
                    )):
                        return True
                except Exception:
                    logger.debug("NNC1 Playwright 身分栏填写失败 [%s]", text, exc_info=True)
                    continue
        return False

    async def _log_nnc1_identity_markers(self, page) -> None:
        try:
            info = await page.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll('*')) {
                        const t = (el.innerText || '').replace(/\\s+/g, '').trim();
                        if (!t || t.length > 28) continue;
                        if (!/身分|身份|護照|护照/.test(t)) continue;
                        const r = el.getBoundingClientRect();
                        out.push({
                            tag: el.tagName,
                            t,
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                        });
                        if (out.length >= 40) break;
                    }
                    return out;
                }"""
            )
            logger.warning("NNC1 身分栏 DOM 候选: %s", info)
            ctrls = await page.evaluate(
                """() => {
                    const compact = (s) => (s || '').replace(/\\s+/g, '').trim();
                    const out = [];
                    for (const div of document.querySelectorAll('div')) {
                        const t = compact(div.innerText || '');
                        if (t !== '完整香港身分证号码()' && t !== '完整护照号码'
                            && t !== '完整香港身分證號碼()' && t !== '完整護照號碼')
                            continue;
                        const nodes = [...div.querySelectorAll('input,textarea,select')];
                        out.push({
                            t,
                            w: Math.round(div.getBoundingClientRect().width),
                            n: nodes.length,
                            ctrls: nodes.slice(0, 8).map((el) => ({
                                tag: el.tagName,
                                type: el.getAttribute('type') || '',
                                name: el.name || '',
                                id: el.id || '',
                                dis: !!el.disabled,
                                w: Math.round(el.getBoundingClientRect().width),
                                h: Math.round(el.getBoundingClientRect().height),
                            })),
                        });
                        if (out.length >= 6) break;
                    }
                    return out;
                }"""
            )
            logger.warning("NNC1 身分栏控件: %s", ctrls)
        except Exception:
            logger.debug("NNC1 身分栏 DOM 候选失败", exc_info=True)

    async def _log_nnc1_identity_values(self, page) -> None:
        try:
            vals = await page.evaluate(
                """() => {
                    const compact = (s) => (s || '').replace(/\\s+/g, '').trim();
                    const boxed = (root) => [...root.querySelectorAll('input, textarea')].filter((el) => {
                        if (!el || el.disabled) return false;
                        const t = (el.getAttribute('type') || 'text').toLowerCase();
                        if (['hidden','radio','checkbox','file','submit','button'].includes(t))
                            return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 2 && r.height > 2;
                    });
                    const grab = (wants) => {
                        let best = null;
                        let area = 1e15;
                        for (const div of document.querySelectorAll('div')) {
                            const t = compact(div.innerText || '');
                            if (!wants.some(w => t === w || t === w + '()')) continue;
                            const r = div.getBoundingClientRect();
                            const a = r.width * r.height;
                            if (a < area) { area = a; best = div; }
                        }
                        return best ? boxed(best).map(el => el.value) : [];
                    };
                    return {
                        hkid: grab(['完整香港身分证号码','完整香港身分證號碼','完整香港身分証號碼']),
                        ppt: grab(['完整护照号码','完整護照號碼']),
                    };
                }"""
            )
            logger.info(
                "NNC1-3.1 可见栏值 hkid=%s passport=%s",
                (vals or {}).get("hkid"),
                (vals or {}).get("ppt"),
            )
        except Exception:
            logger.debug("NNC1 身分栏取值失败", exc_info=True)

    async def _fill_hkid_number(self, page, id_number: str) -> bool:
        """完整香港身分证号码：只认该 LABEL 容器。实际号填两框；無只写第一框。"""
        raw = (id_number or "").strip()
        if not raw:
            return False
        labels = [
            "完整香港身分证号码",
            "完整香港身分證號碼",
            "完整香港身分証號碼",
            "完整香港身份证号码",
            "完整香港身份證號碼",
            "完整香港身份証號碼",
        ]
        if raw == self._NNC1_ID_NONE:
            ok = await self._fill_input_under_exact_label(page, labels, raw)
        else:
            main, check = split_hkid_number(raw)
            if not main:
                return False
            ok = await self._fill_input_under_exact_label(
                page, labels, main, check=check
            )
        if ok:
            logger.info("NNC1 港证已填: %s", raw[:16])
            return True
        logger.warning("NNC1 港证未写入")
        return False

    async def _fill_nnc1_passport_number(self, page, value: str) -> bool:
        """完整护照号码：只认 LABEL 精确文案，填其所在小容器里第一个输入框。"""
        value = (value or "").strip()
        if not value:
            return False
        labels = ["完整护照号码", "完整護照號碼"]
        ok = await self._fill_input_under_exact_label(page, labels, value)
        if ok:
            logger.info("NNC1 护照已填: %s", value[:16])
            return True
        logger.warning("NNC1 护照未写入")
        return False

    async def _fill_by_accessible_label(
        self, page, labels: list[str], value: str, *, check: str = ""
    ) -> bool:
        """用 label 无障碍名称定位，避免整页扫框。"""
        check = (check or "").strip()
        for frame in page.frames:
            for text in labels:
                loc = frame.get_by_label(text, exact=True)
                try:
                    n = await loc.count()
                except Exception:
                    continue
                if n < 1:
                    continue
                visible: list[int] = []
                for i in range(n):
                    try:
                        box = await loc.nth(i).bounding_box()
                    except Exception:
                        box = None
                    if box and box.get("width", 0) > 2 and box.get("height", 0) > 2:
                        visible.append(i)
                if not visible:
                    continue
                if check and len(visible) < 2:
                    continue
                try:
                    first = loc.nth(visible[0])
                    await first.click(timeout=4000)
                    await first.fill(value)
                    if check:
                        second = loc.nth(visible[1])
                        await second.click(timeout=4000)
                        await second.fill(check)
                    got = (await first.input_value()).strip()
                    if got != value:
                        continue
                    if check:
                        got2 = (await loc.nth(visible[1]).input_value()).strip()
                        if got2 != check:
                            continue
                    return True
                except Exception:
                    logger.debug("NNC1 get_by_label 填写失败 [%s]", text, exc_info=True)
                    continue
        return False

    async def _fill_input_under_exact_label(
        self, page, labels: list[str], value: str, *, check: str = ""
    ) -> bool:
        """只在该 LABEL 自己的小容器里填可见 input，不爬隔壁身分栏。

        无 check：只写第一个框。有 check：前两个框分别写主号、校验位。
        """
        last: Any = None
        arg = {"labels": labels, "value": value, "check": (check or "").strip()}
        if await self._fill_by_accessible_label(page, labels, value, check=arg["check"]):
            return True
        js = """({ labels, value, check }) => {
            const setNative = (el, v) => {
                const proto = el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                el.focus();
                setter.call(el, v);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            };
            const vis = (el) => {
                if (!el || el.disabled) return false;
                const t = (el.getAttribute('type') || 'text').toLowerCase();
                if (['hidden','radio','checkbox','file','submit','button'].includes(t))
                    return false;
                return true;
            };
            const compact = (s) => (s || '').replace(/\\s+/g, '').trim();
            const wants = labels.map(s => compact(s));
            const otherIdRe = /香港身分[証證证]部分|完整[護护]照|[護护]照部分|[護护]照签发|[護护]照簽發|完整香港身分/;
            const hasOtherIdLabel = (root, lab) => {
                for (const l of root.querySelectorAll('label')) {
                    if (lab && l === lab) continue;
                    const t = compact(l.innerText || '');
                    if (!t) continue;
                    if (wants.includes(t) || wants.includes(t.replace(/\\*$/, '')))
                        continue;
                    if (otherIdRe.test(t)) return true;
                }
                return false;
            };
            const tryFill = (ins) => {
                const boxed = ins.filter((el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 2 && r.height > 2;
                });
                const use = boxed.length ? boxed : ins;
                if (check) {
                    if (use.length < 2) return false;
                    setNative(use[0], value);
                    setNative(use[1], check);
                    return use[0].value === value && use[1].value === check;
                }
                if (use.length < 1) return false;
                setNative(use[0], value);
                return use[0].value === value;
            };
            const cands = [];
            for (const want of wants) {
                for (const div of document.querySelectorAll('div')) {
                    const t = compact(div.innerText || '');
                    if (t !== want && t !== want + '()') continue;
                    if (hasOtherIdLabel(div, null)) continue;
                    const ins = [...div.querySelectorAll('input, textarea')].filter(vis);
                    if (!ins.length) continue;
                    const r = div.getBoundingClientRect();
                    cands.push({ ins, area: r.width * r.height });
                }
            }
            cands.sort((a, b) => a.area - b.area);
            for (const c of cands) {
                if (tryFill(c.ins)) return true;
            }
            for (const lab of document.querySelectorAll('label')) {
                const t = compact(lab.innerText || '');
                if (!wants.includes(t) && !wants.includes(t.replace(/\\*$/, '')))
                    continue;
                let root = lab.parentElement;
                for (let i = 0; i < 5 && root; i++) {
                    if (hasOtherIdLabel(root, lab)) break;
                    if (tryFill([...root.querySelectorAll('input, textarea')].filter(vis)))
                        return true;
                    root = root.parentElement;
                }
            }
            return false;
        }"""
        for frame in page.frames:
            try:
                last = await frame.evaluate(js, arg)
            except Exception:
                continue
            if last:
                return True
        return bool(last)

    async def _select_nnc1_passport_country(self, page, option: str) -> bool:
        """护照签发国家／地区：只认该 LABEL，在其所在容器里选下拉。"""
        option = (option or "").strip()
        if not option:
            return False
        labels = [
            "护照签发国家／地区",
            "护照签发国家/地区",
            "護照簽發國家／地區",
            "護照簽發國家/地區",
        ]
        opened = await page.evaluate(
            """({ labels }) => {
                const wants = labels.map(s => (s || '').replace(/\\s+/g, ''));
                const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                for (const lab of document.querySelectorAll('label')) {
                    const t = (lab.innerText || '').replace(/\\s+/g, '').trim();
                    if (!wants.includes(t)) continue;
                    let root = lab.parentElement;
                    for (let i = 0; i < 5 && root; i++) {
                        const native = root.querySelector('select');
                        if (native && vis(native)) return 'native';
                        const ant = root.querySelector('.ant-select');
                        if (ant && vis(ant)) {
                            const trigger = ant.querySelector('.ant-select-selector') || ant;
                            trigger.scrollIntoView({ block: 'center' });
                            trigger.click();
                            return 'ant';
                        }
                        root = root.parentElement;
                    }
                }
                return '';
            }""",
            {"labels": labels},
        )
        if opened == "ant":
            try:
                await page.wait_for_selector(".ant-select-item-option", timeout=15000)
            except Exception:
                pass
            opt = page.locator(".ant-select-item-option").filter(
                has_text=re.compile(re.escape(option), re.I)
            ).first
            if await opt.count() > 0:
                await opt.click(timeout=5000)
                logger.info("已选择护照签发国家: %s", option)
                await page.wait_for_timeout(400)
                return True
            return False
        if opened == "native":
            return await self._select_option_by_label(page, labels, option)
        logger.warning("NNC1 未找到护照签发国家下拉")
        return False

    async def _fill_nnc1_identity_slot(
        self, page, labels: list[str], value: str
    ) -> bool:
        """身分識別栏兜底：短标签，避免大容器误填。"""
        if not value:
            return False
        hit = await page.evaluate(
            """({ labels, value }) => {
                const setNative = (el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    ).set;
                    el.focus();
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const skip = /請輸入|请输入|如沒有|如没有|部分號碼|部分号码/;
                const visInp = (x) => {
                    const r = x.getBoundingClientRect();
                    const t = (x.getAttribute('type') || 'text').toLowerCase();
                    return r.width > 0 && r.height > 0 && !x.disabled
                        && !['hidden','radio','checkbox','file','submit','button'].includes(t);
                };
                const nodes = [...document.querySelectorAll(
                    'label, .rowTitle, td, th, span, p'
                )];
                const allInp = [...document.querySelectorAll(
                    'input:not([type=hidden]):not([type=checkbox]):not([type=radio])'
                )].filter(visInp);
                for (const pat of labels) {
                    const compactPat = pat.replace(/\\s+/g, '');
                    const hits = [];
                    for (const el of nodes) {
                        const t = (el.innerText || '').replace(/\\s+/g, '').trim();
                        if (!t || t.length > 22) continue;
                        if (skip.test(t)) continue;
                        if (!t.includes(compactPat)) continue;
                        hits.push({ el, len: t.length });
                    }
                    hits.sort((a, b) => a.len - b.len);
                    for (const { el } of hits) {
                        const lr = el.getBoundingClientRect();
                        const nearby = allInp.filter((inp) => {
                            const rel = el.compareDocumentPosition(inp);
                            const following = (rel & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
                            const contained = (rel & Node.DOCUMENT_POSITION_CONTAINED_BY) !== 0;
                            if (!following && !contained) return false;
                            const r = inp.getBoundingClientRect();
                            const midL = (lr.top + lr.bottom) / 2;
                            const midI = (r.top + r.bottom) / 2;
                            return Math.abs(midI - midL) < 48
                                || (r.top >= lr.top - 16 && r.top <= lr.bottom + 130);
                        });
                        if (!nearby.length) continue;
                        setNative(nearby[0], value);
                        return nearby[0].value === value ? pat : '';
                    }
                }
                return '';
            }""",
            {"labels": labels, "value": value},
        )
        if hit:
            logger.info("已填写身分栏 [%s]: %s", hit, value[:80])
            return True
        logger.warning("未找到身分栏: %s", labels[0])
        return False

    async def _fill_step3_identity(
        self, page, person: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """NNC1-3.1 身分識別。

        香港身份证：完整港证号码拆两框；完整护照号码填「無」；不选护照签发国家。
        非香港身份证：港证第一框填「無」；护照号码 + 签发国家按实际填写。
        """
        id_number, id_type = self._resolve_person_id(person, data)
        issuing = self._resolve_issuing_country(person, data)
        plan = nnc1_identity_fill_plan(id_type, id_number, issuing)
        is_hkid = id_type == "HKID"
        await self._scroll_to_section(page, ["身分識別", "身份识别", "Identification"])
        logger.info(
            "NNC1-3.1 身分識別 type=%s hkid=%s passport=%s country=%s",
            id_type,
            plan["hkid"][:16],
            plan["passport"][:16],
            plan.get("passport_country") or "-",
        )

        hkid_ok = await self._fill_hkid_number(page, plan["hkid"])

        passport_ok = await self._fill_nnc1_passport_number(page, plan["passport"])
        await self._log_nnc1_identity_values(page)

        if not hkid_ok or not passport_ok:
            await self._log_nnc1_identity_markers(page)
            await self._maybe_screenshot(page, "step3_identity_fail")
            logger.warning(
                "NNC1-3.1 身分欄未寫全 hkid_ok=%s passport_ok=%s",
                hkid_ok,
                passport_ok,
            )

        if is_hkid:
            logger.info("NNC1-3.1 港证：跳过护照签发国家")
            return
        if plan.get("passport_country"):
            await self._wait_labeled_dropdown_options(
                page,
                [
                    "护照签发国家／地区",
                    "護照簽發國家／地區",
                    "护照签发国家",
                    "護照簽發國家",
                ],
            )
            for country in icris_country_select_candidates(
                issuing or plan["passport_country"]
            ):
                if await self._select_nnc1_passport_country(page, country):
                    break

    async def _click_add_to_officer_list(self, page) -> None:
        """点击「加入至創辦成員/高級人員列表」。"""
        await self._scroll_form_to_bottom(page)
        await page.wait_for_timeout(300)
        pat = re.compile(
            r"加入至創辦成員.*高級人員列表|加入至创办成员.*高级人员列表",
            re.I,
        )
        btn = page.locator("button, a, input[type=button], [role=button]").filter(
            has_text=pat
        ).first
        if await btn.count() > 0:
            await btn.scroll_into_view_if_needed()
            await btn.click(timeout=15000)
            logger.info("已点击「加入至創辦成員/高級人員列表」")
            await wait_spin_clear(page, timeout_ms=90000)
            await page.wait_for_timeout(1500)
            return
        clicked = await page.evaluate(
            """() => {
                const pat = /加入至創辦成員.*高級人員列表|加入至创办成员.*高级人员列表/i;
                for (const el of document.querySelectorAll(
                    'button, a, input[type=button], [role=button], .btn'
                )) {
                    const t = (el.innerText || el.value || '').trim();
                    if (!pat.test(t.replace(/\\s+/g, ''))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    el.scrollIntoView({ block: 'center' });
                    el.click();
                    return t.slice(0, 50);
                }
                return '';
            }"""
        )
        if clicked:
            logger.info("已点击「%s」(JS)", clicked)
            await wait_spin_clear(page, timeout_ms=90000)
            await page.wait_for_timeout(1500)
            return
        await self._maybe_screenshot(page, "add_officer_fail")
        raise RuntimeError("未找到「加入至創辦成員/高級人員列表」按钮")

    # ========== NNC1-3.2 董事同意书签署人 ==========

    async def _wait_director_consent_signatory_page(self, page) -> None:
        """等待「出任董事職位同意書」簽署人选择页加载完成。"""
        logger.info("等待董事同意书簽署人页面")
        await wait_spin_clear(page, timeout_ms=60000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass

        def _signatory_ready_js() -> str:
            return """() => {
                if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return '';
                const body = document.body?.innerText || '';
                const hasHeading = /請選擇出任董事職位同意書的簽署人|选择出任董事职位同意书的签署人|出任董事職位同意書.*簽署人/i.test(body);
                const hasLabel = /簽署人|签署人/.test(body);
                if (!hasHeading && !hasLabel) return '';
                let hasConfirm = false;
                for (const el of document.querySelectorAll('button,a,[role=button],input[type=button]')) {
                    const t = (el.innerText || el.value || '').trim();
                    if (/^確認$|^确认$|^Confirm$/i.test(t)) {
                        hasConfirm = true;
                        break;
                    }
                }
                const hasSelect = document.querySelector('.ant-select, select');
                if ((hasHeading || hasLabel) && (hasConfirm || hasSelect)) return 'ready';
                return '';
            }"""

        try:
            await page.wait_for_function(_signatory_ready_js(), timeout=120000)
            logger.info("董事同意书簽署人页面已就绪")
        except Exception as e:
            # spinner 未消失但表单已可见时继续
            ok = await page.evaluate(_signatory_ready_js())
            if ok:
                logger.warning("簽署人页面 spinner 未清，但表单已可见，继续")
            else:
                await self._maybe_screenshot(page, "step3_signatory_wait_fail")
                raise RuntimeError(f"簽署人页面加载超时: {e}")
        await page.wait_for_timeout(800)
        # 等待簽署人下拉选项加载
        try:
            await page.wait_for_function(
                """() => {
                    for (const el of document.querySelectorAll(
                        'label, .rowTitle, span, div, th, p'
                    )) {
                        const t = (el.innerText || '').replace(/\\s+/g, '');
                        if (!t.includes('簽署人') && !t.includes('签署人')) continue;
                        if (t.length > 60) continue;
                        const row = el.closest('tr,.ant-row,.ant-form-item,fieldset,div')
                            || el.parentElement?.parentElement;
                        if (!row) continue;
                        const sel = row.querySelector('select');
                        if (sel && sel.options.length > 1) return true;
                        const ant = row.querySelector('.ant-select');
                        if (ant) return true;
                    }
                    return document.querySelector('.ant-select') !== null;
                }""",
                timeout=30000,
            )
        except Exception:
            logger.debug("簽署人下拉 DOM 等待超时，继续尝试选择")

    async def _mark_signatory_select(self, page) -> bool:
        return bool(
            await page.evaluate(
                "() => {\n"
                + _NNC1_FIND_SIGNATORY_FN
                + """
                    document.querySelectorAll('[data-nnc1-signatory-select]').forEach(el => {
                        el.removeAttribute('data-nnc1-signatory-select');
                    });
                    const node = nnc1FindSignatoryControl();
                    if (!node) return false;
                    node.setAttribute('data-nnc1-signatory-select', '1');
                    return true;
                }"""
            )
        )

    async def _get_signatory_display_text(self, page) -> str:
        if not await self._mark_signatory_select(page):
            return ""
        return (
            await page.evaluate(
                """() => {
                    const root = document.querySelector('[data-nnc1-signatory-select]');
                    if (!root) return '';
                    if (root.tagName === 'SELECT') {
                        const opt = root.options[root.selectedIndex];
                        return ((opt?.textContent || opt?.value || '')).trim();
                    }
                    const sel = root.querySelector('.ant-select-selector') || root;
                    const item = root.querySelector('.ant-select-selection-item');
                    const placeholder = root.querySelector('.ant-select-selection-placeholder');
                    const parts = [
                        item?.innerText,
                        sel?.innerText,
                        root.getAttribute('title'),
                    ].map(t => (t || '').trim()).filter(Boolean);
                    for (const p of parts) {
                        if (!/請選擇|请选择|^Select$/i.test(p)) return p;
                    }
                    return (placeholder?.innerText || parts[0] || '').trim();
                }"""
            )
            or ""
        ).strip()

    async def _select_first_signatory(self, page) -> bool:
        """簽署人下拉按顺序选第一项真实选项，不按姓名匹配。

        只操作已标记的簽署人控件及其当前打开的下拉面板，避免扫到页面上
        其它 ant-select（国家/证件等）的选项。
        """
        await self._mark_signatory_select(page)
        cur = await self._get_signatory_display_text(page)
        if cur and not is_signatory_placeholder(cur):
            logger.info("簽署人已选中: %s", cur[:50])
            return True

        # native <select>：按 index 选第一项非 placeholder
        picked_native = await page.evaluate(
            """() => {
                const sel = document.querySelector('[data-nnc1-signatory-select]');
                if (!sel || sel.tagName !== 'SELECT') return '';
                for (let i = 0; i < sel.options.length; i++) {
                    const t = (sel.options[i].textContent || '').trim();
                    if (!t || /請選擇|请选择|^Select$/i.test(t)) continue;
                    sel.selectedIndex = i;
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return t.slice(0, 50);
                }
                return '';
            }"""
        )
        if picked_native:
            logger.info("已选簽署人(select/JS index): %s", picked_native)
            await page.wait_for_timeout(500)
            return True

        sel_loc = page.locator("[data-nnc1-signatory-select]").first
        if await sel_loc.count() > 0 and await sel_loc.evaluate("el => el.tagName === 'SELECT'"):
            count = await sel_loc.locator("option").count()
            texts: list[str] = []
            for i in range(count):
                texts.append(
                    (await sel_loc.locator("option").nth(i).inner_text() or "").strip()
                )
            idx = first_real_signatory_index(texts)
            if idx is not None:
                try:
                    await sel_loc.select_option(index=idx)
                    logger.info(
                        "已选簽署人(select index=%s): %s", idx, texts[idx][:50]
                    )
                    await page.wait_for_timeout(500)
                    return True
                except Exception:
                    pass

        trigger = page.locator(
            "[data-nnc1-signatory-select] .ant-select-selector"
        ).first
        if await trigger.count() == 0:
            trigger = page.locator("[data-nnc1-signatory-select]").first
        if await trigger.count() == 0:
            logger.warning("未找到簽署人下拉控件")
            return False

        await trigger.scroll_into_view_if_needed()
        try:
            await trigger.click(timeout=10000)
        except Exception:
            await page.evaluate(
                """() => {
                    const t = document.querySelector(
                        '[data-nnc1-signatory-select] .ant-select-selector,'
                        + '[data-nnc1-signatory-select]'
                    );
                    if (t) t.click();
                }"""
            )
        try:
            await page.wait_for_selector(
                ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
                ".ant-select-item-option",
                timeout=15000,
            )
        except Exception:
            await page.wait_for_timeout(800)

        # 只点当前打开面板里、按 DOM 顺序的第一项（跳过 placeholder / disabled）
        picked = await page.evaluate(
            """() => {
                const skip = t => !t || /請選擇|请选择|^Select$/i.test(t.trim());
                const panels = [...document.querySelectorAll(
                    '.ant-select-dropdown:not(.ant-select-dropdown-hidden)'
                )];
                const panel = panels.length ? panels[panels.length - 1] : null;
                const items = panel
                    ? panel.querySelectorAll('.ant-select-item-option')
                    : [];
                for (const opt of items) {
                    if (opt.classList.contains('ant-select-item-option-disabled')) {
                        continue;
                    }
                    const tx = (opt.innerText || '').trim();
                    if (skip(tx)) continue;
                    opt.scrollIntoView({ block: 'nearest' });
                    opt.click();
                    return tx.slice(0, 50);
                }
                return '';
            }"""
        )
        if picked:
            await page.wait_for_timeout(500)
            new_val = await self._get_signatory_display_text(page)
            if new_val and not is_signatory_placeholder(new_val):
                logger.info("已选簽署人(第一项): %s", new_val[:50])
                return True
            logger.warning(
                "簽署人点击后仍显示请选择 (picked=%s display=%s)",
                picked[:40],
                (new_val or "")[:40],
            )

        # 键盘：打开后下箭头选第一项，不读姓名
        try:
            await trigger.click(timeout=5000)
            await page.wait_for_timeout(400)
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            final_kb = await self._get_signatory_display_text(page)
            if final_kb and not is_signatory_placeholder(final_kb):
                logger.info("已选簽署人(键盘第一项): %s", final_kb[:50])
                return True
        except Exception as e:
            logger.debug("簽署人键盘选择失败: %s", e)

        final = await self._get_signatory_display_text(page)
        if final and not is_signatory_placeholder(final):
            logger.info("簽署人已选中(校验): %s", final[:50])
            return True
        logger.warning("未选到簽署人第一项，当前显示: %s", final[:40] if final else "空")
        return False

    async def _ensure_first_signatory_selected(
        self, page, *, fail_label: str, err: str
    ) -> None:
        for _attempt in range(5):
            if await self._select_first_signatory(page):
                cur = await self._get_signatory_display_text(page)
                if cur and not is_signatory_placeholder(cur):
                    logger.info("簽署人已确认: %s", cur[:50])
                    return
            await page.wait_for_timeout(800)
        await self._maybe_screenshot(page, fail_label)
        raise RuntimeError(err)

    async def _wait_signatory_confirm_ready(self, page) -> None:
        """选择簽署人后等待 spinner 消失且「確認」可点。"""
        logger.info("等待簽署人选择后页面就绪")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        await wait_spin_clear(page, timeout_ms=60000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const compact = s => (s || '').replace(/\\s+/g, '');
                    const isConfirm = t => /^(確認|确认|Confirm)$/i.test(compact(t));
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button], .btn, .ant-btn'
                    )) {
                        const t = compact(
                            el.innerText || el.value || el.getAttribute('aria-label') || ''
                        );
                        if (!isConfirm(t)) continue;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                        if (el.classList?.contains('ant-btn-disabled')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return false;
                }""",
                timeout=30000,
            )
        except Exception:
            logger.debug("簽署人頁「確認」等待超时，继续尝试点击")
        await page.wait_for_timeout(200)
        logger.info("簽署人选择后加载等待结束，准备点击確認")

    async def _dump_signatory_click_candidates(self, page) -> None:
        """失败时输出簽署人页可点击候选，便于排查。"""
        try:
            info = await page.evaluate(
                """() => {
                    const items = [];
                    for (const el of document.querySelectorAll(
                        'button, a, input, [role=button], .btn, .ant-btn, [onclick]'
                    )) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const cs = getComputedStyle(el);
                        items.push({
                            tag: el.tagName,
                            type: el.getAttribute('type') || '',
                            cls: (el.className || '').toString().slice(0, 80),
                            text: ((el.innerText || el.value || el.getAttribute('aria-label') || '')
                                .trim()).slice(0, 40),
                            value: (el.value || '').slice(0, 40),
                            bg: cs.backgroundColor,
                            color: cs.color,
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width), h: Math.round(r.height),
                        });
                    }
                    return {
                        url: location.href,
                        bodyHasConfirm: /確認|确认|Confirm/.test(document.body?.innerText || ''),
                        bodyHasCancel: /取消|Cancel/.test(document.body?.innerText || ''),
                        iframeCount: document.querySelectorAll('iframe').length,
                        items: items.slice(0, 80),
                    };
                }"""
            )
            out = (
                Path(PROJECT_ROOT)
                / "data"
                / "icris_form_screenshots"
                / f"signatory_click_debug_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("簽署人点击候选已写入: %s", out)
            logger.info(
                "bodyHasConfirm=%s bodyHasCancel=%s iframes=%s candidates=%s",
                info.get("bodyHasConfirm"),
                info.get("bodyHasCancel"),
                info.get("iframeCount"),
                len(info.get("items") or []),
            )
        except Exception as e:
            logger.debug("dump signatory candidates failed: %s", e)

    async def _find_confirm_button(self, page):
        """查找董事同意书页「確認」按钮（文案 / 结构 / 红色按钮）。"""
        for text in ("確認", "确认", "Confirm"):
            loc = page.get_by_text(text, exact=True)
            n = await loc.count()
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                except Exception:
                    continue
                handle = await el.evaluate_handle(
                    """el => {
                        const ok = n => n && (
                            n.tagName === 'BUTTON' || n.tagName === 'A' ||
                            n.tagName === 'INPUT' || n.getAttribute('role') === 'button' ||
                            (n.className && /btn|button/i.test(String(n.className)))
                        );
                        let cur = el;
                        for (let i = 0; i < 6 && cur; i++) {
                            if (ok(cur)) return cur;
                            cur = cur.parentElement;
                        }
                        return el;
                    }"""
                )
                btn = handle.as_element()
                if btn is None:
                    continue
                disabled = await btn.evaluate(
                    """el => !!(el.disabled || el.getAttribute('aria-disabled') === 'true'
                        || el.classList?.contains('ant-btn-disabled')
                        || el.classList?.contains('disabled'))"""
                )
                if not disabled:
                    return btn

        handle = await page.evaluate_handle(
            """() => {
                const compact = s => (s || '').replace(/\\s+/g, '');
                const isConfirm = t => /^(確認|确认|Confirm)$/i.test(compact(t));
                const isCancel = t => /^(取消|Cancel)$/i.test(compact(t));
                const labelOf = el => compact(
                    el.innerText || el.value || el.getAttribute('aria-label')
                    || el.getAttribute('title') || ''
                );
                const actionable = el => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return null;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return null;
                    if (el.classList?.contains('ant-btn-disabled')) return null;
                    return el;
                };
                const isRedish = el => {
                    const bg = getComputedStyle(el).backgroundColor || '';
                    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                    if (!m) return false;
                    const r = +m[1], g = +m[2], b = +m[3];
                    return r > 150 && g < 100 && b < 100;
                };
                const all = [...document.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], input[type=image], '
                    + '[role=button], .btn, .ant-btn, [onclick]'
                )];

                for (const el of all) {
                    if (!isConfirm(labelOf(el))) continue;
                    const hit = actionable(el);
                    if (hit) return hit;
                }
                for (const el of all) {
                    if (!isCancel(labelOf(el))) continue;
                    const parent = el.parentElement;
                    if (!parent) continue;
                    const kids = [...parent.children].filter(c => {
                        const r = c.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    const idx = kids.indexOf(el);
                    if (idx > 0) {
                        const prev = kids[idx - 1];
                        const hit = actionable(prev)
                            || actionable(prev.querySelector('button,a,input,.btn,.ant-btn'));
                        if (hit) return hit;
                    }
                    for (const sib of kids) {
                        if (sib === el) continue;
                        if (isRedish(sib)) {
                            const hit = actionable(sib);
                            if (hit) return hit;
                        }
                    }
                }
                let scope = null;
                for (const el of document.querySelectorAll('div, section, form, fieldset, table')) {
                    const t = el.innerText || '';
                    if (/請選擇出任董事職位同意書的簽署人|签署人|簽署人/.test(t)
                        && /取消|Cancel/.test(t)) {
                        scope = el;
                        break;
                    }
                }
                const pool = scope ? [...scope.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], .btn, .ant-btn, [role=button]'
                )] : all;
                const reds = pool.filter(el => actionable(el) && isRedish(el));
                if (reds.length) {
                    reds.sort((a, b) => {
                        const ra = a.getBoundingClientRect();
                        const rb = b.getBoundingClientRect();
                        return (rb.y - ra.y) || (ra.x - rb.x);
                    });
                    return reds[0];
                }
                return null;
            }"""
        )
        el = handle.as_element() if handle else None
        if el is not None:
            return el
        return None

    async def _click_confirm_button(self, page) -> bool:
        """点击「確認」按钮（董事同意书页）；选完簽署人并加载后再点。"""
        await self._scroll_form_to_bottom(page)
        btn = await self._find_confirm_button(page)
        if btn is not None:
            await btn.scroll_into_view_if_needed()
            try:
                await btn.click(timeout=15000)
            except Exception:
                await btn.click(timeout=15000, force=True)
            logger.info("已点击「確認」")
            await wait_spin_clear(page, timeout_ms=90000)
            await page.wait_for_timeout(1000)
            return True

        clicked = await page.evaluate(
            """() => {
                const compact = s => (s || '').replace(/\\s+/g, '');
                const labelOf = el => compact(
                    el.innerText || el.value || el.getAttribute('aria-label')
                    || el.getAttribute('title') || ''
                );
                const isConfirm = t => /^(確認|确认|Confirm)$/i.test(t);
                const isCancel = t => /^(取消|Cancel)$/i.test(t);
                const isRedish = el => {
                    const bg = getComputedStyle(el).backgroundColor || '';
                    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                    if (!m) return false;
                    const r = +m[1], g = +m[2], b = +m[3];
                    return r > 150 && g < 100 && b < 100;
                };
                const tryClick = (el) => {
                    if (!el) return '';
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return '';
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return '';
                    el.scrollIntoView({ block: 'center' });
                    el.click();
                    return labelOf(el) || (el.tagName + ':' + Math.round(r.x) + ',' + Math.round(r.y));
                };
                const all = [...document.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], input[type=image], '
                    + '[role=button], .btn, .ant-btn, [onclick]'
                )];
                for (const el of all) {
                    if (isConfirm(labelOf(el))) {
                        const hit = tryClick(el);
                        if (hit) return hit;
                    }
                }
                for (const el of all) {
                    if (!isCancel(labelOf(el))) continue;
                    const parent = el.parentElement;
                    if (!parent) continue;
                    const kids = [...parent.children];
                    const idx = kids.indexOf(el);
                    if (idx > 0) {
                        const hit = tryClick(kids[idx - 1]);
                        if (hit) return hit;
                    }
                    for (const sib of kids) {
                        if (sib === el) continue;
                        if (isRedish(sib)) {
                            const hit = tryClick(sib);
                            if (hit) return hit;
                        }
                    }
                }
                const reds = all.filter(isRedish);
                reds.sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
                if (reds[0]) {
                    const hit = tryClick(reds[0]);
                    if (hit) return hit;
                }
                return '';
            }"""
        )
        if clicked:
            logger.info("已点击「確認」(JS): %s", str(clicked)[:50])
            await wait_spin_clear(page, timeout_ms=90000)
            await page.wait_for_timeout(1000)
            return True
        return False

    async def _confirm_director_consent_signatory(self, page) -> None:
        """簽署人选第一项 → 等待加载 → 確認。"""
        await self._wait_director_consent_signatory_page(page)
        await self._ensure_first_signatory_selected(
            page,
            fail_label="step3_signatory_select_fail",
            err="未能选择簽署人第一项",
        )
        await self._wait_signatory_confirm_ready(page)
        if not await self._click_confirm_button(page):
            await page.wait_for_timeout(2000)
            await wait_spin_clear(page, timeout_ms=30000)
            if not await self._click_confirm_button(page):
                await self._dump_signatory_click_candidates(page)
                await self._maybe_screenshot(page, "step3_confirm_fail")
                raise RuntimeError("未找到董事同意书「確認」按钮")

    async def _wait_step3_officer_list_page(self, page) -> None:
        """等待創辦成員/高級人員列表页（含儲存及繼續）。"""
        logger.info("等待創辦成員/高級人員列表页")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const body = document.body?.innerText || '';
                    if (!/創辦成員.*高級人員列表|创办成员.*高级人员列表|創辦成員\\/高級人員/.test(body)) {
                        return false;
                    }
                    if (!/(儲存|储存|存储)及(繼續|继续)/.test(body)) return false;
                    if (/修改|刪除|删除/.test(body)) return true;
                    const tables = document.querySelectorAll('table');
                    for (const tb of tables) {
                        if (/中文名稱|中文名称|英文名稱/.test(tb.innerText || '')) return true;
                    }
                    return false;
                }""",
                timeout=120000,
            )
            logger.info("創辦成員/高級人員列表页已就绪")
        except Exception as e:
            await self._maybe_screenshot(page, "step3_list_wait_fail")
            raise RuntimeError(f"創辦成員列表页加载超时: {e}")
        await page.wait_for_timeout(800)

    # ========== NNC1-3.3 公司秘书（法人团体） ==========

    def _resolve_company_secretary(self, data: dict[str, Any]) -> dict[str, Any]:
        """解析公司秘书法人团体信息：个案优先，缺省读注册配置。"""
        from src.materials.aggregator import apply_default_office, _get_default_office

        apply_default_office(data)
        sec = dict(data.get("company_secretary") or {})
        default = _get_default_office()
        if not str(sec.get("br_number") or "").strip():
            sec["br_number"] = default.get("secretary_br_no", "")
        if not str(sec.get("license_number") or "").strip():
            sec["license_number"] = default.get("secretary_license_no", "")
        if not str(sec.get("company_number") or "").strip():
            sec["company_number"] = default.get("secretary_company_no", "")
        sec.setdefault("type", "body_corporate")
        if sec.get("hk_registered") is None:
            sec["hk_registered"] = True
        return sec

    async def _select_body_corporate(self, page) -> bool:
        """选择類型：法人團體。"""
        for attempt in range(3):
            if await self._select_radio_by_patterns(
                page, ["法人團體", "法人团体", "Body Corporate"]
            ):
                return True
            await wait_spin_clear(page, timeout_ms=30000)
            await page.wait_for_timeout(800)
        loc = page.locator(".ant-radio-wrapper, label").filter(
            has_text=re.compile(r"法人團體|法人团体")
        )
        count = await loc.count()
        for i in range(count):
            item = loc.nth(i)
            txt = re.sub(r"\s+", "", (await item.inner_text() or ""))
            if len(txt) > 30:
                continue
            try:
                await item.scroll_into_view_if_needed()
                await item.click(force=True, timeout=5000)
                logger.info("已选择单选(Playwright): 法人團體")
                await page.wait_for_timeout(400)
                return True
            except Exception:
                pass
        return False

    async def _wait_step3_secretary_form_expanded(self, page) -> None:
        """勾选公司秘书后等待法人团体详情展开。"""
        logger.info("等待 NNC1 步骤3 公司秘书表单")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const body = document.body?.innerText || '';
                    return /是否為在香港註冊的法人團體|是否为在香港注册的法人团体|商業登記號碼|商业登记号码/.test(body);
                }""",
                timeout=120000,
            )
            logger.info("NNC1 步骤3 公司秘书表单已展开")
        except Exception as e:
            await self._maybe_screenshot(page, "step3_secretary_wait_fail")
            raise RuntimeError(f"步骤3 公司秘书表单展开超时: {e}")
        await page.wait_for_timeout(800)

    async def _select_hk_registered_corporate_yes(self, page) -> bool:
        """是否為在香港註冊的法人團體？ → 是。"""
        hit = await page.evaluate(
            """() => {
                const qRe = /是否為在香港註冊的法人團體|是否为在香港注册的法人团体/;
                let scope = null;
                for (const el of document.querySelectorAll(
                    'div, section, fieldset, tr, td, label, span'
                )) {
                    const t = (el.innerText || '').trim();
                    if (!qRe.test(t) || t.length > 80) continue;
                    scope = el.closest('tr, .ant-row, .ant-form-item, fieldset, div')
                        || el.parentElement;
                    break;
                }
                const roots = scope ? [scope, scope.parentElement, document] : [document];
                for (const root of roots) {
                    if (!root) continue;
                    for (const label of root.querySelectorAll(
                        'label, .ant-radio-wrapper, span'
                    )) {
                        const t = (label.innerText || '').replace(/\\s+/g, '').trim();
                        if (t !== '是' && t !== 'Yes') continue;
                        const input = label.querySelector('input[type=radio]')
                            || label.closest('label')?.querySelector('input[type=radio]');
                        if (input) {
                            if (!input.checked) input.click();
                            return '是';
                        }
                        label.click();
                        return '是';
                    }
                    for (const input of root.querySelectorAll('input[type=radio]')) {
                        const ctx = (
                            input.closest('label, span, div')?.innerText || ''
                        ).replace(/\\s+/g, '').trim();
                        if (ctx !== '是' && ctx !== 'Yes') continue;
                        if (!input.checked) input.click();
                        return '是';
                    }
                }
                return '';
            }"""
        )
        if hit:
            logger.info("已选择香港注册法人团体: %s", hit)
            await page.wait_for_timeout(500)
            return True
        return await self._select_radio_by_patterns(page, ["^是$", "是"])

    async def _check_company_secretary_only(self, page) -> None:
        """阶段B身分：只勾「公司秘書」，明确取消创办成员/董事。

        限定在「身分」行内操作，避免点到董事帮助文案里的「公司秘書」。
        """
        r_sec = await self._set_capacity_role(page, "secretary", True)
        if not r_sec or not r_sec.get("ok"):
            if not await self._click_capacity_role_playwright(
                page, ("公司秘書", "公司秘书"), want_checked=True
            ):
                logger.warning(
                    "公司秘書勾选失败: %s labels=%s",
                    (r_sec or {}).get("reason"),
                    (r_sec or {}).get("labels"),
                )
                await self._maybe_screenshot(page, "step3_secretary_check_fail")
                raise RuntimeError("未能勾选「公司秘書」（勿误勾董事）")
        await wait_spin_clear(page, timeout_ms=60000)
        await page.wait_for_timeout(300)

        for role, texts in (
            ("director", ("董事",)),
            ("founder", ("創辦成員", "创办成员")),
        ):
            r = await self._set_capacity_role(page, role, False)
            if r and not r.get("ok") and not str(r.get("reason") or "").startswith("missing_"):
                await self._click_capacity_role_playwright(
                    page, texts, want_checked=False
                )
        await wait_spin_clear(page, timeout_ms=30000)

        state = await self._read_capacity_state(page)
        if not state.get("secretary"):
            await self._maybe_screenshot(page, "step3_secretary_check_fail")
            raise RuntimeError(
                f"公司秘書勾选校验失败：未勾选 labels={state.get('labels')}"
            )
        if state.get("director"):
            await self._maybe_screenshot(page, "step3_secretary_check_fail")
            raise RuntimeError("公司秘書勾选校验失败：董事仍被勾选")
        if state.get("founder"):
            await self._maybe_screenshot(page, "step3_secretary_check_fail")
            raise RuntimeError("公司秘書勾选校验失败：创办成员仍被勾选")
        logger.info("阶段B身分校验通过: 仅公司秘書")
        await page.wait_for_timeout(400)

    async def _br_search_names_filled(self, page) -> bool:
        """商業登記號碼检索后中文/英文名称是否已带出。"""
        return bool(
            await page.evaluate(
                """() => {
                    for (const el of document.querySelectorAll(
                        'label, .rowTitle, span, div, th, td'
                    )) {
                        const t = (el.innerText || '').replace(/\\s+/g, '');
                        if (!/^(中文名稱|英文名稱|中文名称|英文名称)$/.test(t)) {
                            continue;
                        }
                        const row = el.closest('tr,.ant-row,.ant-form-item,div')
                            || el.parentElement;
                        const inp = row?.querySelector(
                            'textarea, input:not([type=hidden]):not([type=checkbox])'
                        );
                        if (inp && (inp.value || '').trim().length > 1) return true;
                    }
                    return false;
                }"""
            )
        )

    async def _prepare_br_number_input(self, page) -> bool:
        """聚焦商業登記號碼输入框并触发 blur，确保值已提交。"""
        return bool(
            await page.evaluate(
                """() => {
                    const compact = s => (s || '').replace(/\\s+/g, '').trim();
                    let brInp = null;
                    for (const el of document.querySelectorAll(
                        'label, .rowTitle, span, div, th, td, p'
                    )) {
                        const t = compact(el.innerText || '');
                        if (!/^商業登記號碼|^商业登记号码/.test(t) || t.length > 24) {
                            continue;
                        }
                        const row = el.closest(
                            'tr, .ant-row, .ant-form-item, fieldset, div'
                        ) || el.parentElement;
                        brInp = row?.querySelector(
                            'input:not([type=hidden]):not([type=checkbox])'
                            + ':not([type=radio]), textarea'
                        );
                        if (brInp) break;
                    }
                    if (!brInp) {
                        for (const inp of document.querySelectorAll('input, textarea')) {
                            if (/^\\d{7,8}$/.test((inp.value || '').trim())) {
                                brInp = inp;
                                break;
                            }
                        }
                    }
                    if (!brInp) return false;
                    brInp.focus();
                    brInp.dispatchEvent(new Event('input', { bubbles: true }));
                    brInp.dispatchEvent(new Event('change', { bubbles: true }));
                    brInp.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                    return true;
                }"""
            )
        )

    async def _find_br_search_button_coords(self, page) -> dict | None:
        """定位紧挨商業登記號碼输入框下方的「檢索」按钮坐标。"""
        raw = await page.evaluate(
            """() => {
                const compact = s => (s || '').replace(/\\s+/g, '').trim();
                const isSearch = t => t === '檢索' || t === '检索'
                    || /^Search$/i.test(t);
                let brRect = null;
                for (const el of document.querySelectorAll(
                    'label, .rowTitle, span, div, th, td, p'
                )) {
                    const t = compact(el.innerText || '');
                    if (!/^商業登記號碼|^商业登记号码/.test(t) || t.length > 24) {
                        continue;
                    }
                    const row = el.closest(
                        'tr, .ant-row, .ant-form-item, fieldset, div'
                    ) || el.parentElement;
                    const brInp = row?.querySelector(
                        'input:not([type=hidden]):not([type=checkbox])'
                        + ':not([type=radio]), textarea'
                    );
                    if (brInp) {
                        brRect = brInp.getBoundingClientRect();
                        break;
                    }
                }
                if (!brRect) {
                    for (const inp of document.querySelectorAll('input, textarea')) {
                        if (!/^\\d{7,8}$/.test((inp.value || '').trim())) continue;
                        brRect = inp.getBoundingClientRect();
                        break;
                    }
                }
                if (!brRect || brRect.width <= 0) return null;

                let best = null;
                let bestScore = Infinity;
                for (const el of document.querySelectorAll(
                    'button, a, input[type=button], input[type=submit], '
                    + 'span, div, .ant-btn, [role=button]'
                )) {
                    const t = compact(el.innerText || el.value || '');
                    if (!isSearch(t) || t.length > 6) continue;
                    const clickable = el.closest(
                        'button, a, .ant-btn, [role=button]'
                    ) || el;
                    const r = clickable.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    // 檢索在 BR 输入框正下方（截图：按钮紧贴输入框下方）
                    if (r.top < brRect.top - 20 || r.top > brRect.bottom + 90) continue;
                    if (Math.abs(r.left - brRect.left) > 320) continue;
                    const score = (r.top - brRect.bottom)
                        + Math.abs(r.left - brRect.left) * 0.05;
                    if (score < bestScore) {
                        bestScore = score;
                        best = clickable;
                    }
                }
                if (!best) return null;
                const r = best.getBoundingClientRect();
                return {
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    tag: best.tagName,
                    cls: (best.className || '').slice(0, 80),
                };
            }"""
        )
        return raw if raw else None

    async def _wait_br_search_result(self, page, *, timeout_ms: int = 45000) -> bool:
        """等待检索后公司名称带出。"""
        if await self._br_search_names_filled(page):
            return True
        try:
            await page.wait_for_function(
                """() => {
                    for (const el of document.querySelectorAll(
                        'label, .rowTitle, span, div, th, td'
                    )) {
                        const t = (el.innerText || '').replace(/\\s+/g, '');
                        if (!/^(中文名稱|英文名稱|中文名称|英文名称)$/.test(t)) {
                            continue;
                        }
                        const row = el.closest('tr,.ant-row,.ant-form-item,div')
                            || el.parentElement;
                        const inp = row?.querySelector(
                            'textarea, input:not([type=hidden]):not([type=checkbox])'
                        );
                        if (inp && (inp.value || '').trim().length > 1) return true;
                    }
                    return false;
                }""",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    async def _click_br_search_button(self, page) -> bool:
        """点击商業登記號碼旁的「檢索」按钮，并验证检索已触发。"""
        if await self._br_search_names_filled(page):
            logger.info("商業登記號碼检索结果已存在，跳过檢索")
            return True

        await self._prepare_br_number_input(page)
        await page.wait_for_timeout(300)

        async def _try_mouse_click() -> bool:
            coords = await self._find_br_search_button_coords(page)
            if not coords:
                return False
            await page.mouse.click(coords["x"], coords["y"])
            logger.info(
                "已坐标点击「檢索」(x=%.0f,y=%.0f tag=%s)",
                coords["x"],
                coords["y"],
                coords.get("tag", "?"),
            )
            return True

        async def _try_playwright_row_click() -> bool:
            br_row = page.locator("tr, .ant-row, .ant-form-item, div").filter(
                has_text=re.compile(r"商業登記號碼|商业登记号码")
            ).first
            if await br_row.count() == 0:
                return False
            for text in ("檢索", "检索", "Search"):
                btn = br_row.get_by_text(text, exact=True).first
                if await btn.count() == 0:
                    btn = br_row.locator(
                        f"button:has-text('{text}'), .ant-btn:has-text('{text}'), "
                        f"a:has-text('{text}'), span:has-text('{text}')"
                    ).first
                if await btn.count() == 0:
                    continue
                wrap = btn.locator(
                    "xpath=ancestor-or-self::button"
                    "[1] | ancestor-or-self::*[contains(@class,'ant-btn')][1]"
                ).first
                target = wrap if await wrap.count() > 0 else btn
                await target.scroll_into_view_if_needed()
                try:
                    await target.click(timeout=10000)
                except Exception:
                    await target.click(timeout=10000, force=True)
                logger.info("已点击「檢索」(Playwright row): %s", text)
                return True
            return False

        async def _try_enter_on_br_input() -> bool:
            loc = page.locator("input, textarea").filter(
                has=page.locator(
                    "xpath=ancestor::*[contains(., '商業登記號碼')"
                    " or contains(., '商业登记号码')][1]"
                )
            ).first
            if await loc.count() == 0:
                ok = await page.evaluate(
                    """() => {
                        for (const inp of document.querySelectorAll(
                            'input, textarea'
                        )) {
                            if (!/^\\d{7,8}$/.test((inp.value || '').trim())) {
                                continue;
                            }
                            inp.focus();
                            return true;
                        }
                        return false;
                    }"""
                )
                if not ok:
                    return False
            else:
                await loc.focus()
            await page.keyboard.press("Enter")
            logger.info("已在商業登記號碼输入框按 Enter 触发检索")
            return True

        for attempt, click_fn in enumerate(
            (_try_playwright_row_click, _try_mouse_click, _try_enter_on_br_input),
            start=1,
        ):
            try:
                if not await click_fn():
                    continue
                await wait_spin_clear(page, timeout_ms=10000)
                result_ms = 30000 if attempt >= 3 else 8000
                if await self._wait_br_search_result(page, timeout_ms=result_ms):
                    logger.info("商業登記號碼检索成功 (attempt=%s)", attempt)
                    return True
                logger.warning(
                    "檢索点击后名称未带出 (attempt=%s)，重试下一种方式",
                    attempt,
                )
            except Exception as e:
                logger.warning("檢索点击失败 (attempt=%s): %s", attempt, e)

        await self._maybe_screenshot(page, "step3_br_search_fail")
        return False

    async def _fill_secretary_hk_address(self, page, office: dict[str, Any]) -> None:
        """公司秘书香港地址：室/楼/座、大厦、街道、区。"""
        await self._select_radio_by_patterns(
            page, ["香港地址", "本港地址", "Hong Kong Address"]
        )
        flat = (office.get("flat_floor") or "").strip()
        building = (office.get("building") or "").strip()
        street = (office.get("street_en") or office.get("street") or "").strip()
        district = (office.get("district") or "").strip()

        if flat:
            await self._fill_field_by_label(
                page,
                ["室／樓／座", "室／樓", "室/楼/座", "室/楼", "Flat / Floor"],
                flat,
            )
        if building:
            await self._fill_field_by_label(page, ["大廈", "大厦", "Building"], building)
        if street:
            await self._fill_field_by_label(
                page,
                ["街道／屋苑／地段／村", "街道", "Street", "Estate"],
                street,
            )
        if district:
            await self._select_option_by_label(
                page, ["區", "区", "District"], district
            )

    async def _fill_step3_corporate_secretary(self, page, data: dict[str, Any]) -> None:
        """步骤3：在列表页追加法人团体公司秘书，再交给储存及继续。"""
        logger.info("NNC1 步骤3: 填写法人团体公司秘书")
        sec = self._resolve_company_secretary(data)
        office = dict(data.get("registered_office") or {})
        email = str((data.get("contact") or {}).get("email") or "").strip()
        br_no = str(sec.get("br_number") or "").strip()
        license_no = str(sec.get("license_number") or "").strip()
        company_no = str(sec.get("company_number") or "").strip()

        if not await self._select_body_corporate(page):
            raise RuntimeError("未能选择「法人團體」")
        await page.wait_for_timeout(600)

        await self._check_company_secretary_only(page)
        await self._wait_step3_secretary_form_expanded(page)

        # 表单展开后再次确保身分只剩公司秘書
        await self._check_company_secretary_only(page)

        if not await self._select_hk_registered_corporate_yes(page):
            logger.warning("未能选择「是否為在香港註冊的法人團體？是」，继续尝试填表")
        await wait_spin_clear(page, timeout_ms=60000)
        await page.wait_for_timeout(500)

        if br_no:
            br_filled = False
            br_field = page.get_by_label(
                re.compile(r"商業登記號碼|商业登记号码|Business Registration")
            ).first
            if await br_field.count() > 0:
                await br_field.scroll_into_view_if_needed()
                await br_field.fill(br_no)
                await br_field.press("Tab")
                br_filled = True
                logger.info("已填写 [商業登記號碼]: %s", br_no)
            if not br_filled:
                br_filled = await self._fill_field_by_label(
                    page,
                    ["商業登記號碼", "商业登记号码", "Business Registration"],
                    br_no,
                )
            if not br_filled:
                raise RuntimeError("未能填写商業登記號碼")
            await page.wait_for_timeout(400)
            if not await self._click_br_search_button(page):
                raise RuntimeError("未找到或未能点击「檢索」按钮")
            if await self._br_search_names_filled(page):
                logger.info("商業登記號碼检索后公司名称已带出")
            else:
                logger.warning("檢索后公司名称未自动带出，继续填写其余字段")

        await self._fill_secretary_hk_address(page, office)

        if email:
            await self._fill_field_by_label(
                page, ["電郵地址", "电邮地址", "Email"], email
            )

        if license_no:
            await self._scroll_to_section(
                page, ["牌照編號", "牌照编号", "信託或公司服務", "信托或公司服务"]
            )
            await page.wait_for_timeout(400)
            ok_lic = await self._fill_field_by_label(
                page,
                [
                    "牌照編號",
                    "牌照编号",
                    "Licence No",
                    "License No",
                    "牌照",
                ],
                license_no,
            )
            if not ok_lic:
                # 精确填入「牌照編號」旁的 textarea
                filled = await page.evaluate(
                    """(value) => {
                        const re = /牌照編號|牌照编号/;
                        for (const el of document.querySelectorAll(
                            'label, .rowTitle, span, div, th, td, p'
                        )) {
                            const t = (el.innerText || '').replace(/\\s+/g, '').trim();
                            if (!re.test(t) || t.length > 20) continue;
                            const row = el.closest('tr, .ant-row, .ant-form-item, fieldset, div')
                                || el.parentElement;
                            const inp = row?.querySelector(
                                'textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio])'
                            );
                            if (!inp || inp.disabled) continue;
                            inp.focus();
                            inp.value = value;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                        return false;
                    }""",
                    license_no,
                )
                if filled:
                    logger.info("已填写牌照編號(JS): %s", license_no)
                else:
                    logger.warning("未找到字段: 牌照編號")
            await page.wait_for_timeout(300)

        if company_no:
            # 表单未必有此栏；有则填
            await self._fill_field_by_label(
                page,
                ["公司號碼", "公司号码", "Company No", "Company Number"],
                company_no,
            )

        await self._click_add_to_officer_list(page)
        await wait_spin_clear(page, timeout_ms=90000)
        await page.wait_for_timeout(1000)

    # ========== NNC1-3 总控：创办成员/董事 → 签署人 → 公司秘书 → 储存及继续 ==========

    async def _fill_step3_person_form(self, page, data: dict[str, Any]) -> None:
        """NNC1-3.1.1～3.1.7：自然人到同意书。國家／地區 失败时抛 Nnc1CountryDropdownEmpty。"""
        # --- NNC1-3.1.1 类型：自然人 ---
        if not await self._select_natural_person(page):
            logger.warning("未能选择自然人，继续尝试填表")
        await page.wait_for_timeout(300)

        # --- NNC1-3.1.2 身分：创办成员 + 董事 ---
        await self._ensure_step3_identity_checkboxes(page)
        await self._wait_step3_form_expanded(page)

        # --- NNC1-3.1.3 姓名 ---
        person = self._resolve_step3_person(data)
        await self._fill_step3_person_names(page, person, data)

        # --- NNC1-3.1.4 认购股本 ---
        sc = self._resolve_share_capital(data)
        await self._scroll_form_to_bottom(page)
        await self._fill_member_subscribed_capital(page, sc)

        # --- NNC1-3.1.5 地址（创办成员 / 董事通讯 / 复制通常住址）---
        addr = self._resolve_person_address(person, data)

        await self._fill_nnc1_address_section(
            page,
            r"地址.*適用於創辦成員|地址.*适用于创办成员|地址.*創辦成員",
            addr,
            exclude_re=r"通訊|通讯|通常",
            block_key="founder",
            scroll_keywords=["地址", "創辦成員", "创办成员"],
        )
        await self._fill_nnc1_address_section(
            page,
            r"通訊地址.*適用於董事|通讯地址.*适用于董事|通訊地址.*董事",
            addr,
            block_key="corr",
            scroll_keywords=["通訊地址", "通讯地址", "董事"],
        )

        await self._click_copy_founder_address(page)

        # --- NNC1-3.1.6 身份识别 ---
        await self._fill_step3_identity(page, person, data)

        # --- NNC1-3.1.7 出任董事职位同意书 ---
        await self._ensure_checkbox_by_patterns(
            page,
            ["出任董事職位同意書", "出任董事职位同意书", "董事將會簽署"],
        )

    async def _fill_step3_member_info(self, page, data: dict[str, Any]) -> None:
        """步骤3：創辦成員/董事 — 类型/身分、姓名、认购股本、地址、证件、加入列表。"""
        logger.info("NNC1 步骤3: 輸入創辦成員/董事/公司秘書資料")
        await self._wait_step3_shell_ready(page)

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    await self._reenter_step3_from_step2(page)
                await self._fill_step3_person_form(page, data)
                last_err = None
                break
            except Nnc1CountryDropdownEmpty as e:
                last_err = e
                logger.warning(
                    "步驟3.1 國家／地區 失败 attempt=%s/3: %s",
                    attempt + 1,
                    e,
                )
                if attempt >= 2:
                    await self._maybe_screenshot(page, "step3_country_fail")
                    raise
        if last_err is not None:
            await self._maybe_screenshot(page, "step3_country_fail")
            raise last_err

        # --- NNC1-3.1.8 加入创办成员/高级人员列表 ---
        await self._click_add_to_officer_list(page)
        # --- NNC1-3.2 董事同意书签署人 ---
        await self._confirm_director_consent_signatory(page)
        await self._wait_step3_officer_list_page(page)
        # --- NNC1-3.3 公司秘书（法人团体）---
        await self._fill_step3_corporate_secretary(page, data)
        await self._wait_step3_officer_list_page(page)
        # --- NNC1-3.4 储存及继续 ---
        await self._click_save_and_continue(page)

    # ========== NNC1-3.5 创办成员表簽署人 ==========

    async def _fill_step3_form_signatory(self, page) -> None:
        """NNC1-3.5：人员列表「储存及继续」之后，仍停在步骤3。

        页面文案「請選擇創辦成員…的簽署人」，簽署人下拉选第一项（跳过請選擇），
        再点「储存及继续」。与 3.2 董事同意书页不同：那边点的是「確認」。
        """
        logger.info("NNC1 步骤3.5: 创办成员表簽署人")
        # --- NNC1-3.5.1 等待簽署人页加载完成（含下拉选项，勿过早点选）---
        await self._wait_nnc1_step_ready(
            page,
            heading_js=r"請選擇創辦成員|请选择创办成员",
            label="3.5 簽署人",
            footer="save_continue",
        )
        await self._wait_form_signatory_dropdown_ready(page)

        # --- NNC1-3.5.2 簽署人下拉：第一项真实选项 ---
        await self._ensure_first_signatory_selected(
            page,
            fail_label="step3_form_signatory_fail",
            err="未能选择创办成员表簽署人第一项",
        )

        # --- NNC1-3.5.3 储存及继续 → 步骤4 ---
        await self._click_save_and_continue(page)

    # ========== NNC1-4 致商業登記署通知書 ==========

    async def _select_br_notice_no(self, page) -> bool:
        """在步骤4 区块内点「否」（一年期商业登记证），避免整页误匹配。"""
        if await is_page_loading(page):
            logger.info("步骤4 选否前仍有载入中，等待遮罩消失")
            cleared = await wait_spin_clear(page, timeout_ms=90000)
            if not cleared or await is_page_loading(page):
                logger.warning("步骤4 载入中未消失，不点否")
                return False
        hit = await page.evaluate(
            """() => {
                const qRe = /致商業登記署通知書|致商业登记署通知书/;
                let scope = null;
                for (const el of document.querySelectorAll(
                    'div, section, fieldset, form, table, article'
                )) {
                    const t = (el.innerText || '').trim();
                    if (!qRe.test(t) || t.length > 6000) continue;
                    scope = el;
                    break;
                }
                const roots = scope ? [scope, document] : [document];
                const clickNo = (root) => {
                    for (const label of root.querySelectorAll(
                        'label, .ant-radio-wrapper, span'
                    )) {
                        const t = (label.innerText || '').replace(/\\s+/g, '').trim();
                        if (t !== '否' && t !== 'No') continue;
                        const input = label.querySelector('input[type=radio]')
                            || label.closest('label')?.querySelector('input[type=radio]');
                        if (input) {
                            if (!input.checked) input.click();
                            return '否';
                        }
                        label.click();
                        return '否';
                    }
                    for (const input of root.querySelectorAll('input[type=radio]')) {
                        const ctx = (
                            input.closest('label, span, div')?.innerText || ''
                        ).replace(/\\s+/g, '').trim();
                        if (ctx !== '否' && ctx !== 'No') continue;
                        if (!input.checked) input.click();
                        return '否';
                    }
                    return '';
                };
                for (const root of roots) {
                    const got = clickNo(root);
                    if (got) return got;
                }
                return '';
            }"""
        )
        if hit:
            logger.info("已选择商业登记通知: %s", hit)
            await page.wait_for_timeout(400)
            return True
        return False

    async def _fill_step4_br_notice(self, page) -> None:
        """NNC1-4：致商業登記署通知書 — 选「否」后储存及继续。"""
        logger.info("NNC1 步骤4: 致商業登記署通知書")
        # --- NNC1-4.1 等待步骤4 加载完成 ---
        await self._wait_nnc1_step_ready(
            page,
            heading_js=r"致商業登記署通知書|致商业登记署通知书",
            label="4 商業登記署通知書",
            footer="save_continue",
        )
        if await is_page_loading(page):
            logger.info("步骤4 就绪后又出现载入中，再等遮罩")
            await wait_spin_clear(page, timeout_ms=90000)

        # --- NNC1-4.2 选「否」（一年期商业登记证）---
        if not await self._select_br_notice_no(page):
            await self._maybe_screenshot(page, "step4_no_fail")
            raise RuntimeError("未能选择「致商業登記署通知書」否")

        # --- NNC1-4.3 储存及继续 → 步骤5 ---
        await self._click_save_and_continue(page)

    # ========== NNC1-5 詳情概要 ==========

    async def _fill_step5_summary(self, page) -> None:
        """NNC1-5：詳情概要 — 滚到底，点「繼續」。"""
        logger.info("NNC1 步骤5: 詳情概要")
        await self._wait_nnc1_step_ready(
            page,
            heading_js=r"詳情概要|详情概要",
            label="5 詳情概要",
            footer="continue",
        )
        await self._scroll_form_to_bottom(page)
        await page.wait_for_timeout(400)
        await self._click_continue_only(page)

    # ========== NNC1-6 簽署及提交（董事同意书） ==========

    async def _eval_in_frames(self, page, expression: str, arg: Any = None) -> Any:
        """在主文档和 iframe 里执行 JS，返回第一个有效结果。"""
        last: Any = None
        for frame in page.frames:
            try:
                last = (
                    await frame.evaluate(expression, arg)
                    if arg is not None
                    else await frame.evaluate(expression)
                )
                if last in (None, False, "", [], {}):
                    continue
                if isinstance(last, dict) and not any(last.values()):
                    continue
                return last
            except Exception:
                continue
        return last

    async def _body_text(self, page) -> str:
        try:
            return str(await page.evaluate("() => document.body?.innerText || ''") or "")
        except Exception:
            return ""

    def _looks_like_step6_table(self, text: str) -> bool:
        if not re.search(r"預覽並簽署|预览并签署", text):
            return False
        return bool(
            re.search(r"步驟\s*6|步骤\s*6|簽署及提交|签署及提交", text)
            or re.search(r"出任董事職位同意書|出任董事职位同意书", text)
        )

    def _looks_like_full_form_preview(self, text: str) -> bool:
        if re.search(r"預覽並簽署|预览并签署", text):
            return False
        return bool(
            re.search(r"檢閱|检阅", text)
            and re.search(r"放棄申請|放弃申请", text)
            and re.search(r"繼續|继续", text)
        )

    async def _sign_modal_visible(self, page) -> str:
        """彈窗處於哪一屏：consent / credentials / success / ''。"""
        return str(
            await self._eval_in_frames(
                page,
                """() => {
                    const vis = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const roots = [...document.querySelectorAll(
                        '.ant-modal, .ant-modal-content, [role=dialog], .modal'
                    )].filter(vis);
                    const blob = (roots.length ? roots : [document.body])
                        .map(el => el.innerText || '').join('\\n');
                    if (/已成功簽署|已成功签署/.test(blob)) return 'success';
                    if (/input|密碼|密码/.test(blob)
                        && document.querySelector('input[type=password]')
                        && vis(document.querySelector('input[type=password]'))) {
                        return 'credentials';
                    }
                    if (/本人現核證|本人现核证/.test(blob)
                        || /本人同意在公司成立為法團時擔任其董事|本人同意在公司成立为法团时担任其董事/.test(blob)
                        || /請選擇簽署方式|请选择签署方式/.test(blob)) {
                        return 'consent';
                    }
                    return '';
                }""",
            )
            or ""
        )

    async def _ensure_step6_sign_table(self, page) -> None:
        """步骤5 繼續之后：若落在整表预览，再點繼續，直到步骤6 签署表。"""
        logger.info("NNC1 步骤6: 等待簽署及提交表格")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        await page.wait_for_timeout(800)

        for attempt in range(3):
            text = await self._body_text(page)
            if self._looks_like_step6_table(text):
                await self._wait_nnc1_step_ready(
                    page,
                    heading_js=r"預覽並簽署|预览并签署",
                    label="6 簽署及提交",
                    footer="none",
                )
                logger.info("NNC1 步骤6 签署表已就绪")
                return
            if self._looks_like_full_form_preview(text) or (
                re.search(r"檢閱|检阅|放棄申請|放弃申请", text)
                and re.search(r"繼續|继续", text)
                and not re.search(r"預覽並簽署|预览并签署", text)
            ):
                logger.info("NNC1 步骤6: 整表预览页，再點繼續 (%s)", attempt + 1)
                await self._click_continue_only(page)
                await wait_spin_clear(page, timeout_ms=90000)
                continue
            await page.wait_for_timeout(1500)

        await self._wait_nnc1_step_ready(
            page,
            heading_js=r"預覽並簽署|预览并签署",
            label="6 簽署及提交",
            footer="none",
        )

    async def _click_director_consent_preview_sign(self, page) -> None:
        """在「出任董事職位同意書（自然人）」行點「預覽並簽署」，勿點發送簽署請求。"""
        hit = await self._eval_in_frames(
            page,
            """() => {
                const rowRe = /出任董事職位同意書|出任董事职位同意书/;
                const personRe = /自然人|Natural\\s*Person/i;
                const btnRe = /預覽並簽署|预览并签署/;
                const skipRe = /發送簽署請求|发送签署请求/;
                const rows = document.querySelectorAll(
                    'tr, .ant-table-row, li, [class*=row]'
                );
                const tryClick = (scope) => {
                    for (const el of scope.querySelectorAll(
                        'button, a, [role=button], span, div'
                    )) {
                        const lab = (el.innerText || el.getAttribute('title') || '')
                            .replace(/\\s+/g, '');
                        if (!btnRe.test(lab) || skipRe.test(lab)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const clickable = el.closest('button, a, [role=button]') || el;
                        clickable.click();
                        return lab.slice(0, 40);
                    }
                    return '';
                };
                for (const row of rows) {
                    const t = (row.innerText || '').replace(/\\s+/g, '');
                    if (!rowRe.test(t) || t.length > 500) continue;
                    if (!personRe.test(t) && !/自然人/.test(row.innerText || '')) {
                        continue;
                    }
                    const got = tryClick(row);
                    if (got) return got;
                }
                return tryClick(document);
            }""",
        )
        if not hit:
            await self._maybe_screenshot(page, "step6_preview_sign_fail")
            raise RuntimeError("未找到「出任董事職位同意書（自然人）」的預覽並簽署")
        logger.info("已点击預覽並簽署: %s", str(hit)[:40])
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    async def _wait_director_consent_preview(self, page) -> None:
        """預覽並簽署之后的文件预览页（滚到底再點繼續才会出签署弹窗）。"""
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const body = document.body?.innerText || '';
                    if (/本人同意在公司成立為法團時擔任其董事|請選擇簽署方式|本人現核證|本人现核证/.test(body)) {
                        return true;
                    }
                    const savePat = /(储存|存储|儲存)及(继续|繼續)/;
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], [role=button], .btn'
                    )) {
                        const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                        if (savePat.test(compact)) continue;
                        if (!/^(繼續|继续|Continue)$/i.test(compact)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return /表格\\s*NNC1|Form NNC1|檢閱|放弃申请|放棄申請/i.test(body);
                }""",
                timeout=120000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_doc_preview_wait_fail")
            raise RuntimeError(f"董事同意书预览页加载超时: {e}")
        await page.wait_for_timeout(600)

    async def _click_sign_modal_continue(self, page) -> str:
        """只点签署弹窗页脚的「繼續」，避开页面底栏同名按钮。"""
        scopes = [
            page.locator(".ant-modal:visible .ant-modal-footer").last,
            page.locator("[role=dialog]:visible").last,
            page.locator(".modal:visible .modal-footer").last,
        ]
        for scope in scopes:
            try:
                if await scope.count() == 0:
                    continue
            except Exception:
                continue
            btns = scope.locator(
                "button, a, [role=button], .ant-btn, .btn, input[type=button], input[type=submit]"
            )
            try:
                count = await btns.count()
            except Exception:
                continue
            for i in range(count):
                el = btns.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    raw = (
                        (await el.inner_text())
                        or (await el.get_attribute("value"))
                        or ""
                    ).strip()
                    compact = re.sub(r"\s+", "", raw)
                    if re.search(r"取消|Cancel|返回|Back", compact, re.I):
                        continue
                    if NNC1_SAVE_CONTINUE_PAT.search(compact):
                        continue
                    if not re.search(r"繼續|继续|Continue", compact, re.I):
                        continue
                    try:
                        await el.click(timeout=8000)
                    except Exception:
                        await el.click(force=True, timeout=8000)
                    return raw or compact
                except Exception:
                    continue
        hit = await self._eval_in_frames(
            page,
            """() => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roots = [...document.querySelectorAll(
                    '.ant-modal, .ant-modal-content, [role=dialog], .modal'
                )].filter(vis);
                const root = roots.find(el =>
                    /請選擇簽署方式|请选择签署方式|本人同意在公司成立|本人現核證|本人现核证/.test(el.innerText || '')
                ) || roots[0];
                if (!root) return '';
                const savePat = /(储存|存储|儲存)及(继续|繼續)/;
                const footer = root.querySelector('.ant-modal-footer, .modal-footer') || root;
                const nodes = [...footer.querySelectorAll(
                    'button, a, [role=button], .ant-btn, .btn, input[type=button], input[type=submit]'
                )];
                for (const el of nodes.reverse()) {
                    const raw = (el.innerText || el.value || el.textContent || '').trim();
                    const compact = raw.replace(/\\s+/g, '');
                    if (savePat.test(compact)) continue;
                    if (/取消|Cancel|返回/i.test(compact)) continue;
                    if (!/(繼續|继续|Continue)/i.test(compact)) continue;
                    if (!vis(el)) continue;
                    el.removeAttribute('disabled');
                    el.setAttribute('aria-disabled', 'false');
                    el.click();
                    return compact || '繼續';
                }
                return '';
            }""",
        )
        return str(hit or "")

    async def _check_director_consent_and_username(self, page) -> None:
        """弹窗第一屏：勾选核证 + 用戶名稱，再點彈窗內繼續。

        董事弹窗：「本人同意…担任其董事…已年满18岁」
        创办成员陈述书：「本人现核证」
        两框都按步骤3身分勾选那套：找 wrapper → 点击 → input.check。
        """
        consent_pat = re.compile(
            r"本人现核证|本人現核證|"
            r"本人同意在公司成立为法团时担任其董事|"
            r"本人同意在公司成立為法團時擔任其董事|"
            r"已年满18岁|已年滿18歲"
        )
        ok = await self._eval_in_frames(
            page,
            """() => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roots = [...document.querySelectorAll(
                    '.ant-modal, .ant-modal-content, [role=dialog], .modal'
                )].filter(vis);
                const root = roots.find(el =>
                    /請選擇簽署方式|请选择签署方式|本人同意在公司成立|本人現核證|本人现核证/.test(el.innerText || '')
                ) || roots[0];
                if (!root) return null;

                const consentRe = /本人現核證|本人现核证|本人同意在公司成立為法團時擔任其董事|本人同意在公司成立为法团时担任其董事|已年滿18歲|已年满18岁/;
                let checked = false;
                for (const el of root.querySelectorAll(
                    'label, .ant-checkbox-wrapper, span, div'
                )) {
                    const t = (el.innerText || '').replace(/\\s+/g, '');
                    if (!consentRe.test(t) || t.length > 120) continue;
                    const wrap = el.closest('.ant-checkbox-wrapper, label') || el;
                    const box = wrap.querySelector(
                        'input.ant-checkbox-input, input[type=checkbox]'
                    ) || el.querySelector('input[type=checkbox]');
                    if (box && box.checked) {
                        checked = true;
                        break;
                    }
                    if (box && !box.checked) box.click();
                    else wrap.click();
                    const after = wrap.querySelector(
                        'input.ant-checkbox-input, input[type=checkbox]'
                    );
                    checked = !!(after && after.checked)
                        || wrap.classList.contains('ant-checkbox-wrapper-checked');
                    if (checked) break;
                }

                let userPicked = false;
                for (const el of root.querySelectorAll(
                    'label, .ant-radio-wrapper, span'
                )) {
                    const t = (el.innerText || '').replace(/\\s+/g, '').trim();
                    if (t !== '用戶名稱' && t !== '用户名称' && t !== 'UserID'
                        && t !== 'Username') {
                        continue;
                    }
                    const input = el.querySelector('input[type=radio]')
                        || el.closest('label')?.querySelector('input[type=radio]');
                    if (input) {
                        if (!input.checked) input.click();
                        userPicked = true;
                        break;
                    }
                    el.click();
                    userPicked = true;
                    break;
                }
                return { checked, userPicked };
            }""",
        )

        async def _consent_checked() -> bool:
            return bool(
                await self._eval_in_frames(
                    page,
                    """() => {
                        const vis = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        };
                        const roots = [...document.querySelectorAll(
                            '.ant-modal, .ant-modal-content, [role=dialog], .modal'
                        )].filter(vis);
                        const root = roots.find(el =>
                            /請選擇簽署方式|请选择签署方式|本人同意在公司成立|本人現核證|本人现核证/.test(el.innerText || '')
                        ) || roots[0];
                        if (!root) return false;
                        const re = /本人現核證|本人现核证|本人同意在公司成立為法團時擔任其董事|本人同意在公司成立为法团时担任其董事|已年滿18歲|已年满18岁/;
                        for (const wrap of root.querySelectorAll(
                            '.ant-checkbox-wrapper, label.ant-checkbox-wrapper, label'
                        )) {
                            const t = (wrap.innerText || '').replace(/\\s+/g, '');
                            if (!re.test(t) || t.length > 120) continue;
                            const inp = wrap.querySelector(
                                'input.ant-checkbox-input, input[type=checkbox]'
                            );
                            if (inp && inp.checked) return true;
                            if (wrap.classList.contains('ant-checkbox-wrapper-checked')) {
                                return true;
                            }
                        }
                        return false;
                    }""",
                )
            )

        if not await _consent_checked():
            wrap = page.locator(
                ".ant-modal:visible .ant-checkbox-wrapper, "
                ".ant-modal:visible label.ant-checkbox-wrapper, "
                "[role=dialog]:visible .ant-checkbox-wrapper"
            ).filter(has_text=consent_pat).first
            if await wrap.count() > 0:
                try:
                    await wrap.scroll_into_view_if_needed()
                    cls = await wrap.get_attribute("class") or ""
                    if "ant-checkbox-wrapper-checked" not in cls:
                        await wrap.click(force=True, timeout=5000)
                        await page.wait_for_timeout(300)
                        inp = wrap.locator(
                            "input.ant-checkbox-input, input[type=checkbox]"
                        ).first
                        if await inp.count() > 0:
                            await inp.check(force=True)
                except Exception:
                    pass
            if not await _consent_checked():
                await self._maybe_screenshot(page, "step6_consent_check_fail")
                raise RuntimeError("未能勾选签署核证（本人现核证 / 董事同意年满18岁）")
        if not isinstance(ok, dict) or not ok.get("userPicked"):
            await self._maybe_screenshot(page, "step6_username_radio_fail")
            raise RuntimeError("未能选择签署方式「用戶名稱」")
        await page.wait_for_timeout(400)

        continued = ""
        for _attempt in range(4):
            if not await _consent_checked():
                break
            continued = await self._click_sign_modal_continue(page)
            if continued:
                break
            await page.wait_for_timeout(700)
        if not continued:
            await self._maybe_screenshot(page, "step6_modal_continue_fail")
            raise RuntimeError("未能点击签署弹窗「繼續」")
        logger.info(
            "签署弹窗第一屏: checked=True user=%s continue=%s",
            (ok or {}).get("userPicked"),
            continued,
        )
        await wait_spin_clear(page, timeout_ms=30000)
        await page.wait_for_timeout(800)

    async def _fill_sign_modal_credentials(
        self, page, username: str, password: str
    ) -> None:
        filled = await self._eval_in_frames(
            page,
            """({ user, passwd }) => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roots = [...document.querySelectorAll(
                    '.ant-modal, .ant-modal-wrap, [role=dialog], .modal'
                )].filter(vis);
                const root = roots[0] || document;
                const setVal = (inp, value) => {
                    inp.focus();
                    const proto = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (proto) proto.call(inp, value);
                    else inp.value = value;
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                };
                let userOk = false;
                let passOk = false;
                const passInp = root.querySelector('input[type=password]');
                if (passInp && vis(passInp)) {
                    setVal(passInp, passwd);
                    passOk = true;
                }
                for (const inp of root.querySelectorAll(
                    'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=password])'
                )) {
                    if (!vis(inp) || inp.disabled) continue;
                    const ctx = (
                        inp.closest('label, .ant-form-item, tr, div')?.innerText || ''
                    ).replace(/\\s+/g, '');
                    const ph = (inp.placeholder || inp.getAttribute('aria-label') || '');
                    if (/用戶名稱|用户名称|Username|User\\s*ID/i.test(ctx + ph)) {
                        setVal(inp, user);
                        userOk = true;
                        break;
                    }
                }
                if (!userOk) {
                    const first = [...root.querySelectorAll(
                        'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=password])'
                    )].find(el => vis(el) && !el.disabled);
                    if (first) {
                        setVal(first, user);
                        userOk = true;
                    }
                }
                return { userOk, passOk };
            }""",
            {"user": username, "passwd": password},
        )
        if not isinstance(filled, dict) or not filled.get("userOk") or not filled.get("passOk"):
            await self._maybe_screenshot(page, "step6_cred_fill_fail")
            raise RuntimeError("未能填写签署弹窗的用户名称或密码")
        logger.info("已填写签署弹窗用户名称")
        await page.wait_for_timeout(300)

    async def _click_sign_modal_sign_button(self, page) -> None:
        hit = await self._eval_in_frames(
            page,
            """() => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roots = [...document.querySelectorAll(
                    '.ant-modal, .ant-modal-wrap, [role=dialog], .modal'
                )].filter(vis);
                const root = roots[0] || document;
                const savePat = /(储存|存储|儲存)及(继续|繼續)/;
                for (const el of root.querySelectorAll(
                    'button, a, [role=button], .ant-btn, input[type=button], input[type=submit]'
                )) {
                    const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                    if (savePat.test(compact)) continue;
                    if (!/^(簽署|签署|Sign)$/i.test(compact)) continue;
                    if (!vis(el)) continue;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    el.click();
                    return compact;
                }
                return '';
            }""",
        )
        if not hit:
            await self._maybe_screenshot(page, "step6_sign_btn_fail")
            raise RuntimeError("未找到签署弹窗「簽署」按钮")
        logger.info("已点击「%s」", hit)
        await wait_spin_clear(page, timeout_ms=60000)
        await page.wait_for_timeout(800)

    async def _click_signed_ok(self, page) -> None:
        try:
            await page.wait_for_function(
                """() => /已成功簽署|已成功签署/.test(document.body?.innerText || '')""",
                timeout=60000,
            )
        except Exception:
            pass
        hit = await self._eval_in_frames(
            page,
            """() => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roots = [...document.querySelectorAll(
                    '.ant-modal, .ant-modal-wrap, [role=dialog], .modal'
                )].filter(vis);
                const root = roots.find(el =>
                    /已成功簽署|已成功签署/.test(el.innerText || '')
                ) || roots[0] || document;
                for (const el of root.querySelectorAll(
                    'button, a, [role=button], .ant-btn, input[type=button]'
                )) {
                    const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                    if (!/^(確定|确定|OK|Confirm)$/i.test(compact)) continue;
                    if (!vis(el)) continue;
                    el.click();
                    return compact;
                }
                return '';
            }""",
        )
        if not hit:
            await self._maybe_screenshot(page, "step6_ok_fail")
            raise RuntimeError("未找到「已成功簽署」弹窗的確定")
        logger.info("已点击成功签署「%s」", hit)
        await wait_spin_clear(page, timeout_ms=60000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

    async def _complete_step6_sign_modal(self, page, account: IcrisAccount) -> None:
        """签署弹窗：核证/同意 + 用戶名稱 → 账号密码 → 簽署 → 已成功簽署確定。"""
        consent_ready = False
        try:
            await page.wait_for_function(
                """() => /本人同意在公司成立為法團時擔任其董事|請選擇簽署方式|本人現核證|本人现核证/.test(
                    document.body?.innerText || ''
                )""",
                timeout=20000,
            )
            consent_ready = True
        except Exception:
            for _ in range(40):
                if await self._sign_modal_visible(page) in ("consent", "credentials"):
                    consent_ready = True
                    break
                await page.wait_for_timeout(500)
        if not consent_ready:
            await self._maybe_screenshot(page, "step6_modal_wait_fail")
            raise RuntimeError("签署弹窗未出现")
        if await self._sign_modal_visible(page) != "credentials":
            await self._check_director_consent_and_username(page)

        try:
            await page.wait_for_function(
                """() => {
                    const pwd = document.querySelector('input[type=password]');
                    if (!pwd) return false;
                    const r = pwd.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }""",
                timeout=30000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_cred_wait_fail")
            raise RuntimeError(f"签署账号密码框未出现: {e}")
        await self._fill_sign_modal_credentials(
            page, account.username, account.password
        )
        await self._click_sign_modal_sign_button(page)
        await self._click_signed_ok(page)

    async def _click_founder_statement_preview_sign(self, page) -> None:
        """在「创办成员陈述书」行點「預覽並簽署」。"""
        hit = await self._eval_in_frames(
            page,
            """() => {
                const rowRe = /創辦成員陳述書|创办成员陈述书/;
                const btnRe = /預覽並簽署|预览并签署/;
                const skipRe = /發送簽署請求|发送签署请求/;
                const rows = document.querySelectorAll(
                    'tr, .ant-table-row, li, [class*=row]'
                );
                const tryClick = (scope) => {
                    for (const el of scope.querySelectorAll(
                        'button, a, [role=button], span, div'
                    )) {
                        const lab = (el.innerText || el.getAttribute('title') || '')
                            .replace(/\\s+/g, '');
                        if (!btnRe.test(lab) || skipRe.test(lab)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const clickable = el.closest('button, a, [role=button]') || el;
                        clickable.click();
                        return lab.slice(0, 40);
                    }
                    return '';
                };
                for (const row of rows) {
                    const t = (row.innerText || '').replace(/\\s+/g, '');
                    if (!rowRe.test(t) || t.length > 500) continue;
                    const got = tryClick(row);
                    if (got) return got;
                }
                return '';
            }""",
        )
        if not hit:
            await self._maybe_screenshot(page, "step6_founder_preview_sign_fail")
            raise RuntimeError("未找到「创办成员陈述书」的預覽並簽署")
        logger.info("已点击创办成员陈述书預覽並簽署: %s", str(hit)[:40])
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    async def _wait_founder_statement_row_ready(self, page) -> None:
        """董事签完后等签署表上创办成员陈述书仍可「预览并签署」。"""
        logger.info("NNC1 步骤6: 等待创办成员陈述书可签署")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const btnRe = /預覽並簽署|预览并签署/;
                    const skipRe = /發送簽署請求|发送签署请求/;
                    for (const row of document.querySelectorAll(
                        'tr, .ant-table-row, li, [class*=row]'
                    )) {
                        const t = (row.innerText || '').replace(/\\s+/g, '');
                        if (!/創辦成員陳述書|创办成员陈述书/.test(t) || t.length > 500) {
                            continue;
                        }
                        if (/已簽署|已签署/.test(t) && !btnRe.test(t)) return false;
                        for (const el of row.querySelectorAll(
                            'button, a, [role=button], span, div'
                        )) {
                            const lab = (el.innerText || '').replace(/\\s+/g, '');
                            if (!btnRe.test(lab) || skipRe.test(lab)) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) return true;
                        }
                    }
                    return false;
                }""",
                timeout=90000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_founder_row_wait_fail")
            raise RuntimeError(f"创办成员陈述书签署行未就绪: {e}")

    async def _fill_step6_founder_statement_sign(
        self, page, account: IcrisAccount
    ) -> None:
        """NNC1-6.6：签「创办成员陈述书」，到已成功签署确定。"""
        logger.info("NNC1 步骤6: 签署创办成员陈述书")
        await self._wait_founder_statement_row_ready(page)
        await self._click_founder_statement_preview_sign(page)

        if await self._sign_modal_visible(page) != "consent":
            await self._wait_director_consent_preview(page)
            if await self._sign_modal_visible(page) != "consent":
                await self._scroll_form_to_bottom(page)
                await self._click_continue_only(page)
        await page.wait_for_timeout(500)
        await self._complete_step6_sign_modal(page, account)

    async def _wait_step6_both_signed_continue(self, page) -> None:
        """确定后加载：两份已签署，底栏「继续」可点。"""
        logger.info("NNC1 步骤6: 等待两份已签署后的继续")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const body = document.body?.innerText || '';
                    if (!/步驟\\s*6|步骤\\s*6|簽署及提交|签署及提交/.test(body)) {
                        return false;
                    }
                    const signed = (rowRe) => {
                        for (const row of document.querySelectorAll(
                            'tr, .ant-table-row, li, [class*=row]'
                        )) {
                            const t = (row.innerText || '').replace(/\\s+/g, '');
                            if (!rowRe.test(t) || t.length > 500) continue;
                            return /已簽署|已签署/.test(t);
                        }
                        return false;
                    };
                    if (!signed(/出任董事職位同意書|出任董事职位同意书/)) return false;
                    if (!signed(/創辦成員陳述書|创办成员陈述书/)) return false;
                    const savePat = /(储存|存储|儲存)及(继续|繼續)/;
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button], .btn'
                    )) {
                        const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                        if (savePat.test(compact)) continue;
                        if (!/^(繼續|继续|Continue)$/i.test(compact)) continue;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
                            continue;
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return false;
                }""",
                timeout=120000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_both_signed_wait_fail")
            raise RuntimeError(f"两份已签署后「继续」未就绪: {e}")
        logger.info("NNC1 步骤6 两份已签署，继续可点")

    async def _click_goto_submit(self, page) -> None:
        """图二：点底栏「前往提交」，避开预览并签署。"""
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const body = document.body?.innerText || '';
                    for (const el of document.querySelectorAll(
                        'button, a, input[type=button], [role=button], .ant-btn, .btn'
                    )) {
                        const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                        if (!/^(前往提交)$/.test(compact)) continue;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
                            continue;
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return false;
                }""",
                timeout=120000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_goto_submit_wait_fail")
            raise RuntimeError(f"「前往提交」未就绪: {e}")
        await self._scroll_form_to_bottom(page)
        hit = await self._eval_in_frames(
            page,
            """() => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                for (const el of document.querySelectorAll(
                    'button, a, input[type=button], [role=button], .ant-btn, .btn'
                )) {
                    const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                    if (!/^(前往提交)$/.test(compact)) continue;
                    if (!vis(el)) continue;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    el.click();
                    return compact;
                }
                return '';
            }""",
        )
        if not hit:
            await self._maybe_screenshot(page, "step6_goto_submit_fail")
            raise RuntimeError("未找到「前往提交」按钮")
        logger.info("已点击「%s」", hit)
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(800)

    async def _click_save_record_yes(self, page) -> None:
        """图三：你要储存此文件以作纪录吗？→ 是。"""
        try:
            await page.wait_for_function(
                """() => /你要儲存此文件以作紀錄嗎|你要储存此文件以作纪录吗/.test(
                    document.body?.innerText || ''
                )""",
                timeout=60000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_save_record_wait_fail")
            raise RuntimeError(f"存档确认弹窗未出现: {e}")
        hit = await self._eval_in_frames(
            page,
            """() => {
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roots = [...document.querySelectorAll(
                    '.ant-modal, .ant-modal-content, [role=dialog], .modal'
                )].filter(vis);
                const root = roots.find(el =>
                    /你要儲存此文件以作紀錄嗎|你要储存此文件以作纪录吗/.test(el.innerText || '')
                ) || roots[0] || document;
                for (const el of root.querySelectorAll(
                    'button, a, [role=button], .ant-btn, input[type=button]'
                )) {
                    const compact = (el.innerText || el.value || '').replace(/\\s+/g, '');
                    if (!/^(是|Yes)$/i.test(compact)) continue;
                    if (!vis(el)) continue;
                    el.click();
                    return compact;
                }
                return '';
            }""",
        )
        if not hit:
            await self._maybe_screenshot(page, "step6_save_record_yes_fail")
            raise RuntimeError("未找到存档弹窗「是」")
        logger.info("已点击存档确认「%s」", hit)
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

    async def _read_preliminary_check_result(self, page) -> str:
        text = await self._eval_in_frames(
            page,
            """() => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const labelRe = /初步檢查結果|初步检查结果|Preliminary\\s*Check\\s*Result/i;
                for (const tr of document.querySelectorAll('tr')) {
                    const cells = [...tr.querySelectorAll('th, td')];
                    if (cells.length < 2) continue;
                    if (!labelRe.test(norm(cells[0].innerText))) continue;
                    return norm(cells.slice(1).map(c => c.innerText).join(' '));
                }
                for (const el of document.querySelectorAll(
                    'div, td, th, li, p, span, dt, dd'
                )) {
                    const t = norm(el.innerText);
                    if (!labelRe.test(t) || t.length > 80) continue;
                    const row = el.closest('tr, .ant-row, .ant-descriptions-item, dl, div');
                    if (!row) continue;
                    return norm(row.innerText).replace(labelRe, '').replace(/^[:：\\s]+/, '');
                }
                const body = document.body?.innerText || '';
                const m = body.match(/初步檢查結果[:：\\s]*([^\\n]+)|初步检查结果[:：\\s]*([^\\n]+)/);
                return ((m && (m[1] || m[2])) || '').trim();
            }""",
        )
        return str(text or "").strip()

    async def _wait_preliminary_check_and_continue(self, page) -> None:
        """到初步检查页：截图判定是否含通过，不点继续、不进付款页。"""
        logger.info("NNC1 步骤6: 等待初步检查页")
        await wait_spin_clear(page, timeout_ms=90000)
        try:
            await page.wait_for_function(
                """() => {
                    if (""" + ICRIS_PAGE_IS_LOADING_JS + """) return false;
                    const body = document.body?.innerText || '';
                    return /初步檢查|初步检查|Preliminary\\s*Check/i.test(body);
                }""",
                timeout=120000,
            )
        except Exception as e:
            await self._maybe_screenshot(page, "step6_prelim_wait_fail")
            raise RuntimeError(f"初步检查页加载超时: {e}")
        await page.wait_for_timeout(500)
        result = await self._read_preliminary_check_result(page)
        await self._maybe_screenshot(page, "step6_preliminary_check")
        if not prelim_check_passed(result):
            reason = result or "初步检查结果未通过"
            raise RuntimeError(f"初步检查未通过: {reason}")
        logger.info("初步检查通过: %s", result[:120])

    async def _fill_step6_proceed_submit(self, page) -> None:
        """两份已签署 → 继续 → 前往提交 → 存档是 → 初步检查（不点继续）。"""
        logger.info("NNC1 步骤6: 前往提交")
        await self._wait_step6_both_signed_continue(page)
        await self._click_continue_only(page)
        await self._click_goto_submit(page)
        await self._click_save_record_yes(page)
        await self._wait_preliminary_check_and_continue(page)

    async def _fill_step6_director_consent_sign(
        self, page, account: IcrisAccount
    ) -> None:
        """NNC1-6：签董事同意书、创办成员陈述书，再前往提交到初步检查。"""
        logger.info("NNC1 步骤6: 簽署及提交（董事同意书）")

        # --- NNC1-6.0 进入签署表（整表预览则再點繼續）---
        await self._ensure_step6_sign_table(page)

        # --- NNC1-6.1 預覽並簽署 ---
        await self._click_director_consent_preview_sign(page)

        # --- NNC1-6.2 预览页滚到底 → 繼續（若已弹出签署窗则跳过）---
        if await self._sign_modal_visible(page) != "consent":
            await self._wait_director_consent_preview(page)
            if await self._sign_modal_visible(page) != "consent":
                await self._scroll_form_to_bottom(page)
                await self._click_continue_only(page)
        await page.wait_for_timeout(500)

        # --- NNC1-6.3～6.5 弹窗签署 → 確定 ---
        await self._complete_step6_sign_modal(page, account)

        # --- NNC1-6.6 创办成员陈述书 ---
        await self._fill_step6_founder_statement_sign(page, account)
        # --- NNC1-6.7 继续 → 前往提交 → 是 → 初步检查继续 ---
        await self._fill_step6_proceed_submit(page)

    # ========== NNC1 入口 ==========

    async def run(
        self,
        account: IcrisAccount,
        data: dict[str, Any],
        *,
        force_isolated: bool = False,
        screenshot_path: str = "",
        keep_browser: bool = False,
    ) -> tuple[bool, str]:
        """登录 → NNC1 → 接受条款 → 填表。菜单与表单已兼容简繁，不再切简体。"""
        import asyncio as _asyncio

        from src.browser.launcher import import_async_playwright

        async_playwright = import_async_playwright()
        async with async_playwright() as p:
            browser = await launch_browser(p, force_isolated=force_isolated)
            context = await create_browser_context(browser)
            page = await context.new_page()
            await self._maximize_browser_window(page)
            self._job_screenshot_path = (screenshot_path or "").strip()
            self._job_summary_shot_written = False

            try:
                logger.info(
                    "IcrisNnc1FormBot: 打开 ICRIS3EP 登录页 (CDP=%s)",
                    not force_isolated,
                )
                t_login = time.monotonic()
                await page.goto(LOGIN_URL, wait_until="commit", timeout=90000)
                logger.info("登录页 goto 完成 (%.1fs)", time.monotonic() - t_login)
                t_portal = time.monotonic()
                if not await wait_portal_ready(page, timeout_ms=90000):
                    if is_cr_public_site(page.url):
                        raise RuntimeError(
                            "门户被重定向到公开站 — 请使用 CDP 指纹浏览器并确保香港出口 IP"
                        )
                    raise RuntimeError("ICRIS3EP 登录页未就绪（可能仍在載入中）")
                logger.info("门户就绪 (%.1fs)", time.monotonic() - t_portal)
                await dismiss_cookie_banner(page)
                logger.info("登录页总等待 %.1fs", time.monotonic() - t_login)

                # --- NNC1-0 前置：登录 / 打开 NNC1 / 接受条款 ---
                await self._login(page, account)
                await self._wait_login_complete(page)
                await self._wait_dashboard(page)
                await dismiss_cookie_banner(page)
                await dismiss_google_translate(page)

                lang = await page_language_state(page)
                if await is_simplified_chinese_active(page):
                    logger.info("仪表盘已是简体，跳过语言切换")
                else:
                    logger.info(
                        "仪表盘为%s，NNC1 已兼容简繁，跳过切简体",
                        lang,
                    )

                if await self._on_nnc1_form_page(page):
                    logger.info("已在 NNC1 填表页，跳过菜单导航: %s", page.url[:120])
                else:
                    await self._open_nnc1(page)
                    await self._accept_efiling_terms(page)

                # --- NNC1-1 輸入基本資料 ---
                await self._fill_step1_basic_info(page, data)
                # --- NNC1-2 輸入公司資料 ---
                await self._fill_step2_company_info(page, data)
                # --- NNC1-3 輸入創辦成員/董事/公司秘書資料 ---
                await self._fill_step3_member_info(page, data)
                # --- NNC1-3.5 创办成员表簽署人 ---
                await self._fill_step3_form_signatory(page)
                # --- NNC1-4 致商業登記署通知書 ---
                await self._fill_step4_br_notice(page)
                # --- NNC1-5 詳情概要 ---
                await self._fill_step5_summary(page)
                # --- NNC1-6 簽署及提交（停在初步检查，不进付款）---
                await self._fill_step6_director_consent_sign(page, account)

                logger.info(
                    "IcrisNnc1FormBot: NNC1 步骤1-6 完成（初步检查通过，未进入付款页）"
                )
                return True, screenshot_path or ""
            except Exception as e:
                logger.exception("IcrisNnc1FormBot 失败")
                await self._maybe_screenshot(page, "error")
                return False, str(e)
            finally:
                if (
                    screenshot_path
                    and not self._job_summary_shot_written
                    and not page.is_closed()
                ):
                    try:
                        await page.screenshot(path=screenshot_path, full_page=True)
                    except Exception as shot_err:
                        logger.warning("最终截图失败: %s", shot_err)
                if keep_browser:
                    logger.info("已加 --keep-browser，浏览器不关闭，按 Ctrl+C 结束")
                    try:
                        while True:
                            await _asyncio.sleep(3600)
                    except (_asyncio.CancelledError, KeyboardInterrupt):
                        logger.info("收到中断，保留浏览器窗口")
                else:
                    await browser.close()
