"""ICRIS 验证码定位与填写（注册/登录页通用）"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from config.settings import PROJECT_ROOT, settings
from src.browser.captcha_best import resolve_2captcha_only, resolve_captcha_best
from src.browser.captcha_solver import decode_data_url_image, try_solve_captcha

try:
    from src.browser.captcha_audio import solve_captcha_from_audio
except ImportError:
    solve_captcha_from_audio = None  # type: ignore

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

CHECK_CODE_SELECTORS = [
    "#checkCode",
    "input[id='checkCode']",
    "input[name='checkCode']",
]

MAX_CAPTCHA_RETRIES = 5
ICRIS_CAPTCHA_LENGTH = 5


async def _scroll_to_captcha_section(page: "Page") -> None:
    """条款页验证码在页面下方，需滚入视口后才会渲染/可识别"""
    captcha_inp = page.locator("#checkCode").first
    if await captcha_inp.count() == 0:
        return

    await captcha_inp.scroll_into_view_if_needed()
    await page.wait_for_timeout(400)

    # 将验证码区域置于视口中上部，避免被底部栏遮挡
    await page.evaluate(
        """() => {
            const el = document.querySelector('#checkCode');
            if (!el) return;
            const rect = el.getBoundingClientRect();
            const offset = window.scrollY + rect.top - window.innerHeight * 0.32;
            window.scrollTo({ top: Math.max(0, offset), behavior: 'instant' });
        }"""
    )
    await page.wait_for_timeout(500)

    # 等待 GIF 验证码 src 加载完成
    try:
        await page.wait_for_function(
            """() => {
                const inp = document.querySelector('#checkCode');
                if (!inp) return false;
                const form = inp.closest('form');
                const scope = form || document;
                const img = scope.querySelector('img[src^="data:image"]');
                return img && img.src && img.src.length > 200;
            }""",
            timeout=10000,
        )
    except Exception:
        logger.warning("滚动后验证码图片 src 仍未就绪")

    captcha_img = page.locator(
        "form img[src^='data:image'], "
        "#checkCode ~ img[src^='data:image'], "
        "#checkCode + img[src^='data:image']"
    ).first
    if await captcha_img.count() > 0:
        await captcha_img.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)

    logger.info("已滚动至图形验证码区域")


async def find_captcha_image(page: "Page"):
    """定位 ICRIS 验证码图片元素"""
    await _scroll_to_captcha_section(page)

    for sel in CHECK_CODE_SELECTORS:
        inp = page.locator(sel).first
        if await inp.count() == 0:
            continue
        form = inp.locator("xpath=ancestor::form[1]")
        data_imgs = form.locator("img[src^='data:image']")
        if await data_imgs.count() > 0:
            img = data_imgs.first
            await img.scroll_into_view_if_needed()
            return img
        sibling_img = inp.locator(
            "xpath=preceding::img[starts-with(@src,'data:image')][1]"
            " | ../img[starts-with(@src,'data:image')]"
            " | ancestor::div[1]//img[starts-with(@src,'data:image')]"
        ).first
        if await sibling_img.count() > 0:
            await sibling_img.scroll_into_view_if_needed()
            return sibling_img

    for img in await page.locator("img[src^='data:image']").all():
        box = await img.bounding_box()
        if box and 60 <= box.get("width", 0) <= 300 and 30 <= box.get("height", 0) <= 120:
            await img.scroll_into_view_if_needed()
            return img

    for sel in ["img[src*='captcha' i]", "img[id*='captcha' i]", "#captchaImg"]:
        el = page.locator(sel).first
        if await el.count() > 0 and await el.is_visible():
            return el
    return None


async def find_captcha_input(page: "Page"):
    for sel in CHECK_CODE_SELECTORS:
        inp = page.locator(sel).first
        if await inp.count() > 0 and await inp.is_visible():
            return inp
    for sel in [
        "input[name*='captcha' i]",
        "input[id*='captcha' i]",
        "input[name*='verify' i]",
    ]:
        inp = page.locator(sel).first
        if await inp.count() > 0 and await inp.is_visible():
            return inp
    return page.locator("#checkCode").first


async def _get_captcha_bytes(captcha_img, page: "Page | None" = None) -> bytes:
    """获取验证码图片：优先 GIF src，失败时用浏览器截图当前帧"""
    src = await captcha_img.get_attribute("src")
    raw = decode_data_url_image(src or "")
    if raw and len(raw) > 100:
        return raw
    png = await captcha_img.screenshot(type="png")
    if len(png) > 100:
        logger.info("使用浏览器截图获取验证码 (%d bytes)", len(png))
        return png
    return raw or png


async def _get_max_length(page: "Page") -> int | None:
    inp = page.locator("#checkCode").first
    if await inp.count() == 0:
        return None
    ml = await inp.get_attribute("maxlength")
    try:
        n = int(ml) if ml else None
        return n if n and n > 0 else None
    except ValueError:
        return None


async def _get_expected_length(page: "Page") -> int:
    """ICRIS 验证码为 5 位；页面 maxlength 常为 -1（未限制）"""
    ml = await _get_max_length(page)
    if ml and ml > 0:
        return ml
    return ICRIS_CAPTCHA_LENGTH


async def _reload_captcha(page: "Page") -> None:
    """点击「重新载入」刷新验证码"""
    await _scroll_to_captcha_section(page)
    reload_link = page.locator(
        "a:has-text('重新载入'), a:has-text('重新載入'), "
        "a:has-text('Reload'), [onclick*='reload' i]"
    ).first
    if await reload_link.count() > 0 and await reload_link.is_visible():
        await reload_link.click()
        await page.wait_for_timeout(1200)
        logger.info("已刷新验证码")


async def _save_debug_image(image_bytes: bytes, attempt: int) -> None:
    if not settings.captcha_save_debug:
        return
    out = PROJECT_ROOT / "output"
    out.mkdir(exist_ok=True)
    path = out / f"captcha_attempt_{attempt}.gif"
    path.write_bytes(image_bytes)
    latest = out / "captcha_latest.gif"
    latest.write_bytes(image_bytes)
    logger.info("验证码图片已保存: %s", latest)


async def _wait_manual_captcha(
    page: "Page",
    captcha_input,
    expected_len: int,
) -> str | None:
    """等待用户在浏览器中手动输入验证码"""
    timeout_ms = settings.captcha_manual_timeout * 1000
    logger.info(
        "请在浏览器验证码框中手动输入 %d 位字符（最多等待 %d 秒）…",
        expected_len,
        settings.captcha_manual_timeout,
    )
    try:
        await page.wait_for_function(
            f"""() => {{
                const el = document.querySelector('#checkCode');
                return el && el.value.trim().length >= {expected_len};
            }}""",
            timeout=timeout_ms,
        )
    except Exception:
        logger.warning("手动输入验证码超时")
        return None

    value = (await captcha_input.input_value() or "").strip()
    if len(value) >= expected_len:
        logger.info("已检测到手动输入验证码: %s", value[:expected_len])
        return value[:expected_len]
    return None


async def _apply_captcha_code(
    page: "Page",
    captcha_input,
    code: str,
    confidence: str,
    max_len: int,
    candidates: list[tuple[str, str]],
) -> bool:
    """根据置信度填写或等待用户确认"""
    if confidence == "low":
        cand_text = ", ".join(f"{s}={c}" for s, c in candidates[:6])
        logger.warning(
            "自动识别不确定，请在浏览器核对/修改验证码（候选: %s）",
            cand_text or code,
        )
        if code:
            await captcha_input.fill("")
            await captcha_input.fill(code)
        manual = await _wait_manual_captcha(page, captcha_input, max_len)
        return manual is not None

    await captcha_input.fill("")
    await captcha_input.fill(code)
    filled = await captcha_input.input_value()
    if filled != code:
        logger.warning("填写校验失败: 期望 %s, 实际 %s", code, filled)
        return False

    if confidence == "medium":
        logger.info(
            "已填写验证码(中等置信度): %s — 若提交失败请手动修改",
            code,
        )
    else:
        logger.info("已填写验证码(高置信度): %s", code)
    return True


async def fill_captcha(page: "Page", llm_client=None) -> bool:
    """
    识别并填写 ICRIS 图形验证码，失败时自动刷新重试。
    """
    try:
        await page.locator("#checkCode").wait_for(state="attached", timeout=15000)
    except Exception:
        logger.warning("验证码输入框未出现")
        return False

    await _scroll_to_captcha_section(page)

    try:
        await page.locator("#checkCode").wait_for(state="visible", timeout=10000)
    except Exception:
        logger.warning("滚动后验证码输入框仍不可见")
        return False

    max_len = await _get_expected_length(page)
    captcha_input = await find_captcha_input(page)
    await captcha_input.scroll_into_view_if_needed()
    mode = (settings.captcha_mode or "auto").lower()

    if mode == "manual":
        captcha_img = await find_captcha_image(page)
        if captcha_img:
            await _save_debug_image(await _get_captcha_bytes(captcha_img), 1)
        code = await _wait_manual_captcha(page, captcha_input, max_len)
        return bool(code)

    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        await page.wait_for_timeout(600)
        await _scroll_to_captcha_section(page)

        code: str | None = None
        confidence = "low"
        candidates: list[tuple[str, str]] = []

        captcha_img = await find_captcha_image(page)
        if not captcha_img and mode not in ("audio",):
            logger.warning("未找到验证码图片 (尝试 %d/%d)", attempt, MAX_CAPTCHA_RETRIES)
            await _reload_captcha(page)
            continue

        image_bytes = b""
        if captcha_img:
            image_bytes = await _get_captcha_bytes(captcha_img, page)
            await _save_debug_image(image_bytes, attempt)
            logger.info(
                "已获取验证码图片 (%d bytes, 尝试 %d/%d)",
                len(image_bytes),
                attempt,
                MAX_CAPTCHA_RETRIES,
            )

        if mode in ("auto", "2captcha") and settings.twocaptcha_api_key and image_bytes:
            code, confidence, candidates = await asyncio.to_thread(
                resolve_2captcha_only, image_bytes, max_len
            )
            if code and len(code) >= max_len:
                logger.info("2Captcha 识别成功: %s (置信度=%s)", code[:max_len], confidence)
            elif mode == "2captcha":
                logger.warning(
                    "2Captcha 未识别成功 (尝试 %d/%d)，刷新验证码重试",
                    attempt,
                    MAX_CAPTCHA_RETRIES,
                )
                await _reload_captcha(page)
                continue
            else:
                logger.warning("2Captcha 未成功，尝试 OCR/LLM 回退")
                code, confidence, candidates = await resolve_captcha_best(
                    page,
                    image_bytes,
                    max_len,
                    llm_client=llm_client,
                    use_audio=False,
                    skip_2captcha=True,
                )
        elif mode == "audio" and solve_captcha_from_audio:
            try:
                code = await solve_captcha_from_audio(
                    page, expected_len=max_len, llm_client=llm_client
                )
                confidence = "medium" if code and len(code) >= max_len else "low"
            except Exception as e:
                logger.warning("语音识别失败: %s", e)
        elif mode in ("ocr", "ollama", "2captcha") and image_bytes:
            code = await asyncio.to_thread(
                try_solve_captcha, image_bytes, llm_client, max_len
            )
            confidence = "high" if code and len(code) >= max_len and mode == "2captcha" else (
                "medium" if code and len(code) >= max_len else "low"
            )
        elif mode == "auto" and image_bytes:
            code, confidence, candidates = await resolve_captcha_best(
                page,
                image_bytes,
                max_len,
                llm_client=llm_client,
                use_audio=False,
                skip_2captcha=True,
            )

        if not code or len(code) < max_len:
            if attempt >= MAX_CAPTCHA_RETRIES - 1:
                manual = await _wait_manual_captcha(page, captcha_input, max_len)
                if manual:
                    return True
            await _reload_captcha(page)
            continue

        if await captcha_input.count() == 0:
            logger.warning("未找到输入框")
            return False

        ok = await _apply_captcha_code(
            page, captcha_input, code[:max_len], confidence, max_len, candidates
        )
        if ok:
            return True

        if attempt >= MAX_CAPTCHA_RETRIES - 1:
            manual = await _wait_manual_captcha(page, captcha_input, max_len)
            if manual:
                return True
        await _reload_captcha(page)

    if mode in ("auto", "audio"):
        manual = await _wait_manual_captcha(page, captcha_input, max_len)
        if manual:
            return True

    logger.error("验证码识别/填写失败，已重试 %d 次", MAX_CAPTCHA_RETRIES)
    return False
