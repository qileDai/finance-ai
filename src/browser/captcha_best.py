"""ICRIS 验证码最佳识别：多引擎并行 + 投票 + 置信度"""

from __future__ import annotations

import asyncio
import base64
import logging
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
        logger.warning("2Captcha 投票识别失败: %s", e)
        return []


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


async def resolve_captcha_best(
    page: "Page",
    image_bytes: bytes,
    max_len: int,
    llm_client=None,
    *,
    use_audio: bool = True,
) -> tuple[str | None, str, list[tuple[str, str]]]:
    """
    最佳识别流程：
    1. 并行线程：2Captcha 多帧投票 + LLM 视觉 + OCR
    2. 串行：粤语语音识别（避免与页面交互冲突）
    3. 融合 + 置信度评估
    """
    candidates = await asyncio.to_thread(
        collect_image_candidates,
        image_bytes,
        llm_client,
        max_len,
    )

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
