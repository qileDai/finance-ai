"""ICRIS 验证码最佳识别：2Captcha 优先 + 多引擎回退"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import Counter
from typing import TYPE_CHECKING

from config.settings import settings
from src.browser.captcha_fusion import assess_confidence, pick_best_captcha
from src.browser.captcha_solver import normalize_captcha_text, solve_ocr_only

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


def _solve_llm_vision(image_bytes: bytes, llm_client, max_len: int = 5) -> str | None:
    if not llm_client or not settings.openai_api_key:
        return None
    try:
        from src.browser.captcha_api import prepare_image_variants

        variants = prepare_image_variants(image_bytes)
        png = next((p for label, p in variants if label == "merged_min"), variants[0][1])
        b64 = base64.b64encode(png).decode()
        raw = llm_client.solve_captcha_from_image(b64, expected_length=max_len)
        code = normalize_captcha_text(raw, max_len)
        if code and len(code) >= max_len:
            logger.info("LLM 视觉识别: %s", code[:max_len])
            return code[:max_len]
        if code:
            logger.warning("LLM 视觉位数不足: %s", code)
    except Exception as e:
        logger.warning("LLM 视觉识别失败: %s", e)
    return None


def _solve_2captcha_voted(image_bytes: bytes, max_len: int = 5) -> list[tuple[str, str]]:
    if not settings.twocaptcha_api_key:
        return []
    try:
        from src.browser.captcha_api import solve_2captcha_voted

        return solve_2captcha_voted(
            image_bytes,
            settings.twocaptcha_api_key,
            min_len=max_len,
            max_len=max_len,
        )
    except Exception as e:
        logger.error("2Captcha API 调用失败: %s", e)
        return []


def resolve_2captcha_only(
    image_bytes: bytes,
    max_len: int = 5,
) -> tuple[str | None, str, list[tuple[str, str]]]:
    """
    仅使用 2Captcha：多帧并行提交 + 投票共识。
    配置了 TWOCAPTCHA_API_KEY 时应优先调用此函数。
    返回 (code, confidence, candidates)
    """
    if not settings.twocaptcha_api_key:
        logger.warning("2Captcha 未配置 TWOCAPTCHA_API_KEY，跳过")
        return None, "low", []

    logger.info("正在调用 2Captcha API（密钥已配置）…")
    try:
        from src.browser.captcha_api import solve_2captcha_fast

        fast = solve_2captcha_fast(
            image_bytes,
            settings.twocaptcha_api_key,
            min_len=max_len,
            max_len=max_len,
        )
        if fast:
            src, code = fast
            if code and len(code) >= max_len:
                logger.info("2Captcha 快速识别: %s", code[:max_len])
                return code[:max_len], "high", [(src, code[:max_len])]
    except Exception as e:
        logger.warning("2Captcha 快速路径失败: %s", e)

    if settings.twocaptcha_max_variants <= 1:
        return None, "low", []

    candidates = _solve_2captcha_voted(image_bytes, max_len)
    if not candidates:
        logger.error("2Captcha 全部变体均未返回有效结果")
        try:
            from src.browser.captcha_api import solve_2captcha

            code = solve_2captcha(
                image_bytes,
                settings.twocaptcha_api_key,
                min_len=max_len,
                max_len=max_len,
            )
            if code and len(code) >= max_len:
                pair = ("2captcha:sequential", code[:max_len])
                logger.info("2Captcha 顺序识别成功: %s", code[:max_len])
                return code[:max_len], "high", [pair]
        except Exception as e:
            logger.warning("2Captcha 顺序识别失败: %s", e)
        return None, "low", []

    codes = [c[:max_len] for _, c in candidates if len(c) >= max_len]
    if not codes:
        return None, "low", candidates

    winner, votes = Counter(codes).most_common(1)[0]
    if votes >= 2:
        logger.info("2Captcha 多帧共识 (%d/%d): %s", votes, len(codes), winner)
        return winner, "high", candidates

    logger.info("2Captcha 单帧结果: %s (共 %d 个候选)", winner, len(codes))
    return winner, "high", candidates


def collect_image_candidates(
    image_bytes: bytes,
    llm_client=None,
    max_len: int = 5,
) -> list[tuple[str, str]]:
    """同步收集所有图形识别候选（2Captcha 多帧 + LLM + OCR）"""
    candidates: list[tuple[str, str]] = []

    for src, code in _solve_2captcha_voted(image_bytes, max_len):
        if code and len(code) >= max_len:
            candidates.append((src, code[:max_len]))

    llm_code = _solve_llm_vision(image_bytes, llm_client, max_len)
    if llm_code:
        candidates.append(("llm", llm_code))

    try:
        ocr_code = solve_ocr_only(image_bytes, max_length=max_len)
        if ocr_code and len(ocr_code) >= max_len:
            candidates.append(("ocr", ocr_code[:max_len]))
    except Exception as e:
        logger.warning("OCR 识别失败: %s", e)

    return candidates


def _collect_non_2captcha_candidates(
    image_bytes: bytes,
    llm_client=None,
    max_len: int = 5,
) -> list[tuple[str, str]]:
    """LLM + OCR 候选（不含 2Captcha）"""
    candidates: list[tuple[str, str]] = []

    llm_code = _solve_llm_vision(image_bytes, llm_client, max_len)
    if llm_code:
        candidates.append(("llm", llm_code))

    try:
        ocr_code = solve_ocr_only(image_bytes, max_length=max_len)
        if ocr_code and len(ocr_code) >= max_len:
            candidates.append(("ocr", ocr_code[:max_len]))
    except Exception as e:
        logger.warning("OCR 识别失败: %s", e)

    return candidates


async def resolve_captcha_best(
    page: "Page",
    image_bytes: bytes,
    max_len: int,
    llm_client=None,
    *,
    use_audio: bool = True,
    skip_2captcha: bool = False,
) -> tuple[str | None, str, list[tuple[str, str]]]:
    """
    多引擎识别（2Captcha 已由 resolve_2captcha_only 优先处理时可 skip_2captcha=True）。
    """
    candidates: list[tuple[str, str]] = []

    if not skip_2captcha and settings.twocaptcha_api_key:
        for src, code in _solve_2captcha_voted(image_bytes, max_len):
            if code and len(code) >= max_len:
                candidates.append((src, code[:max_len]))

    image_candidates = await asyncio.to_thread(
        _collect_non_2captcha_candidates,
        image_bytes,
        llm_client,
        max_len,
    )
    candidates.extend(image_candidates)

    if use_audio and settings.openai_api_key:
        try:
            from src.browser.captcha_audio import solve_captcha_from_audio

            audio_code = await solve_captcha_from_audio(
                page, expected_len=max_len, llm_client=llm_client
            )
            if audio_code and len(audio_code) >= max_len:
                candidates.append(("audio", audio_code[:max_len]))
                logger.info("语音识别: %s", audio_code[:max_len])
        except Exception as e:
            logger.warning("语音识别失败: %s", e)

    if not candidates:
        return None, "low", []

    best = pick_best_captcha(candidates, expected_len=max_len)
    if not best or len(best) < max_len:
        return None, "low", candidates

    confidence = assess_confidence(candidates, best[:max_len], expected_len=max_len)
    logger.info(
        "验证码最佳结果: %s (置信度=%s, 来源=%d)",
        best[:max_len],
        confidence,
        len(candidates),
    )
    return best[:max_len], confidence, candidates
